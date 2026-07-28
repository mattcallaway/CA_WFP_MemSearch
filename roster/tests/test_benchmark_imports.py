import os
import csv
import tempfile
from django.test import TestCase
from django.db import connection, reset_queries
from django.contrib.auth.models import User

from roster.models import ImportBatch, RawContribution, Contribution, ImportMappingProfile
from roster.services.importer import import_csv_file


class ImportBenchmarkTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", password="password")
        self.profile = ImportMappingProfile.objects.create(
            name="Benchmark Profile",
            mapping_rules={
                "NAME OF CONTRIBUTOR": "NAME OF CONTRIBUTOR",
                "AMOUNT": "AMOUNT",
                "TRANSACTION DATE": "TRANSACTION DATE",
                "ZIP": "ZIP",
                "TRANSACTION ID": "TRANSACTION ID"
            },
            owner=self.user
        )

    def _generate_synthetic_csv(self, num_rows):
        tmp_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.csv', newline='')
        writer = csv.writer(tmp_file)
        writer.writerow(["NAME OF CONTRIBUTOR", "AMOUNT", "TRANSACTION DATE", "ZIP", "TRANSACTION ID"])
        
        for i in range(num_rows):
            # Mix 80% unique transactions, 10% exact duplicates, 10% amendments/refunds
            if i % 10 == 0:
                # Exact duplicate of row 0
                name = "BENCHMARK, DONOR 0"
                amount = "50.00"
                txn = "TXN_0"
            elif i % 10 == 9:
                # Refund
                name = f"BENCHMARK, DONOR {i}"
                amount = "-25.00"
                txn = f"TXN_REF_{i}"
            else:
                name = f"BENCHMARK, DONOR {i}"
                amount = f"{10 + (i % 100):.2f}"
                txn = f"TXN_{i}"

            writer.writerow([name, amount, "2026-05-15", "90012", txn])
            
        tmp_file.close()
        return tmp_file.name

    def test_query_count_bounds_100_rows(self):
        csv_path = self._generate_synthetic_csv(100)
        try:
            reset_queries()
            batch = import_csv_file(csv_path, "bench_100.csv", self.profile.id, actor="admin")
            query_count = len(connection.queries)
            
            # Assert query count is chunk-bounded (under 50 queries for 100 rows)
            self.assertLessEqual(query_count, 50, f"Query count {query_count} exceeded chunk limit of 50 for 100 rows.")
            self.assertEqual(batch.status, 'COMPLETED')
        finally:
            os.remove(csv_path)

    def test_query_count_bounds_1000_rows(self):
        csv_path = self._generate_synthetic_csv(1000)
        try:
            reset_queries()
            batch = import_csv_file(csv_path, "bench_1000.csv", self.profile.id, actor="admin")
            query_count = len(connection.queries)
            
            # Assert query count is chunk-bounded (under 120 queries for 1000 rows)
            self.assertLessEqual(query_count, 120, f"Query count {query_count} exceeded chunk limit of 120 for 1000 rows.")
            self.assertEqual(batch.status, 'COMPLETED')
        finally:
            os.remove(csv_path)
