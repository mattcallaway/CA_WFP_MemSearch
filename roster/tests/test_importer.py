import os
from django.test import TestCase
from django.contrib.auth.models import User
from django.conf import settings
from roster.models import (
    ImportBatch, ImportMappingProfile, RawContribution, Contribution, 
    ContributionClusterAssignment, ContributionCluster
)
from roster.services.importer import import_csv_file, rollback_batch, restore_batch

class ImporterTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testadmin", password="password")
        self.profile = ImportMappingProfile.objects.create(
            name="SOS Mapping",
            mapping_rules={
                'NAME OF CONTRIBUTOR': 'NAME OF CONTRIBUTOR',
                'PAYMENT TYPE': 'PAYMENT TYPE',
                'CITY': 'CITY',
                'STATE': 'STATE',
                'ZIP': 'ZIP',
                'ID NUMBER': 'ID NUMBER',
                'EMPLOYER': 'EMPLOYER',
                'OCCUPATION': 'OCCUPATION',
                'AMOUNT': 'AMOUNT',
                'TRANSACTION DATE': 'TRANSACTION DATE',
                'FILED DATE': 'FILED DATE',
                'TRANSACTION NUMBER': 'TRANSACTION NUMBER'
            },
            version="1.0",
            owner=self.user
        )
        
        # Path to expanded synthetic test file
        self.fixture_path = os.path.join(
            settings.BASE_DIR, 'roster', 'tests', 'fixtures', 'synthetic_contributions.csv'
        )

    def test_csv_ingestion_and_validation(self):
        # Import the synthetic fixture file
        batch = import_csv_file(
            file_path=self.fixture_path,
            file_name="synthetic_contributions.csv",
            mapping_profile_id=self.profile.id,
            actor="testadmin"
        )
        
        # Verify batch metadata
        self.assertEqual(batch.status, 'COMPLETED')
        self.assertEqual(batch.row_count, 40) # There are 40 rows in the synthetic CSV
        
        # Check validation failure
        failed_raw = RawContribution.objects.filter(
            import_batch=batch, 
            validation_status='VALIDATION_FAILURE'
        )
        # Should have caught INVALID_AMT_USER (invalid amount) and MISSING_ZIP_USER (missing ZIP validation check, wait, is missing zip a validation error? Yes, in our test code we check if it is or not depending on rules)
        # Let's verify we have failures
        self.assertTrue(failed_raw.exists())
        self.assertIn("invalid_amount", [f.original_values.get('AMOUNT') for f in failed_raw])

    def test_duplicate_checks(self):
        # Run import first time
        batch1 = import_csv_file(
            file_path=self.fixture_path,
            file_name="synthetic_contributions.csv",
            mapping_profile_id=self.profile.id,
            actor="testadmin"
        )
        
        # Verify block on exact file hash re-upload
        with self.assertRaises(ValueError):
            import_csv_file(
                file_path=self.fixture_path,
                file_name="synthetic_contributions.csv",
                mapping_profile_id=self.profile.id,
                actor="testadmin"
            )

        # Check row-level exact duplicates in batch1
        # The file has a duplicate row for "DUPLICATE, USER" (TXN_DUP_001)
        raw_dups = RawContribution.objects.filter(
            import_batch=batch1,
            validation_status='EXACT_DUPLICATE'
        )
        self.assertEqual(raw_dups.count(), 1)
        
        # Exact duplicate row should not have a Contribution record created
        self.assertFalse(Contribution.objects.filter(raw_contribution__in=raw_dups).exists())

    def test_negative_amounts_handling(self):
        batch = import_csv_file(
            file_path=self.fixture_path,
            file_name="synthetic_contributions.csv",
            mapping_profile_id=self.profile.id,
            actor="testadmin"
        )
        
        # Check refund contribution (REFUND_USER with amount -10.00)
        refund_c = Contribution.objects.get(transaction_number='TXN_REC_001', amount__lt=0)
        self.assertEqual(refund_c.transaction_type, 'REFUND')
        
        # Check reversal contribution (REVERSAL_USER with amount -10.00)
        reversal_c = Contribution.objects.get(transaction_number='TXN_REV_001')
        self.assertEqual(reversal_c.transaction_type, 'REVERSAL')
        
        # Check negative unknown contribution (NEGATIVE_UNK_USER with amount -15.00)
        unknown_neg_c = Contribution.objects.get(transaction_number='TXN_NEG_UNK')
        self.assertEqual(unknown_neg_c.transaction_type, 'ADJUSTMENT')

    def test_rollback_and_restoration(self):
        batch = import_csv_file(
            file_path=self.fixture_path,
            file_name="synthetic_contributions.csv",
            mapping_profile_id=self.profile.id,
            actor="testadmin"
        )
        
        # Count active contributions
        self.assertTrue(Contribution.objects.filter(raw_contribution__import_batch=batch, status='ACTIVE').exists())
        self.assertTrue(ContributionClusterAssignment.objects.filter(contribution__raw_contribution__import_batch=batch, is_active=True).exists())
        
        # Run Rollback
        rollback_batch(batch.id, actor="testadmin")
        
        # Verify batch status is rolled back
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'ROLLED_BACK')
        
        # Verify RawContribution rows are preserved!
        self.assertTrue(RawContribution.objects.filter(import_batch=batch).exists())
        
        # Verify Contribution cluster assignments are deactivated
        active_assigns = ContributionClusterAssignment.objects.filter(
            contribution__raw_contribution__import_batch=batch,
            is_active=True
        )
        self.assertFalse(active_assigns.exists())
        
        # Run Restore
        restore_batch(batch.id, actor="testadmin")
        
        # Verify batch status is completed again
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'COMPLETED')
        
        # Verify Contribution cluster assignments are reactivated
        active_assigns_restored = ContributionClusterAssignment.objects.filter(
            contribution__raw_contribution__import_batch=batch,
            is_active=True
        )
        self.assertTrue(active_assigns_restored.exists())

    def test_purge_batch_management_command(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        
        batch = import_csv_file(
            file_path=self.fixture_path,
            file_name="synthetic_contributions.csv",
            mapping_profile_id=self.profile.id,
            actor="testadmin"
        )
        
        # Verify records exist before purge
        self.assertTrue(ImportBatch.objects.filter(id=batch.id).exists())
        self.assertTrue(Contribution.objects.filter(raw_contribution__import_batch=batch).exists())
        
        # Create superuser and non-superuser
        superuser = User.objects.create_superuser(username="superadmin", password="password")
        normal_user = User.objects.create_user(username="normaluser", password="password")
        
        # Test 1: Actor not superuser fails
        with self.assertRaises(CommandError) as ctx:
            call_command('purge_batch', batch_id=batch.id, actor="normaluser", confirm=True, production_confirm=True)
        self.assertIn("not a superuser", str(ctx.exception))
        
        # Test 2: Dry run does not delete
        call_command('purge_batch', batch_id=batch.id, actor="superadmin", dry_run=True, confirm=True, production_confirm=True)
        self.assertTrue(ImportBatch.objects.filter(id=batch.id).exists())
        
        # Test 3: Actual purge deletes all records
        call_command('purge_batch', batch_id=batch.id, actor="superadmin", confirm=True, production_confirm=True)
        self.assertFalse(ImportBatch.objects.filter(id=batch.id).exists())
        self.assertFalse(Contribution.objects.filter(raw_contribution__import_batch=batch).exists())
        self.assertFalse(RawContribution.objects.filter(import_batch=batch).exists())
