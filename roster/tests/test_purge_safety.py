import os
from django.test import TestCase
from django.db import transaction
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth.models import User
from tempfile import NamedTemporaryFile

from roster.models import (
    ImportMappingProfile, ImportBatch, RawContribution, Contribution, 
    ContributorEntity, ContributionCluster, ContributionClusterAssignment, 
    AuditEvent, MergeDecision, MatchDecision, Location, Person
)
from roster.services.importer import import_csv_file, purge_batch

class PurgeSafetyTestCase(TestCase):
    def setUp(self):
        self.profile = ImportMappingProfile.objects.create(
            name="SOS Mapping",
            mapping_rules={
                'NAME OF CONTRIBUTOR': 'Contributor Name',
                'AMOUNT': 'Contribution Amount',
                'TRANSACTION DATE': 'Contribution Date',
                'ZIP': 'Contributor Zip'
            }
        )
        self.superuser = User.objects.create_superuser(username="admin", password="password")

    def create_temp_csv(self, rows_content):
        temp_file = NamedTemporaryFile(delete=False, suffix='.csv', mode='w', encoding='utf-8')
        temp_file.write("Contributor Name,Contribution Amount,Contribution Date,Contributor Zip\n")
        for row in rows_content:
            temp_file.write(f"{row[0]},{row[1]},{row[2]},{row[3]}\n")
        temp_file.close()
        return temp_file.name

    def test_shared_entity_purge_safety(self):
        # 1. Batch A creates entity
        csv_a = self.create_temp_csv([("JOHN M. SMITH", "100.00", "2026-01-01", "90210")])
        batch_a = import_csv_file(csv_a, "batch_a.csv", self.profile.id, "admin")
        os.remove(csv_a)
        
        # Verify entity exists
        entity = ContributorEntity.objects.get(display_name="JOHN M SMITH")
        self.assertEqual(entity.entity_type, 'INDIVIDUAL')
        
        # 2. Batch B adds a contribution to the same entity (corroborated name and ZIP match)
        csv_b = self.create_temp_csv([("JOHN M. SMITH", "150.00", "2026-02-01", "90210")])
        batch_b = import_csv_file(csv_b, "batch_b.csv", self.profile.id, "admin")
        os.remove(csv_b)
        
        # Verify entity still exists with both contributions
        self.assertEqual(Contribution.objects.filter(assignments__contribution_cluster__contributor_entity=entity).count(), 2)
        
        # 3. Purge Batch A
        # Let's call the purge_batch service helper directly
        purge_batch(batch_a.id, actor="admin")
        
        # 4. Verify Batch B's contribution, assignment, cluster, and entity remain valid!
        self.assertFalse(ImportBatch.objects.filter(id=batch_a.id).exists())
        self.assertTrue(ImportBatch.objects.filter(id=batch_b.id).exists())
        
        # Entity john smith still survives because Batch B links to it!
        entity.refresh_from_db()
        self.assertEqual(entity.display_name, "JOHN M SMITH")
        
        # Contribution count from Batch B remains 1
        contribs = Contribution.objects.filter(assignments__contribution_cluster__contributor_entity=entity)
        self.assertEqual(contribs.count(), 1)
        self.assertEqual(contribs.first().amount, 150.00)

    def test_shared_merged_identity_preservation(self):
        # Batch A creates John Moore (94103)
        csv_a = self.create_temp_csv([("JOHN MOORE", "50.00", "2026-01-01", "94103")])
        batch_a = import_csv_file(csv_a, "batch_a.csv", self.profile.id, "admin")
        os.remove(csv_a)
        
        # Batch B creates John Moore (90210)
        csv_b = self.create_temp_csv([("JOHN MOORE", "75.00", "2026-02-01", "90210")])
        batch_b = import_csv_file(csv_b, "batch_b.csv", self.profile.id, "admin")
        os.remove(csv_b)
        
        # Merge them
        entity_a = ContributorEntity.objects.get(clusters__zip_code="94103")
        entity_b = ContributorEntity.objects.get(clusters__zip_code="90210")
        
        from roster.services.resolver import merge_clusters
        merge_clusters(entity_a.clusters.first().id, entity_b.clusters.first().id, actor="admin")
        
        # Purge Batch A
        purge_batch(batch_a.id, actor="admin")
        
        # Verify Batch B's cluster, entity, and the active MergeDecision remain intact
        self.assertTrue(ContributorEntity.objects.filter(id=entity_b.id).exists())
        self.assertTrue(MergeDecision.objects.filter(is_active=True).exists())

    def test_true_orphan_deletion(self):
        # Import Batch A (creates John Orphan)
        csv_a = self.create_temp_csv([("JOHN ORPHAN", "100.00", "2026-01-01", "90210")])
        batch_a = import_csv_file(csv_a, "batch_a.csv", self.profile.id, "admin")
        os.remove(csv_a)
        
        entity = ContributorEntity.objects.get(display_name="JOHN ORPHAN")
        cluster = entity.clusters.first()
        
        # Purge batch A
        purge_batch(batch_a.id, actor="admin")
        
        # Ensure batch, raw row, contribution, cluster, and entity are deleted!
        self.assertFalse(ImportBatch.objects.filter(id=batch_a.id).exists())
        self.assertFalse(Contribution.objects.filter(raw_contribution__import_batch=batch_a).exists())
        self.assertFalse(ContributionCluster.objects.filter(id=cluster.id).exists())
        self.assertFalse(ContributorEntity.objects.filter(id=entity.id).exists())

    def test_audit_logs_survival_and_failed_purges(self):
        # Create dummy batch
        batch = ImportBatch.objects.create(file_name="dummy.csv", file_hash="dummy_hash", file_type="CSV")
        
        # Run purge using CLI command
        call_command('purge_batch', batch_id=batch.id, actor="admin", confirm=True, production_confirm=True)
        
        # Verify successful purge audit event survives batch deletion
        audit_events = AuditEvent.objects.filter(event_type='PURGE_BATCH')
        self.assertTrue(audit_events.exists())
        self.assertIn("dummy.csv", audit_events.first().description)
        
        # Test failed purge logs audit event outside transaction
        with self.assertRaises(CommandError):
            # Attempt to purge nonexistent batch
            call_command('purge_batch', batch_id=99999, actor="admin", confirm=True, production_confirm=True)
            
        # Verify failed audit event exists
        failed_events = AuditEvent.objects.filter(event_type='PURGE_BATCH_FAILED')
        self.assertTrue(failed_events.exists())
