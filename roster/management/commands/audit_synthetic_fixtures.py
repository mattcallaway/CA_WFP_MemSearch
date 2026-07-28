from django.core.management.base import BaseCommand
from roster.models import ContributorEntity, ContributionClusterAssignment, MembershipAssessment, AuditEvent, ChapterEvaluationResult


class Command(BaseCommand):
    help = "Audits database entities for synthetic benchmark/test fixture provenance, reporting dependency counts and proposed disposition."

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=== SYNTHETIC FIXTURE PROVENANCE AUDIT ==="))

        entities = ContributorEntity.objects.all().order_by('id')
        total = entities.count()
        synthetic_candidates = []

        for e in entities:
            assign_count = ContributionClusterAssignment.objects.filter(contribution_cluster__contributor_entity=e).count()
            ass_count = MembershipAssessment.objects.filter(contributor_entity=e).count()
            chap_res_count = ChapterEvaluationResult.objects.filter(contributor_entity=e).count()
            audit_count = AuditEvent.objects.filter(description__icontains=str(e.id)).count()

            # Candidate rules: 0 assignments AND (name contains Bench/Test OR unassessed)
            is_candidate = (assign_count == 0) and (
                any(kw in e.display_name for kw in ['Bench', 'Test', 'Resolver', 'Atomicity']) or ass_count == 0
            )

            if is_candidate:
                synthetic_candidates.append({
                    'id': e.id,
                    'name': e.display_name,
                    'type': e.entity_type,
                    'assign_count': assign_count,
                    'ass_count': ass_count,
                    'chap_res_count': chap_res_count,
                    'audit_count': audit_count,
                    'disposition': 'SYNTHETIC_BENCHMARK_DISPOSABLE' if assign_count == 0 else 'PRESERVE'
                })

        self.stdout.write(f"Total Database Entities: {total}")
        self.stdout.write(self.style.WARNING(f"Identified {len(synthetic_candidates)} synthetic candidate entities with zero contribution assignments.\n"))

        self.stdout.write(f"{'ID':<8} | {'Display Name':<28} | {'Type':<12} | {'Assigns':<8} | {'Assesses':<8} | {'Disposition':<25}")
        self.stdout.write("-" * 95)
        for c in synthetic_candidates:
            self.stdout.write(
                f"{c['id']:<8} | {c['name']:<28} | {c['type']:<12} | {c['assign_count']:<8} | "
                f"{c['ass_count']:<8} | {c['disposition']:<25}"
            )
        self.stdout.write("-" * 95 + "\n")
