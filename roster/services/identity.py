import os
import json
import uuid
import hashlib
from datetime import datetime
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError

from roster.models import ContributorEntity, MembershipAssessment, AuditEvent
from roster.services.membership import evaluate_membership_for_entities

User = get_user_model()


def _validate_actor(actor):
    """
    Validates that the actor exists, is active, and possesses manage_identity or superuser permissions.
    """
    if isinstance(actor, str):
        user_obj = User.objects.filter(username=actor).first()
        if not user_obj:
            raise ValidationError(f"Actor user '{actor}' does not exist.")
        actor_user = user_obj
        actor_name = actor
    elif isinstance(actor, User):
        actor_user = actor
        actor_name = actor.username
    else:
        raise ValidationError("Actor must be a User instance or valid username string.")

    if not actor_user.is_active:
        raise PermissionDenied(f"Actor '{actor_name}' is inactive.")

    if not (actor_user.is_superuser or actor_user.has_perm('roster.manage_identity')):
        raise PermissionDenied(f"Actor '{actor_name}' lacks 'manage_identity' permission.")

    return actor_user, actor_name


@transaction.atomic
def verify_contributor_identity(*, entity, method, actor, evidence=None, explanation=""):
    """
    Centralized service boundary for manually or external-match verifying a contributor entity identity.
    """
    actor_user, actor_name = _validate_actor(actor)

    allowed_methods = ['ADMIN_REVIEW', 'EXTERNAL_IDENTITY_MATCH', 'LEGACY_REVIEWED']
    if method not in allowed_methods:
        raise ValidationError(f"Verification method '{method}' is invalid. Allowed: {allowed_methods}")

    evidence = evidence or {}
    explanation = (explanation or "").strip()

    # Method specific validations
    if method == 'ADMIN_REVIEW':
        if not explanation:
            raise ValidationError("ADMIN_REVIEW verification requires a non-blank explanation.")
    elif method == 'EXTERNAL_IDENTITY_MATCH':
        if not isinstance(evidence, dict) or not evidence.get('source_type') or not evidence.get('source_version'):
            raise ValidationError("EXTERNAL_IDENTITY_MATCH requires structured evidence with 'source_type' and 'source_version'.")
        # Assert NO source PII in evidence JSON
        pii_keys = {'first_name', 'last_name', 'name', 'street', 'address', 'email', 'phone', 'ssn'}
        if any(k.lower() in pii_keys for k in evidence.keys()):
            raise ValidationError("EXTERNAL_IDENTITY_MATCH evidence must contain reference IDs only, not raw source PII.")
        if not explanation:
            raise ValidationError("EXTERNAL_IDENTITY_MATCH verification requires a non-blank explanation.")
    elif method == 'LEGACY_REVIEWED':
        if not explanation:
            raise ValidationError("LEGACY_REVIEWED verification requires a non-blank legacy basis explanation.")

    entity.verification_status = 'VERIFIED'
    entity.is_verified = True
    entity.verification_method = method
    entity.verified_at = timezone.now()
    entity.verified_by = actor_name
    entity.verification_evidence = evidence
    entity.verification_explanation = explanation
    entity.save()

    # Recalculate current membership assessment for entity
    evaluate_membership_for_entities([entity.id], actor=actor_name)

    # Log AuditEvent
    AuditEvent.objects.create(
        event_type='IDENTITY_VERIFIED',
        description=f"Verified entity ID {entity.id} using method '{method}'.",
        actor=actor_name
    )

    return entity


@transaction.atomic
def unverify_contributor_identity(*, entity, actor, reason=""):
    """
    Centralized service boundary for unverifying a contributor entity identity.
    Resets verification fields to defaults; reason is stored ONLY in immutable AuditEvent.
    """
    actor_user, actor_name = _validate_actor(actor)

    prev_method = entity.verification_method
    entity.verification_status = 'UNVERIFIED'
    entity.is_verified = False
    entity.verification_method = 'NONE'
    entity.verified_at = None
    entity.verified_by = None
    entity.verification_evidence = {}
    entity.verification_explanation = ''
    entity.save()

    # Recalculate current membership assessment for entity
    evaluate_membership_for_entities([entity.id], actor=actor_name)

    # Log AuditEvent with reason
    reason_str = f" Reason: {reason}" if reason else ""
    AuditEvent.objects.create(
        event_type='IDENTITY_UNVERIFIED',
        description=f"Unverified entity ID {entity.id} (previously method '{prev_method}').{reason_str}",
        actor=actor_name
    )

    return entity


