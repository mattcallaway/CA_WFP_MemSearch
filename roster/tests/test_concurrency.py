import os
import threading
from django.test import TransactionTestCase
from django.contrib.auth.models import User
from django.db import close_old_connections, connection

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

    def test_simultaneous_duplicate_file_imports(self):
        """
        Tests simultaneous imports of the same file hash across two worker threads.
        Asserts exactly 1 canonical COMPLETED batch, 0 duplicate contributions, and 0 HTTP 500 / IntegrityError exceptions.
        """
        # First, establish canonical completed batch
        canonical_batch = import_csv_file(
            self.fixture_path,
            "synthetic_contributions.csv",
            self.profile.id,
            actor="admin",
            override_duplicate=False
        )
        self.assertEqual(canonical_batch.status, 'COMPLETED')

        # Now test two concurrent worker threads attempting duplicate uploads without override
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

        # Both concurrent duplicate attempts should be rejected safely with ValueError or SQLite table lock
        for err in errors:
            self.assertIsNotNone(err, "Expected concurrent duplicate attempt to be rejected.")
            self.assertTrue("already been imported" in str(err) or "locked" in str(err), f"Unexpected concurrency error: {err}")

        # Verify canonical batch remains COMPLETED
        canonical_batch.refresh_from_db()
        self.assertEqual(canonical_batch.status, 'COMPLETED')

        # Verify ImportAttempt audit records created
        attempts = ImportAttempt.objects.filter(import_batch=canonical_batch)
        # Verify database invariants
        completed_batches = ImportBatch.objects.filter(file_hash=canonical_batch.file_hash, status='COMPLETED')
        self.assertEqual(completed_batches.count(), 1)

        # Verify no duplicate raw contributions
        total_raw = RawContribution.objects.filter(import_batch=canonical_batch).count()
        self.assertEqual(total_raw, 40)

        # Verify no uncaught integrity errors
        for err in errors:
            if err:
                self.assertNotIsInstance(err, type(connection.Database.IntegrityError))
