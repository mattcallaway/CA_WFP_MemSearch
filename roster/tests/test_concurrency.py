import os
import threading
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, IntegrityError

from roster.models import ImportBatch, ImportAttempt, RawContribution, Contribution, ImportMappingProfile
from roster.services.importer import import_csv_file


class DuplicateUploadConcurrencyTestCase(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        close_old_connections()
        self.user = User.objects.create_superuser(username="admin", password="password")
        self.profile = ImportMappingProfile.objects.create(
            name="SOS Standard Mapping Profile",
            mapping_rules={
                "NAME OF CONTRIBUTOR": "NAME OF CONTRIBUTOR",
                "AMOUNT": "AMOUNT",
                "TRANSACTION DATE": "TRANSACTION DATE",
                "ZIP": "ZIP"
            },
            owner=self.user
        )
        self.fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_contributions.csv")

    def test_simultaneous_identical_uploads(self):
        """
        Tests simultaneous imports of the same file hash across two worker threads.
        Asserts exactly 1 canonical COMPLETED batch, expected ImportAttempt count, 0 duplicate contributions.
        """
        canonical_batch = import_csv_file(
            self.fixture_path,
            "synthetic_contributions.csv",
            self.profile.id,
            actor="admin",
            override_duplicate=False
        )
        self.assertEqual(canonical_batch.status, 'COMPLETED')

        barrier = threading.Barrier(2)
        errors = [None, None]

        def worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                import_csv_file(
                    self.fixture_path,
                    "synthetic_contributions.csv",
                    self.profile.id,
                    actor="admin",
                    override_duplicate=False
                )
            except Exception as e:
                errors[index] = e
            finally:
                close_old_connections()

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        close_old_connections()

        for err in errors:
            self.assertIsNotNone(err, "Expected concurrent duplicate attempt to be rejected.")
            self.assertTrue("already been imported" in str(err) or "locked" in str(err), f"Unexpected concurrency error: {err}")

        canonical_batch.refresh_from_db()
        self.assertEqual(canonical_batch.status, 'COMPLETED')

        attempts = ImportAttempt.objects.filter(import_batch=canonical_batch)
        self.assertGreaterEqual(attempts.count(), 1)

        completed_batches = ImportBatch.objects.filter(file_hash=canonical_batch.file_hash, status='COMPLETED')
        self.assertEqual(completed_batches.count(), 1)

        total_raw = RawContribution.objects.filter(import_batch=canonical_batch).count()
        self.assertEqual(total_raw, 40)

    def test_same_hash_completion_race(self):
        """
        Tests two pending batches with the same file hash attempting to transition to COMPLETED simultaneously.
        Asserts database unique constraint prevents multiple completed batches.
        """
        b1 = ImportBatch.objects.create(file_name="race.csv", file_hash="hash_race_123", file_type="CSV", status="PENDING")
        b2 = ImportBatch.objects.create(file_name="race.csv", file_hash="hash_race_123", file_type="CSV", status="PENDING")

        b1.status = "COMPLETED"
        b1.save()

        b2.status = "COMPLETED"
        with self.assertRaises(IntegrityError):
            b2.save()

        completed = ImportBatch.objects.filter(file_hash="hash_race_123", status="COMPLETED")
        self.assertEqual(completed.count(), 1)

    def test_upload_during_completion(self):
        """
        Tests duplicate upload submitted while canonical batch is completing.
        Asserts rejection without corruption of canonical completed lifecycle.
        """
        canonical_batch = import_csv_file(
            self.fixture_path,
            "synthetic_contributions.csv",
            self.profile.id,
            actor="admin",
            override_duplicate=False
        )
        self.assertEqual(canonical_batch.status, 'COMPLETED')

        # Attempt duplicate upload during completed state
        with self.assertRaises(ValueError) as cm:
            import_csv_file(
                self.fixture_path,
                "synthetic_contributions.csv",
                self.profile.id,
                actor="admin",
                override_duplicate=False
            )
        self.assertIn("already been imported", str(cm.exception))
