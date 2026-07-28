from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.contrib.auth.models import User
from roster.models import Chapter, ChapterEvaluationRun, ChapterEvaluationResult, ChapterAssignment, AuditEvent, ContributorEntity

class Command(BaseCommand):
    help = "Rebuilds the chapter assignment cache from a specified completed evaluation run."

    def add_arguments(self, parser):
        parser.add_argument('--actor', type=str, required=True, help="Username of the actor performing the rebuild.")
        parser.add_argument('--chapter-id', type=int, help="Chapter ID to rebuild from its current evaluation run.")
        parser.add_argument('--run-id', type=int, help="Specific evaluation run ID to rebuild.")
        parser.add_argument('--entity-id', type=int, help="Optional specific entity ID to rebuild.")
        parser.add_argument('--dry-run', action='store_true', help="Preview the rebuild without committing database changes.")

    def handle(self, *args, **options):
        actor_username = options['actor']
        chapter_id = options['chapter_id']
        run_id = options['run_id']
        entity_id = options['entity_id']
        dry_run = options['dry_run']

        # 1. Permission and actor validation
        try:
            actor = User.objects.get(username=actor_username)
        except User.DoesNotExist:
            raise CommandError(f"Actor username '{actor_username}' does not exist.")

        if not actor.is_active:
            raise CommandError(f"Actor username '{actor_username}' is inactive.")

        if not (actor.is_superuser or actor.has_perm('roster.evaluate_chapter_rules')):
            raise CommandError(f"Actor username '{actor_username}' does not have permission 'roster.evaluate_chapter_rules'.")

        # 2. Resolve run ID
        run = None
        if run_id:
            try:
                run = ChapterEvaluationRun.objects.get(id=run_id)
            except ChapterEvaluationRun.DoesNotExist:
                raise CommandError(f"ChapterEvaluationRun with ID {run_id} does not exist.")
            
            if run.status != 'COMPLETED':
                raise CommandError(f"Run {run_id} is in status '{run.status}' and cannot be used for rebuilding (must be COMPLETED).")

            if chapter_id and run.chapter_id != chapter_id:
                raise CommandError(f"Specified run {run_id} does not match specified chapter {chapter_id}.")
        elif chapter_id:
            try:
                chapter = Chapter.objects.get(id=chapter_id)
            except Chapter.DoesNotExist:
                raise CommandError(f"Chapter with ID {chapter_id} does not exist.")
            
            run = chapter.current_evaluation_run
            if not run:
                raise CommandError(f"Chapter {chapter_id} has no current evaluation run assigned.")
        else:
            raise CommandError("You must specify either --run-id or --chapter-id.")

        # 3. Resolve entities
        results_qs = ChapterEvaluationResult.objects.filter(evaluation_run=run).order_by('id')
        if entity_id:
            try:
                ContributorEntity.objects.get(id=entity_id)
            except ContributorEntity.DoesNotExist:
                raise CommandError(f"ContributorEntity with ID {entity_id} does not exist.")
            results_qs = results_qs.filter(contributor_entity_id=entity_id)

        total_results = results_qs.count()
        self.stdout.write(f"Rebuilding assignment cache for run {run.id} (chapter: {run.chapter.name}). Total results to check: {total_results}.")

        created_cnt = 0
        updated_cnt = 0
        identical_cnt = 0
        chunk_size = 500

        with transaction.atomic():
            # In a dry-run we lock but do not commit
            for offset in range(0, total_results, chunk_size):
                chunk_results = list(results_qs[offset:offset+chunk_size])
                chunk_entity_ids = [r.contributor_entity_id for r in chunk_results]

                # Preload existing assignments for this run
                existing_assignments = {
                    a.contributor_entity_id: a 
                    for a in ChapterAssignment.objects.filter(evaluation_run=run, contributor_entity_id__in=chunk_entity_ids)
                }

                assignments_to_create = []
                assignments_to_update = []

                for res in chunk_results:
                    # Determine target assignment status
                    if res.result_status in ['INCLUDED_BY_RULE', 'MANUALLY_INCLUDED']:
                        assign_status = 'INCLUDED'
                    elif res.result_status == 'PROVISIONAL_GEOGRAPHIC_MATCH':
                        assign_status = 'PROVISIONALLY_INCLUDED'
                    elif res.result_status in ['EXCLUDED_BY_RULE', 'MANUALLY_EXCLUDED']:
                        assign_status = 'EXCLUDED'
                    elif res.result_status in ['AMBIGUOUS_GEOGRAPHY', 'AMBIGUOUS_LOCATION']:
                        assign_status = 'AMBIGUOUS'
                    elif res.result_status in ['NO_CURRENT_LOCATION', 'NO_CURRENT_RESOLVED_LOCATION']:
                        assign_status = 'UNRESOLVED'
                    elif res.result_status == 'INELIGIBLE_ENTITY_TYPE':
                        assign_status = 'INELIGIBLE'
                    else:
                        assign_status = 'NO_MATCH'

                    existing = existing_assignments.get(res.contributor_entity_id)
                    if existing:
                        if existing.assignment_status == assign_status:
                            identical_cnt += 1
                        else:
                            existing.assignment_status = assign_status
                            existing.evaluation_result = res
                            assignments_to_update.append(existing)
                            updated_cnt += 1
                    else:
                        assignments_to_create.append(ChapterAssignment(
                            chapter=run.chapter,
                            evaluation_run=run,
                            contributor_entity=res.contributor_entity,
                            evaluation_result=res,
                            assignment_status=assign_status
                        ))
                        created_cnt += 1

                if not dry_run:
                    if assignments_to_create:
                        ChapterAssignment.objects.bulk_create(assignments_to_create)
                    if assignments_to_update:
                        ChapterAssignment.objects.bulk_update(assignments_to_update, fields=['assignment_status', 'evaluation_result'])

            # Report results
            self.stdout.write(f"Rebuild completed. Created: {created_cnt}, Updated: {updated_cnt}, Identical: {identical_cnt}.")

            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write("[DRY-RUN] Rolling back transaction. No changes committed.")
            else:
                AuditEvent.objects.create(
                    event_type='ASSIGNMENT_CACHE_REBUILD',
                    description=f"Rebuilt assignment cache for run {run.id}. Created: {created_cnt}, Updated: {updated_cnt}, Identical: {identical_cnt}.",
                    actor=actor_username
                )
                self.stdout.write("Cache rebuild completed successfully.")
