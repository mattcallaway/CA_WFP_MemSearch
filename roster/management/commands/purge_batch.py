from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from roster.models import (
    ImportBatch, RawContribution, Contribution, ContributionCluster, 
    ContributorEntity, AuditEvent, MembershipAssessment
)
from roster.services.importer import purge_batch

class Command(BaseCommand):
    help = 'Permanently purges an import batch and all associated records (requires superuser status or purge_data permission)'

    def add_arguments(self, parser):
        parser.add_argument('--batch-id', type=int, required=True, help='ID of the batch to purge')
        parser.add_argument('--actor', type=str, required=True, help='Username of the superuser executing the purge')
        parser.add_argument('--dry-run', action='store_true', help='Preview deletion counts without performing the purge')
        parser.add_argument('--confirm', action='store_true', help='Skip interactive confirmation prompt')
        parser.add_argument('--production-confirm', action='store_true', help='Separate production confirmation flag')

    def handle(self, *args, **options):
        batch_id = options['batch_id']
        actor_username = options['actor']
        dry_run = options['dry_run']
        confirm = options['confirm']
        production_confirm = options['production_confirm']

        # 1. Superuser/Permission verification
        try:
            actor = User.objects.get(username=actor_username)
        except User.DoesNotExist:
            # Log failed audit event for non-existent user
            AuditEvent.objects.create(
                event_type="PURGE_BATCH_FAILED",
                description=f"Purge batch {batch_id} failed: User '{actor_username}' does not exist.",
                actor="SYSTEM"
            )
            raise CommandError(f"User '{actor_username}' does not exist.")

        if not actor.is_active:
            AuditEvent.objects.create(
                event_type="PURGE_BATCH_FAILED",
                description=f"Purge batch {batch_id} failed: User '{actor_username}' is inactive.",
                actor=actor_username
            )
            raise CommandError(f"User '{actor_username}' is inactive.")

        if not (actor.is_superuser or actor.has_perm('roster.purge_data')):
            AuditEvent.objects.create(
                event_type="PURGE_BATCH_FAILED",
                description=f"Purge batch {batch_id} failed: User '{actor_username}' lacks purge_data permission.",
                actor=actor_username
            )
            raise CommandError(f"User '{actor_username}' is not a superuser (or lacks purge_data permission). Only superusers are authorized to purge batches.")

        # 2. Retrieve batch
        try:
            batch = ImportBatch.objects.get(id=batch_id)
        except ImportBatch.DoesNotExist:
            AuditEvent.objects.create(
                event_type="PURGE_BATCH_FAILED",
                description=f"Purge batch {batch_id} failed: ImportBatch does not exist.",
                actor=actor_username
            )
            raise CommandError(f"ImportBatch with ID {batch_id} does not exist.")

        # 3. Calculate deletion preview
        raw_count = RawContribution.objects.filter(import_batch=batch).count()
        contrib_count = Contribution.objects.filter(raw_contribution__import_batch=batch).count()
        
        affected_clusters = list(ContributionCluster.objects.filter(
            assignments__contribution__raw_contribution__import_batch=batch
        ).distinct())
        
        affected_entities = list(ContributorEntity.objects.filter(
            clusters__in=affected_clusters
        ).distinct())
        
        orphaned_clusters_count = 0
        preserved_clusters_count = 0
        orphaned_entities_count = 0
        preserved_entities_count = 0
        protected_dependencies = []
        
        for cluster in affected_clusters:
            # Check surviving assignments (active or inactive lineage), merge decisions, and match decisions
            remaining_assigns = cluster.assignments.exclude(contribution__raw_contribution__import_batch=batch).count()
            
            from roster.models import MergeDecision, MatchDecision
            has_merge_decisions = MergeDecision.objects.filter(Q(source_cluster=cluster) | Q(target_cluster=cluster)).exists()
            has_match_decisions = MatchDecision.objects.filter(contribution_cluster=cluster).exists()
            
            if remaining_assigns == 0 and not (has_merge_decisions or has_match_decisions):
                orphaned_clusters_count += 1
            else:
                preserved_clusters_count += 1
                if remaining_assigns > 0:
                    protected_dependencies.append(f"Cluster '{cluster.normalized_name}' has {remaining_assigns} surviving contributions from other batches.")
                if has_merge_decisions:
                    protected_dependencies.append(f"Cluster '{cluster.normalized_name}' has active MergeDecisions.")
                    
        for entity in affected_entities:
            # Entity is orphaned if all its clusters are orphaned and it has no other dependencies
            total_clusters = entity.clusters.count()
            purging_clusters_for_entity = 0
            for c in entity.clusters.all():
                rem = c.assignments.exclude(contribution__raw_contribution__import_batch=batch).count()
                has_merges = MergeDecision.objects.filter(Q(source_cluster=c) | Q(target_cluster=c)).exists()
                has_matches = MatchDecision.objects.filter(contribution_cluster=c).exists()
                if rem == 0 and not (has_merges or has_matches):
                    purging_clusters_for_entity += 1
                    
            has_manual_overrides = MembershipAssessment.objects.filter(contributor_entity=entity, manual_override=True).exists()
            if total_clusters == purging_clusters_for_entity and not (has_manual_overrides or entity.is_verified):
                orphaned_entities_count += 1
            else:
                preserved_entities_count += 1

        self.stdout.write(self.style.WARNING("=== DELETION PREVIEW ==="))
        self.stdout.write(f"Batch ID: {batch.id}")
        self.stdout.write(f"Filename: {batch.file_name}")
        self.stdout.write(f"Raw records proposed for deletion: {raw_count}")
        self.stdout.write(f"Contributions proposed for deletion: {contrib_count}")
        self.stdout.write(f"Orphaned clusters proposed for deletion: {orphaned_clusters_count}")
        self.stdout.write(f"Orphaned entities proposed for deletion: {orphaned_entities_count}")
        self.stdout.write(f"Shared clusters to be preserved: {preserved_clusters_count}")
        self.stdout.write(f"Shared entities to be preserved: {preserved_entities_count}")
        self.stdout.write(f"Audit records that will remain: {AuditEvent.objects.count()}")
        if protected_dependencies:
            self.stdout.write(f"Protected dependencies: {', '.join(protected_dependencies[:3])}...")
        else:
            self.stdout.write("Protected dependencies: None")
        self.stdout.write(self.style.WARNING("========================"))

        if dry_run:
            # Policy: Dry runs record a persistent AuditEvent log tracking the dry-run simulation
            AuditEvent.objects.create(
                event_type="PURGE_PREVIEW",
                description=f"Dry-run purge preview calculated for batch {batch_id} ('{batch.file_name}') by user '{actor_username}'.",
                actor=actor_username
            )
            self.stdout.write(self.style.SUCCESS("Dry-run mode active. No changes were committed to the database."))
            return

        # 4. Confirmation checks
        if not confirm:
            if not production_confirm:
                raise CommandError("Refusal to proceed: production confirmation (--production-confirm) is incomplete.")
                
            self.stdout.write(self.style.NOTICE("This action is permanent and CANNOT be undone."))
            confirm_val = input(f"Type the batch filename '{batch.file_name}' exactly to confirm deletion: ")
            if confirm_val != batch.file_name:
                AuditEvent.objects.create(
                    event_type="PURGE_BATCH_FAILED",
                    description=f"Purge batch {batch_id} failed: Interactive filename confirmation mismatched.",
                    actor=actor_username
                )
                raise CommandError("Purge cancelled: typed filename did not match.")
        else:
            # Non-interactive confirmation checks
            if not production_confirm:
                raise CommandError("Refusal to proceed: production confirmation (--production-confirm) must be provided in non-interactive mode.")

        # 5. Execute purge
        try:
            # Perform deletion inside service transaction
            purge_result = purge_batch(batch_id, actor=actor_username)
            
            # Write audit log OUTSIDE batch deletion constraints
            AuditEvent.objects.create(
                event_type="PURGE_BATCH",
                description=(
                    f"Permanently purged batch {batch_id} ('{purge_result['file_name']}'). "
                    f"Deleted {purge_result['raw_count']} raw rows, {purge_result['deleted_clusters']} clusters, "
                    f"{purge_result['deleted_entities']} entities. Preserved {preserved_clusters_count} shared clusters, "
                    f"{preserved_entities_count} shared entities. Confirmation: Confirm={confirm}, ProdConfirm={production_confirm}."
                ),
                actor=actor_username
            )
        except Exception as e:
            AuditEvent.objects.create(
                event_type="PURGE_BATCH_FAILED",
                description=f"Purge batch {batch_id} failed with error: {type(e).__name__} - {str(e)}.",
                actor=actor_username
            )
            raise CommandError(f"Purge failed: {str(e)}")

        self.stdout.write(self.style.SUCCESS(f"Successfully purged batch {batch_id} and all orphaned entities."))
