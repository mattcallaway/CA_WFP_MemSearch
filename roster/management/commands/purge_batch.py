from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.contrib.auth.models import User
from roster.models import (
    ImportBatch, RawContribution, Contribution, ContributionCluster, 
    ContributorEntity, AuditEvent
)

class Command(BaseCommand):
    help = 'Permanently purges an import batch and all associated records (requires superuser status)'

    def add_arguments(self, parser):
        parser.add_argument('--batch-id', type=int, required=True, help='ID of the batch to purge')
        parser.add_argument('--actor', type=str, required=True, help='Username of the superuser executing the purge')
        parser.add_argument('--dry-run', action='store_true', help='Preview deletion counts without performing the purge')
        parser.add_argument('--confirm', action='store_true', help='Skip interactive confirmation prompt')

    def handle(self, *args, **options):
        batch_id = options['batch_id']
        actor_username = options['actor']
        dry_run = options['dry_run']
        confirm = options['confirm']

        # 1. Superuser verification
        try:
            actor = User.objects.get(username=actor_username)
        except User.DoesNotExist:
            raise CommandError(f"User '{actor_username}' does not exist.")

        if not actor.is_superuser:
            raise CommandError(f"User '{actor_username}' is not a superuser. Only superusers are authorized to purge batches.")

        # 2. Retrieve batch
        try:
            batch = ImportBatch.objects.get(id=batch_id)
        except ImportBatch.DoesNotExist:
            raise CommandError(f"ImportBatch with ID {batch_id} does not exist.")

        # 3. Calculate deletion preview
        raw_count = RawContribution.objects.filter(import_batch=batch).count()
        contrib_count = Contribution.objects.filter(raw_contribution__import_batch=batch).count()
        
        # Calculate clusters and entities that will become empty
        # A cluster is empty if it has no assignments remaining after this batch's assignments are deleted
        assignments_count = Contribution.objects.filter(
            raw_contribution__import_batch=batch
        ).values_list('assignments__id', flat=True)
        
        # We can find candidate clusters
        affected_cluster_ids = ContributionCluster.objects.filter(
            assignments__contribution__raw_contribution__import_batch=batch
        ).values_list('id', flat=True).distinct()
        
        empty_clusters_count = 0
        empty_entities_count = 0
        
        for cid in affected_cluster_ids:
            cluster = ContributionCluster.objects.get(id=cid)
            remaining_assigns = cluster.assignments.exclude(contribution__raw_contribution__import_batch=batch).count()
            if remaining_assigns == 0:
                empty_clusters_count += 1
                entity = cluster.contributor_entity
                # An entity is empty if all its clusters are empty
                total_clusters = entity.clusters.count()
                # Find how many of this entity's clusters are in the list of empty clusters in this purge
                purging_clusters_for_entity = 0
                for c in entity.clusters.all():
                    rem = c.assignments.exclude(contribution__raw_contribution__import_batch=batch).count()
                    if rem == 0:
                        purging_clusters_for_entity += 1
                if total_clusters == purging_clusters_for_entity:
                    empty_entities_count += 1

        self.stdout.write(self.style.WARNING("=== DELETION PREVIEW ==="))
        self.stdout.write(f"Batch ID: {batch.id}")
        self.stdout.write(f"Filename: {batch.file_name}")
        self.stdout.write(f"Raw Contribution Records to delete: {raw_count}")
        self.stdout.write(f"Normalized Contribution Records to delete: {contrib_count}")
        self.stdout.write(f"Contribution Clusters to delete: {empty_clusters_count}")
        self.stdout.write(f"Contributor Profiles/Entities to delete: {empty_entities_count}")
        self.stdout.write(f"ImportBatch Record to delete: 1")
        self.stdout.write(self.style.WARNING("========================"))

        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry-run mode active. No changes were committed to the database."))
            return

        # 4. Confirmation checks
        if not confirm:
            self.stdout.write(self.style.NOTICE("This action is permanent and CANNOT be undone."))
            confirm_val = input(f"Type the batch filename '{batch.file_name}' exactly to confirm deletion: ")
            if confirm_val != batch.file_name:
                raise CommandError("Purge cancelled: typed filename did not match.")

        # 5. Execute purge in a transaction
        with transaction.atomic():
            # Delete contributions and raw records
            Contribution.objects.filter(raw_contribution__import_batch=batch).delete()
            RawContribution.objects.filter(import_batch=batch).delete()

            # Clean empty clusters and entities
            empty_clusters = ContributionCluster.objects.filter(assignments__isnull=True)
            deleted_clusters = 0
            deleted_entities = 0
            for cluster in empty_clusters:
                entity = cluster.contributor_entity
                cluster.delete()
                deleted_clusters += 1
                if entity.clusters.count() == 0:
                    entity.delete()
                    deleted_entities += 1

            batch.delete()

            AuditEvent.objects.create(
                event_type="PURGE_BATCH",
                description=f"Permanently purged batch {batch_id} ('{batch.file_name}'). Deleted {raw_count} raw rows, {contrib_count} contributions, {deleted_clusters} clusters, {deleted_entities} entities.",
                actor=actor.username
            )

        self.stdout.write(self.style.SUCCESS(f"Successfully purged batch {batch_id} and all related entities."))
