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
        self.user = User.objects.create_superuser(username="testadmin", password="password")
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
        # Status receives PROVISIONAL (provisional recurrence pattern), NOT authoritative ACTIVE
        self.assertEqual(status, 'PROVISIONAL')
        ass = MembershipAssessment.objects.get(contributor_entity_id=cluster1.contributor_entity_id, is_current=True)
        self.assertEqual(ass.membership_authority, 'PROVISIONAL')
        self.assertFalse(ass.identity_verified_at_assessment)

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
        
        entity1.verification_status = 'VERIFIED'
        entity1.verification_method = 'ADMIN_REVIEW'
        entity1.verified_at = date(2026, 1, 1)
        entity1.verified_by = 'testadmin'
        entity1.save()
        entity2.verification_status = 'VERIFIED'
        entity2.verification_method = 'ADMIN_REVIEW'
        entity2.verified_at = date(2026, 1, 1)
        entity2.verified_by = 'testadmin'
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
        entity.verification_status = 'VERIFIED'
        entity.verification_method = 'ADMIN_REVIEW'
        entity.verified_at = date(2026, 1, 1)
        entity.verified_by = 'testadmin'
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

    def test_repeated_transactions_do_not_verify_identity(self):
        c1 = self.create_contribution("ALVAREZ, CARLOS", 25.00, date(2026, 1, 10))
        c2 = self.create_contribution("ALVAREZ, CARLOS", 25.00, date(2026, 2, 10))
        c3 = self.create_contribution("ALVAREZ, CARLOS", 25.00, date(2026, 3, 10))
        
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        resolve_and_cluster_contribution(c3)
        
        entity = ContributorEntity.objects.get(id=cluster.contributor_entity_id)
        self.assertFalse(entity.is_verified)
        self.assertEqual(entity.verification_status, 'UNVERIFIED')
        self.assertEqual(entity.verification_method, 'NONE')

    def test_membership_evaluator_is_read_only_with_respect_to_verification(self):
        c1 = self.create_contribution("TEST, USER", 20.00, date(2026, 4, 1))
        c2 = self.create_contribution("TEST, USER", 20.00, date(2026, 5, 1))
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        
        entity = cluster.contributor_entity
        self.assertFalse(entity.is_verified)
        
        evaluate_membership_for_entity(entity.id)
        
        entity.refresh_from_db()
        self.assertFalse(entity.is_verified)
        self.assertEqual(entity.verification_status, 'UNVERIFIED')

    def test_unverified_entity_gets_provisional_status_and_authority(self):
        c1 = self.create_contribution("PROVISIONAL, DONOR", 30.00, date(2026, 5, 1))
        c2 = self.create_contribution("PROVISIONAL, DONOR", 30.00, date(2026, 6, 1))
        c3 = self.create_contribution("PROVISIONAL, DONOR", 30.00, date(2026, 7, 1))
        
        cluster = resolve_and_cluster_contribution(c1)
        resolve_and_cluster_contribution(c2)
        resolve_and_cluster_contribution(c3)
        
        entity = cluster.contributor_entity
        evaluate_membership_for_entity(entity.id, evaluation_date=date(2026, 7, 1))
        
        assessment = MembershipAssessment.objects.get(contributor_entity=entity, is_current=True)
        self.assertEqual(assessment.calculated_status, 'PROVISIONAL')
        self.assertEqual(assessment.membership_authority, 'PROVISIONAL')
        self.assertEqual(assessment.recurrence_pattern_status, 'RECURRING_PATTERN')
        self.assertFalse(assessment.identity_verified_at_assessment)

    def test_check_is_verified_sync_constraint_fails_on_inconsistent_update(self):
        from django.db import IntegrityError
        entity = ContributorEntity.objects.create(
            display_name="CONSTRAINT_TEST",
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False
        )
        with self.assertRaises(IntegrityError):
            ContributorEntity.objects.filter(id=entity.id).update(
                is_verified=True,
                verification_status="UNVERIFIED"
            )

    def test_centralized_identity_services_and_method_validations(self):
        from roster.services.identity import verify_contributor_identity, unverify_contributor_identity
        from roster.models import AuditEvent
        from django.core.exceptions import ValidationError, PermissionDenied

        entity = ContributorEntity.objects.create(
            display_name="SERVICE_TEST",
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False
        )

        # ADMIN_REVIEW requires explanation
        with self.assertRaises(ValidationError):
            verify_contributor_identity(
                entity=entity,
                method="ADMIN_REVIEW",
                actor="testadmin",
                explanation=""
            )

        # ADMIN_REVIEW success
        verify_contributor_identity(
            entity=entity,
            method="ADMIN_REVIEW",
            actor="testadmin",
            explanation="Validated via public record"
        )
        entity.refresh_from_db()
        self.assertTrue(entity.is_verified)
        self.assertEqual(entity.verification_status, "VERIFIED")
        self.assertEqual(entity.verification_method, "ADMIN_REVIEW")

        # Unverify entity
        unverify_contributor_identity(
            entity=entity,
            actor="testadmin",
            reason="Unverified for testing"
        )
        entity.refresh_from_db()
        self.assertFalse(entity.is_verified)
        self.assertEqual(entity.verification_status, "UNVERIFIED")
        self.assertEqual(entity.verification_method, "NONE")
