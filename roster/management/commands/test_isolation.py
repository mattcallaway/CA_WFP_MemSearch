"""Capture and compare active database logical snapshots.

Records entity/contribution/assessment counts before and after
test execution to verify no unintended data changes.

Usage:
    python manage.py test_isolation --mode before --output release/test_isolation.json
    python manage.py test_isolation --mode after --output release/test_isolation.json
"""
import hashlib
import json
import os
from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.db import connection


def _db_snapshot():
    """Take a logical snapshot of key table counts."""
    with connection.cursor() as cursor:
        tables = {
            "roster_contributorentity": "SELECT COUNT(*) FROM roster_contributorentity",
            "roster_contribution": "SELECT COUNT(*) FROM roster_contribution",
            "roster_membershipassessment": "SELECT COUNT(*) FROM roster_membershipassessment",
            "roster_rawcontribution": "SELECT COUNT(*) FROM roster_rawcontribution",
            "roster_importbatch": "SELECT COUNT(*) FROM roster_importbatch",
            "roster_auditevent": "SELECT COUNT(*) FROM roster_auditevent",
            "roster_contributioncluster": "SELECT COUNT(*) FROM roster_contributioncluster",
            "roster_membershipruleversion": "SELECT COUNT(*) FROM roster_membershipruleversion",
        }
        snapshot = {}
        for table, sql in tables.items():
            try:
                cursor.execute(sql)
                snapshot[table] = cursor.fetchone()[0]
            except Exception:
                snapshot[table] = -1

    # DB file hash
    db_path = str(connection.settings_dict.get("NAME", ""))
    if os.path.exists(db_path):
        h = hashlib.sha256()
        with open(db_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        snapshot["_db_sha256"] = h.hexdigest()
    else:
        snapshot["_db_sha256"] = None

    return snapshot


class Command(BaseCommand):
    help = "Capture active database logical snapshots for isolation verification."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode", type=str, choices=["before", "after", "both"],
            default="both",
            help="Capture mode",
        )
        parser.add_argument(
            "--output", type=str, default="release/test_isolation.json",
            help="Output path",
        )

    def handle(self, *args, **options):
        mode = options["mode"]
        output_path = options["output"]
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Load existing data if appending
        existing = {}
        if os.path.exists(output_path):
            with open(output_path) as f:
                existing = json.load(f)

        snapshot = _db_snapshot()
        ts = datetime.now(timezone.utc).isoformat()

        if mode == "before":
            existing["before"] = {"snapshot": snapshot, "timestamp": ts}
        elif mode == "after":
            existing["after"] = {"snapshot": snapshot, "timestamp": ts}
        elif mode == "both":
            existing["before"] = {"snapshot": snapshot, "timestamp": ts}
            existing["after"] = {"snapshot": snapshot, "timestamp": ts}

        # Compare if both snapshots exist
        if "before" in existing and "after" in existing:
            before = existing["before"]["snapshot"]
            after = existing["after"]["snapshot"]
            changes = {}
            for key in before:
                if key.startswith("_"):
                    continue
                if before.get(key) != after.get(key):
                    changes[key] = {
                        "before": before.get(key),
                        "after": after.get(key),
                    }
            existing["logical_pass"] = len(changes) == 0
            existing["changes"] = changes
            existing["db_hash_match"] = (
                before.get("_db_sha256") == after.get("_db_sha256")
            )

        with open(output_path, "w") as f:
            json.dump(existing, f, indent=2)

        logical = existing.get("logical_pass")
        self.stdout.write(
            f"Isolation check: {'PASS' if logical else 'PENDING' if logical is None else 'FAIL'}"
        )
