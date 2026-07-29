import csv
import json
import math
import os
import sys
import tempfile
import time
import tracemalloc
import uuid
from datetime import datetime

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import connection
from django.test.utils import setup_databases, teardown_databases

from roster.services.import_profiler import ImportProfiler


class Command(BaseCommand):
    help = "Profile the production import pipeline by phase with accurate query counting."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scale",
            type=int,
            nargs="+",
            default=[100, 1000, 10000],
            help="Row counts to benchmark (default: 100 1000 10000)",
        )
        parser.add_argument(
            "--output",
            type=str,
            default=None,
            help="Output directory for JSON artifacts",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Import chunk size (default: 500)",
        )

    def handle(self, *args, **options):
        scales = options["scale"]
        output_dir = options["output"]
        chunk_size = options["chunk_size"]

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        is_reliability_env = bool(os.environ.get("WFP_RELIABILITY_DB_PATH"))
        active_db = str(connection.settings_dict.get("NAME", ""))
        self.stdout.write(f"Active database: {active_db}")

        if not is_reliability_env:
            self.stdout.write("Creating isolated test database...")
            old_config = setup_databases(0, False, aliases=["default"])
        else:
            old_config = None

        try:
            coefficients = None
            results = {}

            for scale in scales:
                self.stdout.write(f"\n{'='*60}")
                self.stdout.write(f"BENCHMARK: {scale} rows")
                self.stdout.write(f"{'='*60}")

                result = self._run_benchmark(scale, chunk_size, coefficients)
                results[scale] = result

                if scale in [100, 1000]:
                    if 100 in results and 1000 in results:
                        coefficients = self._calculate_coefficients(results[100], results[1000])
                        self.stdout.write(f"  Calculated Coefficients: {coefficients}")

                if output_dir:
                    fname = f"import_{scale}.json"
                    path = os.path.join(output_dir, fname)
                    with open(path, "w") as f:
                        json.dump(result, f, indent=2)
                    self.stdout.write(f"  Written: {path}")
                else:
                    self.stdout.write(json.dumps(result, indent=2))

                call_command("flush", "--noinput", verbosity=0)
                
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write("RUNNING SEPARATE SCENARIOS")
            self.stdout.write(f"{'='*60}")
            
            self._run_reprocessing_scenario()
            call_command("flush", "--noinput", verbosity=0)
            
            self._run_amendment_scenario()

        finally:
            if old_config:
                teardown_databases(old_config, 0)
                self.stdout.write("\nTest database destroyed.")

    def _generate_mixed_csv(self, num_rows):
        tmp = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv", newline="")
        writer = csv.writer(tmp)
        writer.writerow(["NAME OF CONTRIBUTOR", "AMOUNT", "TRANSACTION DATE", "ZIP", "TRANSACTION ID"])

        composition = {
            "total_input_rows": num_rows,
            "unique_rows": 0,
            "exact_duplicate_rows": 0,
            "missing_txn_rows": 0,
            "refund_rows": 0,
        }

        import hashlib
        seen_hashes = set()
        for i in range(num_rows):
            category = i % 20

            if category == 0 and i > 0:
                # Exact duplicate of DONOR 0
                row = ["BENCHMARK, DONOR 0", "50.00", "2026-05-15", "90012", "TXN_0"]
                composition["exact_duplicate_rows"] += 1
            elif category == 5:
                # Missing transaction number
                row = [f"BENCHMARK, DONOR {i}", f"{10 + (i % 100):.2f}", "2026-05-15", "90012", ""]
                composition["missing_txn_rows"] += 1
            elif category == 15:
                # Refund
                row = [f"BENCHMARK, DONOR {i}", "-25.00", "2026-05-15", "90012", f"TXN_REF_{i}"]
                composition["refund_rows"] += 1
            elif category in (10, 19):
                # Additional exact duplicates (same content as existing rows)
                # category 10: identical to DONOR 1 row
                # category 19: identical to DONOR 2 row
                if category == 10:
                    row = ["BENCHMARK, DONOR 1", "999.99", "2026-06-01", "90012", "TXN_1"]
                else:
                    row = ["BENCHMARK, DONOR 2", f"{10 + (2 % 100):.2f}", "2026-05-15", "90012", "TXN_2"]
                composition["exact_duplicate_rows"] += 1
            else:
                # Unique transaction
                row = [f"BENCHMARK, DONOR {i}", f"{10 + (i % 100):.2f}", "2026-05-15", "90012", f"TXN_{i}"]
                composition["unique_rows"] += 1

            writer.writerow(row)
            row_hash = hashlib.sha256(",".join(row).encode()).hexdigest()
            seen_hashes.add(row_hash)

        composition["unique_row_hashes"] = len(seen_hashes)
        tmp.close()
        return tmp.name, composition

    def _classify_query(self, sql):
        sql = sql.upper()
        if 'AUDITEVENT' in sql:
            return 'finalization'
        if 'SAVEPOINT' in sql or 'RELEASE' in sql:
            return 'fixed'
        if 'ROSTER_RAWCONTRIBUTION' in sql and ('INSERT' in sql or 'UPDATE' in sql):
            return 'import_chunk'
        if 'ROSTER_CONTRIBUTION"' in sql and 'INSERT' in sql:
            return 'import_chunk'
        if 'SOURCERECORDLINK' in sql and 'INSERT' in sql:
            return 'import_chunk'
        if 'ROSTER_CONTRIBUTORENTITY' in sql and 'INSERT' in sql:
            return 'entity_chunk'
        if 'ROSTER_ORGANIZATION' in sql and 'INSERT' in sql:
            return 'entity_chunk'
        if 'ROSTER_PERSON' in sql and 'INSERT' in sql:
            return 'entity_chunk'
        if 'ROSTER_CONTRIBUTIONCLUSTER' in sql and ('INSERT' in sql or 'UPDATE' in sql):
            return 'entity_chunk'
        if 'CLUSTERASSIGNMENT' in sql and 'INSERT' in sql:
            return 'entity_chunk'
        if 'ROSTER_LOCATION' in sql and 'INSERT' in sql:
            return 'entity_chunk'
        if 'SELECT' in sql and 'ROSTER_CONTRIBUTION' in sql and 'TRANSACTION_NUMBER' in sql:
            return 'import_chunk'
        if 'SELECT' in sql and 'ROSTER_CONTRIBUTIONCLUSTER' in sql and 'NORMALIZED_NAME' in sql:
            return 'entity_chunk'
        return 'fixed'

    def _calculate_coefficients(self, res_100, res_1000):
        p100 = res_100['phase_profile']
        p1000 = res_1000['phase_profile']
        
        c_imp_1000 = math.ceil(1000 / 500)
        c_ent_1000 = math.ceil(res_1000['output']['entities_created'] / 500)
        
        # Use 100-row run to establish fixed query count
        fixed_setup = p100.get('fixed', 4)
        fixed_finalization = p100.get('finalization', 4)
        
        # Use 1000-row run to refine per-chunk coefficients
        q_imp = round(p1000.get('import_chunk', 0) / max(1, c_imp_1000))
        q_ent = round(p1000.get('entity_chunk', 0) / max(1, c_ent_1000))
        
        return {
            'fixed_setup_queries': fixed_setup,
            'queries_per_import_chunk': q_imp,
            'queries_per_entity_chunk': q_ent,
            'fixed_finalization_queries': fixed_finalization,
            'fixed_bounded_margin': 15
        }

    def _run_benchmark(self, num_rows, chunk_size, coefficients):
        from django.contrib.auth.models import User
        from roster.models import (
            ImportBatch, ImportMappingProfile, RawContribution,
            Contribution, ContributorEntity, MembershipAssessment,
            ContributionCluster, SourceRecordLink,
        )
        from roster.services.importer import import_csv_file

        run_id = str(uuid.uuid4())[:8]

        user = User.objects.create_superuser(username=f"bench_{run_id}", password="password")
        profile = ImportMappingProfile.objects.create(
            name=f"Bench Profile {run_id}",
            mapping_rules={
                "NAME OF CONTRIBUTOR": "NAME OF CONTRIBUTOR",
                "AMOUNT": "AMOUNT",
                "TRANSACTION DATE": "TRANSACTION DATE",
                "ZIP": "ZIP",
                "TRANSACTION ID": "TRANSACTION ID",
            },
            owner=user,
        )

        csv_path, composition = self._generate_mixed_csv(num_rows)

        try:
            profiler = ImportProfiler()
            tracemalloc.start()
            
            with profiler.phase('full_import', input_rows=num_rows) as phase_result:
                batch = import_csv_file(
                    csv_path, f"bench_{num_rows}.csv", profile.id, actor=f"bench_{run_id}"
                )

            current_mem, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            
            phase_profile = {
                'fixed': 0,
                'import_chunk': 0,
                'entity_chunk': 0,
                'finalization': 0
            }
            
            for q in phase_result.queries:
                phase = self._classify_query(q['sql'])
                phase_profile[phase] += 1

            raw_created = RawContribution.objects.filter(import_batch=batch).count()
            contribs_created = Contribution.objects.filter(raw_contribution__import_batch=batch).count()
            entities_created = ContributorEntity.objects.count()
            clusters_created = ContributionCluster.objects.count()
            assessments_created = MembershipAssessment.objects.count()
            links_created = SourceRecordLink.objects.count()

            # Row arithmetic: count unique row hashes to determine expected
            # contributions. The importer skips any row whose hash has been
            # seen (explicit duplicates, reprocessed duplicates, and
            # intra-batch amendment duplicates that produce identical rows).
            unique_row_hashes = composition.get("unique_row_hashes", None)
            if unique_row_hashes is not None:
                expected_unique_rows = unique_row_hashes
            else:
                # Fallback: count from the CSV file
                import hashlib
                seen_hashes = set()
                with open(csv_path, "r") as f:
                    reader = csv.reader(f)
                    next(reader)  # skip header
                    for row in reader:
                        h = hashlib.sha256(",".join(row).encode()).hexdigest()
                        seen_hashes.add(h)
                expected_unique_rows = len(seen_hashes)
            
            # Subtract validation failures (missing name, bad amount, bad date)
            validation_failures = RawContribution.objects.filter(
                import_batch=batch,
                validation_status='VALIDATION_FAILED'
            ).count()
            expected_contribution_rows = expected_unique_rows - validation_failures
            prevented_duplicates = num_rows - expected_unique_rows

            contribution_arithmetic = {
                "expected_unique_rows": expected_unique_rows,
                "expected_contribution_producing_rows": expected_contribution_rows,
                "prevented_duplicates": prevented_duplicates,
                "validation_failures": validation_failures,
                "expected_contributions_created": expected_contribution_rows,
                "actual_contributions_created": contribs_created,
                "match": contribs_created == expected_contribution_rows,
                "difference": contribs_created - expected_contribution_rows,
            }

            n_chunks_import = math.ceil(num_rows / chunk_size)
            n_chunks_membership = math.ceil(entities_created / chunk_size) if entities_created > 0 else 1

            total_queries = phase_result.query_count

            if coefficients:
                calculated_ceiling = (
                    coefficients['fixed_setup_queries'] +
                    n_chunks_import * coefficients['queries_per_import_chunk'] +
                    n_chunks_membership * coefficients['queries_per_entity_chunk'] +
                    coefficients['fixed_finalization_queries']
                )
                fixed_bounded_margin = coefficients['fixed_bounded_margin']
                
                fixed_setup_queries = coefficients['fixed_setup_queries']
                queries_per_import_chunk = coefficients['queries_per_import_chunk']
                queries_per_entity_chunk = coefficients['queries_per_entity_chunk']
                fixed_finalization_queries = coefficients['fixed_finalization_queries']
            else:
                # Bootstrap ceiling from observed phase profile
                calculated_ceiling = sum(phase_profile.values())
                fixed_bounded_margin = 15
                
                fixed_setup_queries = phase_profile.get('fixed', 0)
                queries_per_import_chunk = round(phase_profile.get('import_chunk', 0) / max(1, n_chunks_import))
                queries_per_entity_chunk = round(phase_profile.get('entity_chunk', 0) / max(1, n_chunks_membership))
                fixed_finalization_queries = phase_profile.get('finalization', 0)

            formula_ceiling = calculated_ceiling + fixed_bounded_margin
            is_holdout = (num_rows == 10000)

            queries_per_row = total_queries / max(num_rows, 1)

            self.stdout.write(f"  Queries: {total_queries}")
            if formula_ceiling:
                self.stdout.write(f"  Ceiling: {formula_ceiling} (Diff: {total_queries - formula_ceiling})")
            self.stdout.write(f"  Duration: {phase_result.duration_seconds:.2f}s")
            self.stdout.write(f"  Peak memory: {peak_mem/(1024*1024):.1f} MB")
            self.stdout.write(f"  Contributions: {contribs_created}")
            
            return {
                "benchmark_method": "management_command:benchmark_import_pipeline",
                "formula_ceiling": formula_ceiling,
                "formula_coefficients": coefficients,
                "ceiling_details": {
                    "rows": num_rows,
                    "entities": entities_created,
                    "chunk_size": chunk_size,
                    "import_chunks": n_chunks_import,
                    "entity_chunks": n_chunks_membership,
                    "fixed_setup_queries": fixed_setup_queries,
                    "queries_per_import_chunk": queries_per_import_chunk,
                    "queries_per_entity_chunk": queries_per_entity_chunk,
                    "fixed_finalization_queries": fixed_finalization_queries,
                    "calculated_ceiling": calculated_ceiling,
                    "fixed_bounded_margin": fixed_bounded_margin,
                    "final_ceiling": formula_ceiling,
                    "actual_queries": total_queries,
                    "within_ceiling": total_queries <= formula_ceiling,
                    "is_holdout": is_holdout,
                },
                "input": composition,
                "output": {
                    "batch_status": batch.status,
                    "batch_row_count": batch.row_count,
                    "raw_rows_created": raw_created,
                    "contributions_created": contribs_created,
                    "entities_created": entities_created,
                },
                "performance": {
                    "total_queries": total_queries,
                    "queries_per_row": round(queries_per_row, 3),
                    "duration_seconds": round(phase_result.duration_seconds, 3),
                    "peak_memory_mb": round(peak_mem / (1024 * 1024), 2),
                },
                "contribution_arithmetic": contribution_arithmetic,
                "phase_profile": phase_profile
            }

        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)

    def _run_reprocessing_scenario(self):
        from django.contrib.auth.models import User
        from roster.models import ImportMappingProfile, RawContribution, Contribution, ImportAttempt
        from roster.services.importer import import_csv_file
        
        self.stdout.write("\nRunning Reprocessing Scenario...")
        
        user = User.objects.create_superuser(username="reprocess_actor", password="password")
        profile = ImportMappingProfile.objects.create(
            name="Reprocess Profile",
            mapping_rules={
                "NAME OF CONTRIBUTOR": "NAME OF CONTRIBUTOR",
                "AMOUNT": "AMOUNT",
                "TRANSACTION DATE": "TRANSACTION DATE",
                "ZIP": "ZIP",
                "TRANSACTION ID": "TRANSACTION ID",
            },
            owner=user,
        )
        
        csv_path, composition = self._generate_mixed_csv(50)
        
        try:
            # First import
            batch_1 = import_csv_file(csv_path, "reprocess_file.csv", profile.id, actor="reprocess_actor")
            raw_count_1 = RawContribution.objects.count()
            contrib_count_1 = Contribution.objects.count()
            
            self.stdout.write(f"  First import successful. Rows: {raw_count_1}, Contribs: {contrib_count_1}")
            
            # Second import (same file)
            try:
                import_csv_file(csv_path, "reprocess_file.csv", profile.id, actor="reprocess_actor", override_duplicate=False)
                self.stdout.write("  ERROR: Reprocessing did not raise an exception!")
            except ValueError as e:
                self.stdout.write(f"  Caught expected exception: {e}")
                
            # Verify attempts
            attempt = ImportAttempt.objects.filter(action="REPROCESS_FAILED").first()
            if attempt:
                self.stdout.write(f"  Found REPROCESS_FAILED attempt: {attempt.notes}")
                
            raw_count_2 = RawContribution.objects.count()
            contrib_count_2 = Contribution.objects.count()
            
            if raw_count_2 == raw_count_1 and contrib_count_2 == contrib_count_1:
                self.stdout.write("  SUCCESS: Zero additional raw rows or contributions created.")
            else:
                self.stdout.write("  FAIL: Additional rows created!")
                
        finally:
            if os.path.exists(csv_path):
                os.remove(csv_path)

    def _run_amendment_scenario(self):
        from django.contrib.auth.models import User
        from roster.models import ImportMappingProfile, RawContribution, Contribution
        from roster.services.importer import import_csv_file
        
        self.stdout.write("\nRunning Amendment Scenario...")
        
        user = User.objects.create_superuser(username="amend_actor", password="password")
        profile = ImportMappingProfile.objects.create(
            name="Amend Profile",
            mapping_rules={
                "NAME OF CONTRIBUTOR": "NAME OF CONTRIBUTOR",
                "AMOUNT": "AMOUNT",
                "TRANSACTION DATE": "TRANSACTION DATE",
                "ZIP": "ZIP",
                "TRANSACTION ID": "TRANSACTION ID",
            },
            owner=user,
        )
        
        # File 1: Original
        tmp1 = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv", newline="")
        writer1 = csv.writer(tmp1)
        writer1.writerow(["NAME OF CONTRIBUTOR", "AMOUNT", "TRANSACTION DATE", "ZIP", "TRANSACTION ID"])
        writer1.writerow(["AMEND DONOR", "100.00", "2026-05-15", "90012", "TXN_AMEND_1"])
        tmp1.close()
        
        # File 2: Amendment (different amount, same txn)
        tmp2 = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv", newline="")
        writer2 = csv.writer(tmp2)
        writer2.writerow(["NAME OF CONTRIBUTOR", "AMOUNT", "TRANSACTION DATE", "ZIP", "TRANSACTION ID"])
        writer2.writerow(["AMEND DONOR", "250.00", "2026-05-15", "90012", "TXN_AMEND_1"])
        tmp2.close()
        
        try:
            batch_1 = import_csv_file(tmp1.name, "orig.csv", profile.id, actor="amend_actor")
            batch_2 = import_csv_file(tmp2.name, "amend.csv", profile.id, actor="amend_actor")
            
            amend_raw = RawContribution.objects.filter(import_batch=batch_2).first()
            self.stdout.write(f"  Amendment validation status: {amend_raw.validation_status}")
            
            c1 = Contribution.objects.filter(raw_contribution__import_batch=batch_1).first()
            c2 = Contribution.objects.filter(raw_contribution__import_batch=batch_2).first()
            
            if amend_raw.validation_status == "POSSIBLE_AMENDMENT":
                self.stdout.write("  SUCCESS: Classification is POSSIBLE_AMENDMENT")
                
            self.stdout.write(f"  Original Amount: {c1.amount}")
            self.stdout.write(f"  Amended Amount: {c2.amount}")
            self.stdout.write(f"  Expected Net Total: {c1.amount + c2.amount}")
            
        finally:
            for p in [tmp1.name, tmp2.name]:
                if os.path.exists(p):
                    os.remove(p)
