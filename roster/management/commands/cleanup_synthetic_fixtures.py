import os
import json
import hashlib
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from roster.models import ContributorEntity, ContributionClusterAssignment, MembershipAssessment, AuditEvent
from roster.services.identity import _validate_actor


class Command(BaseCommand):
    help = "Manifest-driven cleanup of reviewed synthetic benchmark entities with strict dependency validation."

    def add_arguments(self, parser):
        parser.add_argument(
            '--manifest',
            type=str,
            required=True,
            help='Path to JSON cleanup manifest containing list of approved entity_ids.'
        )
        parser.add_argument(
            '--actor',
            type=str,
            required=True,
            help='Username of the authorized administrator performing the cleanup.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=True,
            help='Preview cleanup without executing database deletion (default: True).'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            default=False,
            help='Confirm execution of synthetic entity deletion.'
        )

    def handle(self, *args, **options):
        manifest_path = options['manifest']
        actor_name = options['actor']
        confirm = options['confirm']
        dry_run = not confirm

        actor_user, actor_name = _validate_actor(actor_name)

        if not os.path.exists(manifest_path):
            raise CommandError(f"Cleanup manifest file not found at: {manifest_path}")

        with open(manifest_path, 'r') as f:
            content = f.read()

        data = json.loads(content)
        entity_ids = data.get('entity_ids', [])
        manifest_sha256 = hashlib.sha256(content.encode('utf-8')).hexdigest()

        self.stdout.write(self.style.MIGRATE_HEADING("=== SYNTHETIC FIXTURE MANIFEST CLEANUP ==="))
        self.stdout.write(f"Actor: {actor_name}")
        self.stdout.write(f"Manifest Path: {manifest_path}")
        self.stdout.write(f"Manifest SHA-256: {manifest_sha256}")
        self.stdout.write(f"Target Entity IDs ({len(entity_ids)}): {entity_ids}")
        self.stdout.write(f"Mode: {'DRY-RUN (Preview Only)' if dry_run else 'MUTATING EXECUTION'}\n")

        # Dependency check for each target entity ID
        blocked = []
        entities_to_delete = []
        for ent_id in entity_ids:
            ent = ContributorEntity.objects.filter(id=ent_id).first()
            if not ent:
                continue

            assign_count = ContributionClusterAssignment.objects.filter(contribution_cluster__contributor_entity=ent).count()
            if assign_count > 0:
                blocked.append((ent_id, f"Has {assign_count} active contribution assignments."))
            else:
                entities_to_delete.append(ent)

        if blocked:
            raise CommandError(f"Cleanup blocked due to active dependencies: {blocked}")

        self.stdout.write(f"Validated {len(entities_to_delete)} synthetic entities eligible for deletion (0 active dependencies).")

        if dry_run:
            self.stdout.write(self.style.NOTICE("Dry-run active. Manifest and dependencies verified cleanly. No database modifications made."))
            self.stdout.write(self.style.NOTICE("Run with --confirm --actor <username> to execute deletion.\n"))
            return

        with transaction.atomic():
            deleted_count = 0
            for ent in entities_to_delete:
                ent.delete()
                deleted_count += 1

            AuditEvent.objects.create(
                event_type='SYNTHETIC_FIXTURES_CLEANED',
                description=f"Purged {deleted_count} synthetic benchmark entities from manifest {manifest_sha256}",
                actor=actor_name
            )

        self.stdout.write(self.style.SUCCESS(f"Successfully purged {deleted_count} synthetic benchmark entities."))
