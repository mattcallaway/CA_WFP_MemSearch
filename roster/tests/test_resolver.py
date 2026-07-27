from django.test import TestCase
from django.contrib.auth.models import User
from roster.models import (
    ContributorEntity, Person, Organization, ContributionCluster, 
    Contribution, ContributionClusterAssignment, MergeDecision, ImportBatch, RawContribution
)
from roster.services.resolver import (
    normalize_name, detect_entity_type, resolve_and_cluster_contribution, 
    merge_clusters, split_cluster
)

class ResolverTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testadmin", password="password")
        self.batch = ImportBatch.objects.create(
            file_name="test.csv",
            file_hash="hash123",
            file_type="CSV",
            imported_by="testadmin",
            status='COMPLETED'
        )

    def test_name_normalization(self):
        # Last name first
        parsed = normalize_name("DOE, JOHN A")
        self.assertEqual(parsed['normalized_full_name'], "JOHN A DOE")
        self.assertEqual(parsed['first_name'], "JOHN")
        self.assertEqual(parsed['middle_name'], "A")
        self.assertEqual(parsed['last_name'], "DOE")
        
        # Prefixes and Suffixes
        parsed = normalize_name("DR. VANCE, ROBERT SR")
        self.assertEqual(parsed['normalized_full_name'], "ROBERT VANCE")
        self.assertEqual(parsed['first_name'], "ROBERT")
        self.assertEqual(parsed['last_name'], "VANCE")
        self.assertEqual(parsed['suffix'], "SR")
        
        # Org preservation
        parsed = normalize_name("TEAMSTERS LOCAL 396")
        self.assertEqual(parsed['normalized_full_name'], "TEAMSTERS LOCAL 396")
        self.assertEqual(parsed['first_name'], "")

    def test_detect_entity_type(self):
        self.assertEqual(detect_entity_type("TEAMSTERS LOCAL 396"), "ORGANIZATION")
        self.assertEqual(detect_entity_type("CALIFORNIA CLEAN AIR PAC"), "ORGANIZATION")
        self.assertEqual(detect_entity_type("SMITH, JANE & JOHN"), "JOINT")
        self.assertEqual(detect_entity_type("JOHN DOE"), "INDIVIDUAL")

    def test_insufficient_corroboration_segregation(self):
        # Two contributions with same name and zip but all identifying fields blank must not auto-group
        raw1 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=1,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012"},
            raw_row_hash="hash1"
        )
        c1 = Contribution.objects.create(
            raw_contribution=raw1,
            amount=50.00,
            transaction_date="2026-06-01",
            status='ACTIVE'
        )
        
        raw2 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=2,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012"},
            raw_row_hash="hash2"
        )
        c2 = Contribution.objects.create(
            raw_contribution=raw2,
            amount=100.00,
            transaction_date="2026-06-02",
            status='ACTIVE'
        )
        
        cluster1 = resolve_and_cluster_contribution(c1)
        cluster2 = resolve_and_cluster_contribution(c2)
        
        # They should form separate clusters because there is no other corroboration (employer/occupation/middle name are all empty)
        # "Same normalized name and ZIP with blank corroborating fields remains an unresolved provisional cluster."
        self.assertNotEqual(cluster1.id, cluster2.id)
        self.assertEqual(cluster1.confidence_level, 'LOW')
        self.assertEqual(cluster2.confidence_level, 'LOW')
        self.assertFalse(cluster1.contributor_entity.is_verified)

    def test_corroborated_auto_grouping(self):
        # Same name + ZIP + matching non-empty employer
        raw1 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=1,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012", "EMPLOYER": "ACME"},
            raw_row_hash="hash1"
        )
        c1 = Contribution.objects.create(
            raw_contribution=raw1,
            amount=50.00,
            transaction_date="2026-06-01",
            employer="ACME",
            status='ACTIVE'
        )
        
        raw2 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=2,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012", "EMPLOYER": "ACME"},
            raw_row_hash="hash2"
        )
        c2 = Contribution.objects.create(
            raw_contribution=raw2,
            amount=100.00,
            transaction_date="2026-06-02",
            employer="ACME",
            status='ACTIVE'
        )
        
        cluster1 = resolve_and_cluster_contribution(c1)
        cluster2 = resolve_and_cluster_contribution(c2)
        
        # They should match the same cluster due to matching employer
        self.assertEqual(cluster1.id, cluster2.id)
        cluster1.refresh_from_db()
        self.assertEqual(cluster1.confidence_level, 'MEDIUM') # Upgraded from LOW due to corroboration

    def test_reversible_merges_and_splits(self):
        raw1 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=1,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012", "EMPLOYER": "ACME"},
            raw_row_hash="hash1"
        )
        c1 = Contribution.objects.create(
            raw_contribution=raw1,
            amount=50.00,
            transaction_date="2026-06-01",
            employer="ACME",
            status='ACTIVE'
        )
        
        raw2 = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=2,
            original_values={"NAME OF CONTRIBUTOR": "DAVIS, WILLIAM", "ZIP": "90012", "EMPLOYER": "STARK"},
            raw_row_hash="hash2"
        )
        c2 = Contribution.objects.create(
            raw_contribution=raw2,
            amount=100.00,
            transaction_date="2026-06-02",
            employer="STARK",
            status='ACTIVE'
        )
        
        cluster1 = resolve_and_cluster_contribution(c1)
        cluster2 = resolve_and_cluster_contribution(c2)
        
        self.assertNotEqual(cluster1.id, cluster2.id)
        
        # Record original assignments
        assign1 = ContributionClusterAssignment.objects.get(contribution=c1, is_active=True)
        assign2 = ContributionClusterAssignment.objects.get(contribution=c2, is_active=True)
        
        self.assertEqual(assign1.contribution_cluster, cluster1)
        self.assertEqual(assign2.contribution_cluster, cluster2)
        
        # Merge cluster1 into cluster2
        merge_dec = merge_clusters(cluster1.id, cluster2.id, actor="testadmin")
        
        # Verify merged state
        new_assign1 = ContributionClusterAssignment.objects.get(contribution=c1, is_active=True)
        new_assign2 = ContributionClusterAssignment.objects.get(contribution=c2, is_active=True)
        
        self.assertEqual(new_assign1.contribution_cluster, cluster2)
        self.assertEqual(new_assign2.contribution_cluster, cluster2)
        
        # Split (Undo merge)
        split_cluster(merge_dec.id, actor="testadmin")
        
        # Verify split state returns to original assignments
        restore_assign1 = ContributionClusterAssignment.objects.get(contribution=c1, is_active=True)
        restore_assign2 = ContributionClusterAssignment.objects.get(contribution=c2, is_active=True)
        
        self.assertEqual(restore_assign1.contribution_cluster, cluster1)
        self.assertEqual(restore_assign2.contribution_cluster, cluster2)
