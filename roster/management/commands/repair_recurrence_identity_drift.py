import json
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from roster.models import ContributorEntity, MembershipAssessment, AuditEvent, ContributionCluster
from roster.services.membership import evaluate_membership_for_entities

User = get_user_model()


class Command(BaseCommand):
    help = "Identifies and repairs entities auto-verified solely through cluster-confidence escalation, restoring unverified status and provisional membership authority."

    def add_arguments(self, parser):
        parser.add_argument(
            '--actor',
            type=str,
            required=True,
            help='Username of the authorized administrator performing the repair.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=True,
            help='Preview correction manifest without committing changes (default: True).'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            default=False,
            help='Confirm execution of identity and assessment repairs.'
        )

    def handle(self, *args, **options):
        actor_name = options['actor']
        confirm = options['confirm']
        dry_run = not confirm

        # Verify actor user & authorization
        actor_user = User.objects.filter(username=actor_name).first()
        if not actor_user:
            raise CommandError(f"Actor '{actor_name}' does not exist in the database.")
        if not (actor_user.is_superuser or actor_user.has_perm('roster.manage_identity')):
            raise CommandError(f"Actor '{actor_name}' lacks 'manage_identity' permission.")

        self.stdout.write(self.style.MIGRATE_HEADING("=== STAGE 2B.1 IDENTITY & RECURRENCE DRIFT REPAIR ==="))
        self.stdout.write(f"Actor: {actor_name}")
        self.stdout.write(f"Mode: {'DRY-RUN (Preview Only)' if dry_run else 'MUTATING EXECUTION'}\n")

        # 1. Identify entities auto-verified solely through cluster-confidence escalation
        # These are entities where is_verified=True or verification_status='VERIFIED',
        # but verification_method is 'NONE' or verified_by is null (no manual/external record).
        invalid_verified_entities = ContributorEntity.objects.filter(
            Q(is_verified=True) | Q(verification_status='VERIFIED'),
            Q(verification_method='NONE') | Q(verified_by__isnull=True)
        ).select_related().prefetch_related('clusters', 'membership_assessments')

        total_invalid = invalid_verified_entities.count()

        if total_invalid == 0:
            self.stdout.write(self.style.SUCCESS("No invalid auto-verified entities detected. System is clean."))
            return

        # 2. Build non-PII Correction Manifest
        manifest = []
        entity_ids = []

        for ent in invalid_verified_entities:
            entity_ids.append(ent.id)
            cluster_ids = list(ent.clusters.values_list('id', flat=True))
            current_ass = ent.membership_assessments.filter(is_current=True).first()
            ass_id = current_ass.id if current_ass else None
            ass_status = current_ass.calculated_status if current_ass else 'UNKNOWN'

            manifest.append({
                'entity_id': ent.id,
                'entity_type': ent.entity_type,
                'previous_is_verified': ent.is_verified,
                'previous_verification_status': ent.verification_status,
                'previous_verification_method': ent.verification_method,
                'cluster_ids': cluster_ids,
                'current_assessment_id': ass_id,
                'current_assessment_status': ass_status,
                'proposed_verification_status': 'UNVERIFIED',
                'proposed_verification_method': 'NONE',
                'proposed_membership_authority': 'PROVISIONAL',
                'reason_code': 'INVALID_CLUSTER_CONFIDENCE_AUTOVERIFICATION'
            })

        self.stdout.write(self.style.WARNING(f"Identified {total_invalid} entities verified solely through cluster-confidence escalation.\n"))

        # Output non-PII manifest preview (first 10 records)
        self.stdout.write("--- CORRECTION MANIFEST PREVIEW (First 10 of {0} records) ---".format(total_invalid))
        self.stdout.write(f"{'Entity ID':<10} | {'Type':<12} | {'Prev Verified':<14} | {'Current Status':<20} | {'Proposed Status':<20}")
        self.stdout.write("-" * 80)
        for item in manifest[:10]:
            self.stdout.write(
                f"{item['entity_id']:<10} | {item['entity_type']:<12} | {str(item['previous_is_verified']):<14} | "
                f"{item['current_assessment_status']:<20} | PROVISIONAL (UNVERIFIED)"
            )

        self.stdout.write("-" * 80 + "\n")

        if dry_run:
            self.stdout.write(self.style.NOTICE("Dry-run mode active. No database modifications were made."))
            self.stdout.write(self.style.NOTICE("Run with --confirm --actor <username> to execute identity unverification and assessment recalculation.\n"))
            return

        # 3. Mutating Execution inside transaction
        self.stdout.write("Executing identity repair and assessment recalculations...")
        with transaction.atomic():
            # Update entities to UNVERIFIED status
            updated_count = ContributorEntity.objects.filter(id__in=entity_ids).update(
                is_verified=False,
                verification_status='UNVERIFIED',
                verification_method='NONE',
                verified_at=None,
                verified_by=None,
                verification_explanation="Reset during Stage 2B.1 identity drift repair (removed auto-verification from cluster confidence)."
            )

            # Record AuditEvent
            AuditEvent.objects.create(
                event_type='IDENTITY_DRIFT_REPAIR',
                description=f"Repaired {updated_count} entities auto-verified through cluster confidence escalation. Restored UNVERIFIED status.",
                actor=actor_name
            )

            # Recalculate membership assessments for repaired entities
            evaluate_membership_for_entities(entity_ids, actor=actor_name)

            # Create Rollback Manifest audit record
            AuditEvent.objects.create(
                event_type='IDENTITY_REPAIR_MANIFEST',
                description=f"Stored repair manifest for {updated_count} entities.",
                actor=actor_name
            )

        self.stdout.write(self.style.SUCCESS(f"Successfully repaired {updated_count} entities and recalculated current membership assessments."))
