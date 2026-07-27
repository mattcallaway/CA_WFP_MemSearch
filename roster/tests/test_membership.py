from datetime import date
from django.test import TestCase
from django.contrib.auth.models import User
from roster.models import (
    ImportBatch, RawContribution, Contribution, ContributionCluster, 
    ContributorEntity, MembershipRuleVersion, DatasetCoverageMetadata, 
    MembershipAssessment, ProfilePatternAssessment
)
from roster.services.resolver import resolve_and_cluster_contribution, merge_clusters
from roster.services.membership import evaluate_cluster_recurrence, evaluate_membership_for_entity

class MembershipTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testadmin", password="password")
        self.batch = ImportBatch.objects.create(
            file_name="test.csv",
            file_hash="hash123",
            file_type="CSV",
            imported_by="testadmin",
            status='COMPLETED'
        )
        
        # Create rule version
        self.rule = MembershipRuleVersion.objects.create(
            name="CA WFP Rule v1",
            monthly_interval_min=20,
            monthly_interval_max=40,
            active_grace_period=60,
            min_recurring_payments=2,
            allowed_amount_variance=0.00,
            skip_payment_allowed=True,
            effective_date=date(2026, 1, 1),
            created_by="testadmin",
            is_active=True
        )

        # Create dataset coverage metadata (valid continuous coverage through 2026-07-27)
        self.coverage_valid = DatasetCoverageMetadata.objects.create(
            coverage_start_date=date(2026, 1, 1),
            coverage_end_date=date(2026, 7, 27),
            coverage_complete_through=date(2026, 7, 27),
            coverage_status='CONFIRMED_COMPLETE',
            source_obtained_date=date(2026, 7, 27)
        )

    def create_contribution(self, name, amount, date_str, zip_code="90012", txn_id="", employer="ACME"):
        raw = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=RawContribution.objects.count() + 1,
            original_values={"NAME OF CONTRIBUTOR": name, "ZIP": zip_code, "EMPLOYER": employer},
            raw_row_hash=f"hash_{name}_{date_str}_{amount}"
        )
        c = Contribution.objects.create(
            raw_contribution=raw,
            transaction_number=txn_id,
            amount=amount,
            transaction_type='CONTRIBUTION',
            status='ACTIVE',
            transaction_date=date_str,
            employer=employer
        )
        return c

    def test_recurring_monthly_contributor(self):
        # John Doe: 3 transactions, ~30 days intervals
        c1 = self.create_contribution("DOE, JOHN", 10.00, date(2026, 4, 10))
        c2 = self.create_contribution("DOE, JOHN", 10.00, date(2026, 5, 10))
        c3 = self.create_contribution("DOE, JOHN", 10.00, date(2026, 6, 9))
        
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        resolve_and_cluster_contribution(c3)
        
        # Recurrence check
        pattern = evaluate_cluster_recurrence(cluster.id, evaluation_date=date(2026, 6, 9))
        self.assertEqual(pattern, 'POSSIBLE_RECURRING')

    def test_skipped_month_recurring(self):
        # Jane Miller: Feb 10, Apr 12, May 10 (skipped March)
        c1 = self.create_contribution("MILLER, JANE", 15.00, date(2026, 2, 10))
        c2 = self.create_contribution("MILLER, JANE", 15.00, date(2026, 4, 12))
        c3 = self.create_contribution("MILLER, JANE", 15.00, date(2026, 5, 10))
        
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        resolve_and_cluster_contribution(c3)
        
        pattern = evaluate_cluster_recurrence(cluster.id, evaluation_date=date(2026, 5, 10))
        self.assertEqual(pattern, 'POSSIBLE_RECURRING') # Skipped month is allowed

    def test_unverified_cluster_excludes_membership(self):
        # Low confidence clusters remain unverified and cannot become active members
        c1 = self.create_contribution("DAVIS, WILLIAM", 50.00, date(2026, 6, 1), employer="ACME")
        c2 = self.create_contribution("DAVIS, WILLIAM", 50.00, date(2026, 7, 1), employer="ACME")
        
        cluster1 = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        
        self.assertEqual(cluster1.confidence_level, 'LOW')
        self.assertFalse(cluster1.contributor_entity.is_verified)
        
        # Calculate cluster recurrence
        pattern = evaluate_cluster_recurrence(cluster1.id, evaluation_date=date(2026, 7, 1))
        self.assertEqual(pattern, 'POSSIBLE_RECURRING')
        
        # Calculate entity membership
        status = evaluate_membership_for_entity(cluster1.contributor_entity_id, evaluation_date=date(2026, 7, 1))
        # Status remains UNKNOWN/PROVISIONAL because it is unverified
        self.assertEqual(status, 'UNKNOWN')

    def test_voter_zip_move_timeline_aggregation(self):
        # One verified person moved ZIP codes: Name + ZIP A, Name + ZIP B
        # Let's verify that merging them under the same entity aggregates their timeline
        c1 = self.create_contribution("GARCIA, MARIA", 30.00, date(2026, 4, 1), zip_code="90012")
        c2 = self.create_contribution("GARCIA, MARIA", 30.00, date(2026, 5, 1), zip_code="90210")
        
        cluster1 = resolve_and_cluster_contribution(c1)
        cluster2 = resolve_and_cluster_contribution(c2)
        
        self.assertNotEqual(cluster1.id, cluster2.id)
        
        # Verify entities
        entity1 = cluster1.contributor_entity
        entity2 = cluster2.contributor_entity
        
        entity1.is_verified = True
        entity1.save()
        entity2.is_verified = True
        entity2.save()
        
        # Merge cluster2 into cluster1 (making them share entity1)
        merge_clusters(cluster2.id, cluster1.id, actor="testadmin")
        
        # Force entity link on cluster2
        cluster2.contributor_entity = entity1
        cluster2.save()
        
        # Evaluate aggregated membership
        status = evaluate_membership_for_entity(entity1.id, evaluation_date=date(2026, 5, 1))
        # Now John Doe has contributions on both clusters aggregated: April 1 and May 1 -> Active recurring!
        self.assertEqual(status, 'ACTIVE')

    def test_stale_dataset_suppresses_lapsed_status(self):
        # Charlie Brown: payments in Jan/Feb/Mar 2025.
        # Roster complete date is mid 2026.
        c1 = self.create_contribution("BROWN, CHARLIE", 10.00, date(2025, 1, 10))
        c2 = self.create_contribution("BROWN, CHARLIE", 10.00, date(2025, 2, 10))
        c3 = self.create_contribution("BROWN, CHARLIE", 10.00, date(2025, 3, 10))
        
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        resolve_and_cluster_contribution(c3)
        
        entity = cluster.contributor_entity
        entity.is_verified = True
        entity.save()
        
        # Case A: Dataset is stale (coverage ends 2026-07-27, but we query 60 days later with a stale indicator)
        # We simulate this by changing coverage status to PARTIAL / STALE
        self.coverage_valid.coverage_status = 'STALE'
        self.coverage_valid.save()
        
        status = evaluate_membership_for_entity(entity.id, evaluation_date=date(2026, 7, 27))
        # Should be PREVIOUSLY_RECURRING (stale) or DATASET_TOO_STALE rather than Lapsed
        self.assertEqual(status, 'PREVIOUSLY_RECURRING')
        
        # Case B: Dataset is fresh, and he didn't contribute -> Lapsed
        self.coverage_valid.coverage_status = 'CONFIRMED_COMPLETE'
        self.coverage_valid.save()
        
        status = evaluate_membership_for_entity(entity.id, evaluation_date=date(2026, 7, 27))
        self.assertEqual(status, 'LAPSED')
