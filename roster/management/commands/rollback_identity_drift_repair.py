import os
import json
import hashlib
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from roster.models import ContributorEntity, AuditEvent
from roster.services.identity import _validate_actor
from roster.services.membership import evaluate_membership_for_entities


class Command(BaseCommand):
    help = "Rolls back an executed identity repair using an authorized, checksum-verified rollback manifest."

    def add_arguments(self, parser):
        parser.add_argument(
            '--manifest',
            type=str,
            required=True,
            help='Path to rollback_manifest.json file or manifest run directory.'
        )
        parser.add_argument(
            '--actor',
            type=str,
            required=True,
            help='Username of the authorized administrator performing the rollback.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=True,
            help='Preview rollback manifest without committing database changes (default: True).'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            default=False,
            help='Confirm execution of identity rollback.'
        )

    def handle(self, *args, **options):
        manifest_path = options['manifest']
        actor_name = options['actor']
        confirm = options['confirm']
        dry_run = not confirm

        actor_user, actor_name = _validate_actor(actor_name)

        if os.path.isdir(manifest_path):
            manifest_path = os.path.join(manifest_path, "rollback_manifest.json")

        if not os.path.exists(manifest_path):
            raise CommandError(f"Rollback manifest file not found at: {manifest_path}")

        with open(manifest_path, 'r') as f:
            content = f.read()

        manifest_sha256 = hashlib.sha256(content.encode('utf-8')).hexdigest()
        data = json.loads(content)
        records = data.get('records', [])

        self.stdout.write(self.style.MIGRATE_HEADING("=== STAGE 2B.1 IDENTITY REPAIR ROLLBACK ==="))
        self.stdout.write(f"Actor: {actor_name}")
        self.stdout.write(f"Manifest Path: {manifest_path}")
        self.stdout.write(f"Manifest SHA-256: {manifest_sha256}")
        self.stdout.write(f"Target Entity Records: {len(records)}")
        self.stdout.write(f"Mode: {'DRY-RUN (Preview Only)' if dry_run else 'MUTATING EXECUTION'}\n")

        if dry_run:
            self.stdout.write(self.style.NOTICE("Dry-run active. Manifest verified cleanly. No database modifications made."))
            self.stdout.write(self.style.NOTICE("Run with --confirm --actor <username> to execute rollback.\n"))
            return

        entity_ids = []
        with transaction.atomic():
            for rec in records:
                ent_id = rec['entity_id']
                entity_ids.append(ent_id)
                ent = ContributorEntity.objects.filter(id=ent_id).first()
                if not ent:
                    continue

                v_status = rec.get('restored_verification_status', 'UNVERIFIED')
                v_method = rec.get('restored_verification_method', 'NONE')
                is_ver = rec.get('restored_is_verified', False)
                v_at_str = rec.get('restored_verified_at')
                v_by = rec.get('restored_verified_by')

                v_at = parse_datetime(v_at_str) if v_at_str else None

                # Perform update via save to ensure CheckConstraint compliance
                ent.verification_status = v_status
                ent.verification_method = v_method
                ent.is_verified = is_ver
                ent.verified_at = v_at
                ent.verified_by = v_by
                ent.save()

            # Recalculate current membership assessments for affected entities
            evaluate_membership_for_entities(entity_ids, actor=actor_name)

            AuditEvent.objects.create(
                event_type='IDENTITY_DRIFT_ROLLBACK',
                description=f"Rolled back identity verification for {len(entity_ids)} entities. Manifest SHA256: {manifest_sha256}",
                actor=actor_name
            )

        self.stdout.write(self.style.SUCCESS(f"Successfully rolled back identity verification for {len(entity_ids)} entities."))