def bulk_unverify_identity_drift(*, actor, dry_run=True, confirm=False, output_base_dir=None):
    """
    Bounded bulk repair service that validates actor once, generates unique directory manifests,
    unverifies drifted entities, recalculates assessments in bulk, and logs SHA-256 audited events.
    """
    actor_user, actor_name = _validate_actor(actor)

    # 1. Identify invalid auto-verified entities
    drifted_entities = ContributorEntity.objects.filter(
        verification_status='VERIFIED',
        verification_method='NONE'
    ).select_related().prefetch_related('membership_assessments')

    total_drifted = drifted_entities.count()

    if total_drifted == 0:
        return {
            'status': 'CLEAN',
            'repaired_count': 0,
            'manifest_dir': None,
            'correction_hash': None,
            'rollback_hash': None
        }

    run_uuid = str(uuid.uuid4())
    base_dir = output_base_dir or os.path.join("artifacts", "audit", "identity_repair")
    run_dir = os.path.abspath(os.path.join(base_dir, run_uuid))

    manifest_records = []
    rollback_records = []
    entity_ids = []

    for ent in drifted_entities:
        entity_ids.append(ent.id)
        current_ass = ent.membership_assessments.filter(is_current=True).first()
        ass_id = current_ass.id if current_ass else None

        manifest_records.append({
            'entity_id': ent.id,
            'entity_type': ent.entity_type,
            'previous_verification_status': ent.verification_status,
            'previous_verification_method': ent.verification_method,
            'previous_is_verified': ent.is_verified,
            'previous_current_assessment_id': ass_id,
            'corrected_verification_status': 'UNVERIFIED',
            'corrected_verification_method': 'NONE',
            'corrected_is_verified': False,
            'reason_code': 'INVALID_CLUSTER_CONFIDENCE_AUTOVERIFICATION'
        })

        rollback_records.append({
            'entity_id': ent.id,
            'restored_verification_status': ent.verification_status,
            'restored_verification_method': ent.verification_method,
            'restored_is_verified': ent.is_verified,
            'restored_verified_at': ent.verified_at.isoformat() if ent.verified_at else None,
            'restored_verified_by': ent.verified_by
        })

    if dry_run or not confirm:
        return {
            'status': 'DRY_RUN',
            'repaired_count': total_drifted,
            'manifest_records': manifest_records[:10],
            'run_uuid': run_uuid
        }

    # Atomic mutating execution
    os.makedirs(run_dir, exist_ok=False)

    correction_manifest_path = os.path.join(run_dir, "correction_manifest.json")
    rollback_manifest_path = os.path.join(run_dir, "rollback_manifest.json")
    summary_path = os.path.join(run_dir, "run_summary.json")

    correction_content = json.dumps({'schema_version': '1.0', 'run_uuid': run_uuid, 'records': manifest_records}, indent=2)
    rollback_content = json.dumps({'schema_version': '1.0', 'run_uuid': run_uuid, 'records': rollback_records}, indent=2)

    with open(correction_manifest_path, 'w') as f:
        f.write(correction_content)
    with open(rollback_manifest_path, 'w') as f:
        f.write(rollback_content)

    corr_hash = hashlib.sha256(correction_content.encode('utf-8')).hexdigest()
    roll_hash = hashlib.sha256(rollback_content.encode('utf-8')).hexdigest()

    summary_content = json.dumps({
        'schema_version': '1.0',
        'run_uuid': run_uuid,
        'actor': actor_name,
        'timestamp': timezone.now().isoformat(),
        'total_repaired': total_drifted,
        'correction_manifest_sha256': corr_hash,
        'rollback_manifest_sha256': roll_hash
    }, indent=2)

    with open(summary_path, 'w') as f:
        f.write(summary_content)

    with transaction.atomic():
        updated_count = ContributorEntity.objects.filter(id__in=entity_ids).update(
            is_verified=False,
            verification_status='UNVERIFIED',
            verification_method='NONE',
            verified_at=None,
            verified_by=None,
            verification_evidence={},
            verification_explanation=''
        )

        # Bulk recalculate membership assessments
        evaluate_membership_for_entities(entity_ids, actor=actor_name)

        AuditEvent.objects.create(
            event_type='IDENTITY_DRIFT_REPAIR',
            description=f"Repaired {updated_count} entities. Manifest UUID: {run_uuid}. Correction SHA256: {corr_hash}",
            actor=actor_name
        )

    return {
        'status': 'COMPLETED',
        'repaired_count': updated_count,
        'run_dir': run_dir,
        'run_uuid': run_uuid,
        'correction_sha256': corr_hash,
        'rollback_sha256': roll_hash
    }
