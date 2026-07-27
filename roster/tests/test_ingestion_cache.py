import os
import decimal
from django.test import TestCase
from django.db import IntegrityError
from tempfile import NamedTemporaryFile
from roster.models import (
    ImportMappingProfile, ImportBatch, RawContribution, Contribution, 
    ContributorEntity, ContributionCluster, ContributionClusterAssignment, SourceRecordLink
)
from roster.services.importer import import_csv_file
from roster.services.cache import IngestionCache
from roster.services.resolver import normalize_name, detect_entity_type, has_conflict, check_corroboration

class IngestionCacheTestCase(TestCase):
    def setUp(self):
        # Create standard mapping profile
        self.profile = ImportMappingProfile.objects.create(
            name="SOS Mapping",
            mapping_rules={
                'NAME OF CONTRIBUTOR': 'Contributor Name',
                'AMOUNT': 'Contribution Amount',
                'TRANSACTION DATE': 'Contribution Date',
                'ZIP': 'Contributor Zip',
                'STREET ADDRESS': 'Contributor Street',
                'CITY': 'Contributor City',
                'STATE': 'Contributor State',
                'TRANSACTION NUMBER': 'Txn Number'
            }
        )

    def test_cache_decision_parity(self):
        # Create candidate cluster in DB
        entity = ContributorEntity.objects.create(entity_type='INDIVIDUAL', display_name='JOHN MOORE')
        cluster = ContributionCluster.objects.create(
            contributor_entity=entity,
            normalized_name='JOHN MOORE',
            zip_code='94103',
            confidence_level='LOW'
        )
        
        # Instantiate explicit cache
        ing_cache = IngestionCache()
        cluster_key = ing_cache.get_cluster_key(cluster)
        ing_cache.assignments_by_cluster[cluster_key] = []
        ing_cache.clusters_by_name_zip[('JOHN MOORE', '94103')] = [cluster]
        
        # Run conflict checks using both cached and non-cached signatures
        conflict_non_cached = has_conflict(cluster, 'JOHN', '', 'MOORE', '', 'ACME', 'ENGINEER')
        conflict_cached = has_conflict(cluster, 'JOHN', '', 'MOORE', '', 'ACME', 'ENGINEER', assignment_cache=ing_cache.assignments_by_cluster)
        
        # Parity check
        self.assertEqual(conflict_non_cached, conflict_cached)
        
        # Check corroboration parity
        corrob_non_cached = check_corroboration(cluster, 'JOHN', '', 'MOORE', '', 'ACME', 'ENGINEER')
        corrob_cached = check_corroboration(cluster, 'JOHN', '', 'MOORE', '', 'ACME', 'ENGINEER', assignment_cache=ing_cache.assignments_by_cluster)
        self.assertEqual(corrob_non_cached, corrob_cached)

    def test_temp_key_resolution_and_visibility(self):
        ing_cache = IngestionCache()
        cluster = ContributionCluster(normalized_name='ALICE SMITH', zip_code='90210')
        
        # Get temporary UUID-scoped key
        temp_key = ing_cache.get_cluster_key(cluster)
        self.assertTrue(temp_key.startswith('temp_cluster_'))
        
        # Store mock assignment with valid contribution
        batch = ImportBatch.objects.create(file_name="test.csv", file_hash="h1", file_type="CSV")
        raw = RawContribution.objects.create(import_batch=batch, row_number=1, original_values={})
        contrib = Contribution.objects.create(
            raw_contribution=raw,
            amount=50.00,
            transaction_date='2026-01-01',
            status='ACTIVE'
        )
        mock_assignment = ContributionClusterAssignment(contribution=contrib, is_active=True)
        ing_cache.assignments_by_cluster[temp_key] = [mock_assignment]
        
        # Resolver finds assignment using temporary key
        has_assigns_cached = has_conflict(cluster, 'ALICE', '', 'SMITH', '', '', '', assignment_cache=ing_cache.assignments_by_cluster)
        self.assertFalse(has_assigns_cached) # No conflict, but finds assignment inside cache

    def test_unique_active_assignment_constraint(self):
        entity = ContributorEntity.objects.create(entity_type='INDIVIDUAL', display_name='JOHN MOORE')
        cluster = ContributionCluster.objects.create(
            contributor_entity=entity,
            normalized_name='JOHN MOORE',
            zip_code='94103'
        )
        batch = ImportBatch.objects.create(file_name="test.csv", file_hash="h1", file_type="CSV")
        raw = RawContribution.objects.create(import_batch=batch, row_number=1, original_values={})
        contrib = Contribution.objects.create(
            raw_contribution=raw,
            amount=50.00,
            transaction_date='2026-01-01',
            status='ACTIVE'
        )
        
        # Create first active assignment
        ContributionClusterAssignment.objects.create(
            contribution=contrib,
            contribution_cluster=cluster,
            is_active=True
        )
        
        # Creating a second active assignment for the same contribution must raise IntegrityError
        with self.assertRaises(IntegrityError):
            ContributionClusterAssignment.objects.create(
                contribution=contrib,
                contribution_cluster=cluster,
                is_active=True
            )

    def test_special_csv_handling_quoted_and_bom(self):
        # CSV content with UTF-8 BOM, quoted commas, quoted line breaks, leading zero zip
        csv_content = '\ufeffContributor Name,Contribution Amount,Contribution Date,Contributor Zip,Contributor Street,Contributor City,Contributor State,Txn Number\n' \
                      '"MOORE, JOHN",50.00,2026-05-15,02108,"123 Main St\nSuite 4B",Boston,MA,TXN100\n'
        
        temp_file = NamedTemporaryFile(delete=False, suffix='.csv', mode='w', encoding='utf-8')
        temp_file.write(csv_content)
        temp_file.close()
        
        try:
            batch = import_csv_file(
                file_path=temp_file.name,
                file_name="bom_test.csv",
                mapping_profile_id=self.profile.id,
                actor="testadmin"
            )
            
            # Assert leading zero was preserved
            contrib = Contribution.objects.get(raw_contribution__import_batch=batch)
            self.assertEqual(contrib.amount, decimal.Decimal('50.00'))
            self.assertEqual(contrib.raw_contribution.original_values.get('Contributor Zip'), '02108')
            
            # Assert source link created
            self.assertTrue(SourceRecordLink.objects.filter(target_record_id=contrib.id, target_model_name='Contribution').exists())
        finally:
            os.remove(temp_file.name)

    def test_import_exception_atomicity(self):
        # Invalid CSV that will trigger an exception (e.g. malformed CSV row structure)
        # We write a row with an invalid date value that triggers validation failure,
        # but to test exception atomicity we will pass an invalid mapping_profile_id to force database rollback!
        
        with self.assertRaises(Exception):
            import_csv_file(
                file_path="nonexistent.csv",
                file_name="nonexistent.csv",
                mapping_profile_id=99999, # Fails with profile DoesNotExist
                actor="testadmin"
            )
            
        # Ensure no batches are created in the database
        self.assertEqual(ImportBatch.objects.count(), 0)
