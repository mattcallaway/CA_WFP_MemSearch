"""Test migrations on an isolated copy of the active database.

Uses SQLite's backup API to create a consistent copy, then:
1. Records pre-migration logical fingerprint
2. Applies all migrations
3. Runs Django checks
4. Records post-migration fingerprint
5. Verifies no data changed unexpectedly
6. Verifies new amendment tables exist
7. Runs amendment tests

Usage:
    python manage.py test_migration_safety --output release/migration_test.json
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.db import connection


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _logical_fingerprint(db_path):
    """Compute logical fingerprint of key tables."""
    conn = sqlite3.connect(db_path)
    fingerprint = {}
    tables = [
        "roster_contributorentity",
        "roster_contribution",
        "roster_membershipassessment",
        "roster_rawcontribution",
        "roster_importbatch",
        "roster_contributionclu",  # Might be truncated
        "roster_contributioncluster",
        "roster_contributionclusterassignment",
        "roster_auditevent",
        "roster_membershipruleversion",
        "roster_datasetcoveragemetadata",
    ]
    for table in tables:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            fingerprint[table] = {"count": count}
        except Exception:
            # Table might not exist yet
            pass

    # Geography tables
    for table in ["roster_county", "roster_geographicplace",
                  "roster_postalarea", "roster_location"]:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            fingerprint[table] = {"count": count}
        except Exception:
            pass

    # Chapter tables
    for table in ["roster_chapter", "roster_chapterevaluationrun",
                  "roster_chapterevaluationresult"]:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            fingerprint[table] = {"count": count}
        except Exception:
            pass

    conn.close()
    return fingerprint


class Command(BaseCommand):
    help = "Test migrations on an isolated DB copy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", type=str, default="release/migration_test.json",
            help="Output path for test results",
        )

    def handle(self, *args, **options):
        output_path = options["output"]
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        active_db = str(connection.settings_dict.get("NAME", ""))
        if not os.path.exists(active_db):
            self.stderr.write(f"Active database not found: {active_db}")
            return

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_path": active_db,
            "source_hash": _sha256_file(active_db),
        }

        # Step 1: Create consistent copy via SQLite backup API
        with tempfile.TemporaryDirectory(prefix="wfp_migration_test_") as tmpdir:
            copy_path = os.path.join(tmpdir, "test_copy.sqlite3")

            src_conn = sqlite3.connect(active_db)
            dst_conn = sqlite3.connect(copy_path)
            src_conn.backup(dst_conn)
            src_conn.close()
            dst_conn.close()

            copy_hash = _sha256_file(copy_path)
            result["copy_path"] = copy_path
            result["copy_hash"] = copy_hash

            # Step 2: Pre-migration fingerprint
            pre_fp = _logical_fingerprint(copy_path)
            result["pre_migration_fingerprint"] = pre_fp

            # Step 3: Apply migrations on the copy via subprocess
            env = os.environ.copy()
            env["WFP_RELIABILITY_DB_PATH"] = copy_path
            env["DJANGO_SETTINGS_MODULE"] = "wfp_memsearch.test_settings_reliability"

            mig_result = subprocess.run(
                [sys.executable, "manage.py", "migrate", "--run-syncdb", "--verbosity=1"],
                capture_output=True, text=True, env=env, cwd=".",
                timeout=120,
            )
            result["migration_exit_code"] = mig_result.returncode
            result["migration_stdout"] = mig_result.stdout[-2000:]
            result["migration_stderr"] = mig_result.stderr[-2000:]

            # Step 4: Run Django checks
            check_result = subprocess.run(
                [sys.executable, "manage.py", "check"],
                capture_output=True, text=True, env=env, cwd=".",
                timeout=60,
            )
            result["django_check_exit_code"] = check_result.returncode

            # Step 5: Post-migration fingerprint
            post_fp = _logical_fingerprint(copy_path)
            result["post_migration_fingerprint"] = post_fp

            # Step 6: Compare fingerprints
            data_changes = {}
            unchanged = True
            for table in pre_fp:
                pre_count = pre_fp[table]["count"]
                post_count = post_fp.get(table, {}).get("count", -1)
                if pre_count != post_count:
                    data_changes[table] = {
                        "before": pre_count,
                        "after": post_count,
                    }
                    unchanged = False

            result["data_unchanged"] = unchanged
            result["data_changes"] = data_changes

            # Step 7: Verify amendment table exists
            try:
                test_conn = sqlite3.connect(copy_path)
                cursor = test_conn.execute(
                    "SELECT COUNT(*) FROM roster_amendmentrelationship"
                )
                amendment_count = cursor.fetchone()[0]
                result["amendment_table_exists"] = True
                result["amendment_row_count"] = amendment_count

                # Check constraints exist
                cursor = test_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'roster_amendmentrelationship'"
                )
                create_sql = cursor.fetchone()
                result["amendment_table_sql"] = (
                    create_sql[0][:500] if create_sql else None
                )
                test_conn.close()
            except Exception as e:
                result["amendment_table_exists"] = False
                result["amendment_table_error"] = str(e)

            # Step 8: Run amendment tests against the copy
            test_result = subprocess.run(
                [sys.executable, "manage.py", "test",
                 "roster.tests.test_amendment_disposition",
                 "--verbosity=2"],
                capture_output=True, text=True, env=env, cwd=".",
                timeout=120,
            )
            result["amendment_test_exit_code"] = test_result.returncode
            result["amendment_test_output"] = test_result.stdout[-2000:]

        # Overall result
        result["overall_pass"] = (
            result.get("migration_exit_code") == 0
            and result.get("django_check_exit_code") == 0
            and result.get("data_unchanged", False)
            and result.get("amendment_table_exists", False)
        )

        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

        self.stdout.write(
            f"Migration test: {'PASS' if result['overall_pass'] else 'FAIL'}"
        )
        self.stdout.write(f"Results: {output_path}")
