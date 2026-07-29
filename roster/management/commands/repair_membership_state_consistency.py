"""Dry-run membership state consistency repair.

Produces a repair manifest comparing each current assessment against
the pure membership calculator's output. The manifest records:

- Existing current rows to demote (set is_current=False)
- Replacement current rows to insert
- Separate historical rows to insert (expected: 0)
- Unchanged current rows
- Field-level transition counts
- Locked calculation inputs for reproducibility

The dry-run is completely read-only. No database mutations occur.

When eventually executed (non-dry-run), the repair will:
- Demote exactly one existing current row per changed entity
- Insert exactly one replacement current row
- Never insert a duplicate historical copy
- Leave unchanged entities untouched
- Return ALREADY_APPLIED on safe repeat
- Return PARTIAL_FAILURE for mixed states

Usage:
    python manage.py repair_membership_state_consistency --dry-run \\
        --actor stage2b1_runner --output release/dry_run_repair_manifest.json
"""
import hashlib
import json
import os
import subprocess
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connection

from roster.models import (
    ContributorEntity,
    ContributionClusterAssignment,
    MembershipAssessment,
)
from roster.services.membership import get_active_rule, get_active_coverage
from roster.services.membership_calculator import (
    build_calculator_inputs,
    calculate_membership_state,
)


def _get_git_revision():
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            capture_output=True, text=True, cwd=".",
        )
        return result.stdout.strip()
    except Exception:
        return "UNKNOWN"


def _get_migration_state():
    try:
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("showmigrations", "roster", stdout=out)
        lines = out.getvalue().strip().split("\n")
        unapplied = [l.strip() for l in lines if "[ ]" in l]
        return {
            "all_applied": len(unapplied) == 0,
            "unapplied_count": len(unapplied),
        }
    except Exception as e:
        return {"error": str(e)}


def _get_db_digest():
    db_path = str(connection.settings_dict.get("NAME", ""))
    if os.path.exists(db_path):
        h = hashlib.sha256()
        with open(db_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    return None


def _get_matrix_digest():
    base_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    matrix_path = os.path.join(base_dir, "fixtures", "allowed_state_matrix.json")
    if not os.path.exists(matrix_path):
        return None, None
    with open(matrix_path) as f:
        data = json.load(f)
    canonical = {
        "schema_version": data.get("schema_version"),
        "policy_version": data.get("policy_version"),
        "states": data.get("states", []),
        "rejected_combinations": data.get("rejected_combinations", []),
    }
    canonical_json = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return digest, data.get("status", "UNKNOWN")


class Command(BaseCommand):
    help = "Repair membership state consistency (dry-run required)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", required=True,
            help="Dry run mode (required)",
        )
        parser.add_argument(
            "--actor", type=str, required=True,
            help="Actor running the repair",
        )
        parser.add_argument(
            "--output", type=str, required=True,
            help="Path for output manifest JSON",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        actor = options["actor"]
        output_path = options["output"]

        if not dry_run:
            self.stderr.write("Only --dry-run is supported currently.")
            return

        # Lock calculation inputs
        git_rev = _get_git_revision()
        mig_state = _get_migration_state()
        db_digest = _get_db_digest()
        matrix_digest, matrix_status = _get_matrix_digest()

        current_rule = get_active_rule()
        current_coverage = get_active_coverage()
        eval_date = current_coverage.coverage_complete_through

        # Fetch assessments
        current_assessments = (
            MembershipAssessment.objects.filter(is_current=True)
            .select_related("contributor_entity", "rule_version")
        )

        assessment_by_entity = {
            a.contributor_entity_id: a for a in current_assessments
        }
        entity_ids = list(assessment_by_entity.keys())

        entities = (
            ContributorEntity.objects.filter(id__in=entity_ids)
            .prefetch_related("clusters")
        )

        # Fetch contributions
        assignments = (
            ContributionClusterAssignment.objects.filter(
                contribution_cluster__contributor_entity_id__in=entity_ids,
                is_active=True,
                contribution__raw_contribution__import_batch__status="COMPLETED",
            )
            .select_related(
                "contribution",
                "contribution__raw_contribution",
                "contribution_cluster",
            )
        )
        entity_contribs = {}
        for assign in assignments:
            ent_id = assign.contribution_cluster.contributor_entity_id
            entity_contribs.setdefault(ent_id, []).append(assign.contribution)

        # Load matrix for state ID matching
        base_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        matrix_path = os.path.join(base_dir, "fixtures", "allowed_state_matrix.json")
        matrix_states = []
        if os.path.exists(matrix_path):
            with open(matrix_path) as f:
                matrix_states = json.load(f).get("states", [])

        def _match_matrix_state(entity, state_obj):
            """Find the matrix state ID for a computed state."""
            for ms in matrix_states:
                et = ms.get("entity_type")
                et_match = (
                    (entity.entity_type in et)
                    if isinstance(et, list)
                    else (entity.entity_type == et or et == "ANY")
                )
                vs = ms.get("verification_status", "ANY")
                vs_match = entity.verification_status == vs or vs == "ANY"
                cs_match = state_obj.calculated_status == ms["calculated_status"]
                rps_match = (
                    state_obj.recurrence_pattern_status
                    == ms["recurrence_pattern_status"]
                )
                ma_match = (
                    state_obj.membership_authority == ms["membership_authority"]
                )
                if et_match and vs_match and cs_match and rps_match and ma_match:
                    return ms.get("id", ms.get("ID", ""))
            return None

        # Build manifest
        manifest = {
            "repair_run_uuid": str(uuid.uuid4()),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "locked_inputs": {
                "git_revision": git_rev,
                "migration_state": mig_state,
                "database_snapshot_digest": db_digest,
                "matrix_content_sha256": matrix_digest,
                "matrix_status": matrix_status,
                "rule_version_id": current_rule.id,
                "repair_evaluation_date": str(eval_date),
                "coverage_through_date": str(
                    current_coverage.coverage_complete_through
                ),
                "coverage_status": current_coverage.coverage_status,
                "effective_contribution_policy_version": "2B1-002",
                "amendment_disposition_policy_version": "2B1-002",
            },
            "repairs": [],
            "summary": {
                "total_examined": 0,
                "existing_current_rows_to_demote": 0,
                "replacement_current_rows_to_insert": 0,
                "separate_historical_rows_to_insert": 0,
                "unchanged_current_rows": 0,
                "preexisting_historical_rows_preserved": 0,
                "entity_type_breakdown": {},
                "field_level_transitions": {
                    "calculated_status": {},
                    "recurrence_pattern_status": {},
                    "membership_authority": {},
                    "recurring_amount_changed": 0,
                    "payment_interval_changed": 0,
                    "rule_version_changed": 0,
                },
                "matrix_state_transitions": {},
            },
        }

        # Count preexisting historical rows
        hist_count = MembershipAssessment.objects.filter(is_current=False).count()
        manifest["summary"]["preexisting_historical_rows_preserved"] = hist_count

        for entity in entities:
            current_a = assessment_by_entity.get(entity.id)
            if not current_a:
                continue

            manifest["summary"]["total_examined"] += 1
            contributions = entity_contribs.get(entity.id, [])

            # Compute expected state
            try:
                ent_input, contrib_inputs, rule_input, cov_input = (
                    build_calculator_inputs(
                        entity=entity,
                        contributions_qs=contributions,
                        rule=current_rule,
                        coverage=current_coverage,
                    )
                )
                calc_state = calculate_membership_state(
                    entity=ent_input,
                    contributions=contrib_inputs,
                    rule=rule_input,
                    coverage=cov_input,
                    evaluation_date=eval_date,
                )
            except Exception as e:
                # Record error but continue
                manifest["repairs"].append({
                    "entity_id": entity.id,
                    "current_assessment_id": current_a.id,
                    "error": str(e),
                    "reason_code": "CALCULATOR_ERROR",
                })
                continue

            # Entity type breakdown
            et = entity.entity_type
            manifest["summary"]["entity_type_breakdown"][et] = (
                manifest["summary"]["entity_type_breakdown"].get(et, 0) + 1
            )

            # Compare ALL material fields
            cs_diff = current_a.calculated_status != calc_state.calculated_status
            rps_diff = (
                current_a.recurrence_pattern_status
                != calc_state.recurrence_pattern_status
            )
            ma_diff = (
                current_a.membership_authority != calc_state.membership_authority
            )
            amt_diff = abs(
                float(current_a.recurring_amount or 0)
                - float(calc_state.recurring_amount or 0)
            ) > 0.01
            pi_diff = (current_a.payment_interval or "") != (
                calc_state.payment_interval or ""
            )
            rv_diff = (current_a.rule_version_id or 0) != (
                calc_state.rule_version_id or 0
            )

            has_diff = cs_diff or rps_diff or ma_diff or amt_diff or pi_diff or rv_diff

            if has_diff:
                manifest["summary"]["existing_current_rows_to_demote"] += 1
                manifest["summary"]["replacement_current_rows_to_insert"] += 1

                # Track field-level transitions
                fl = manifest["summary"]["field_level_transitions"]
                if cs_diff:
                    key = f"{current_a.calculated_status}->{calc_state.calculated_status}"
                    fl["calculated_status"][key] = fl["calculated_status"].get(key, 0) + 1
                if rps_diff:
                    key = f"{current_a.recurrence_pattern_status}->{calc_state.recurrence_pattern_status}"
                    fl["recurrence_pattern_status"][key] = fl["recurrence_pattern_status"].get(key, 0) + 1
                if ma_diff:
                    key = f"{current_a.membership_authority}->{calc_state.membership_authority}"
                    fl["membership_authority"][key] = fl["membership_authority"].get(key, 0) + 1
                if amt_diff:
                    fl["recurring_amount_changed"] += 1
                if pi_diff:
                    fl["payment_interval_changed"] += 1
                if rv_diff:
                    fl["rule_version_changed"] += 1

                # Matrix state transition
                before_state_id = _match_matrix_state(entity, type("S", (), {
                    "calculated_status": current_a.calculated_status,
                    "recurrence_pattern_status": current_a.recurrence_pattern_status,
                    "membership_authority": current_a.membership_authority,
                })()) or "UNMATCHED"
                after_state_id = _match_matrix_state(entity, calc_state) or "UNMATCHED"
                ms_key = f"{before_state_id}->{after_state_id}"
                manifest["summary"]["matrix_state_transitions"][ms_key] = (
                    manifest["summary"]["matrix_state_transitions"].get(ms_key, 0) + 1
                )

                repair_record = {
                    "entity_id": entity.id,
                    "current_assessment_id": current_a.id,
                    "entity_type": entity.entity_type,
                    "before": {
                        "calculated_status": current_a.calculated_status,
                        "recurrence_pattern_status": current_a.recurrence_pattern_status,
                        "membership_authority": current_a.membership_authority,
                        "recurring_amount": float(current_a.recurring_amount or 0),
                        "payment_interval": current_a.payment_interval or "",
                        "rule_version_id": current_a.rule_version_id,
                    },
                    "after": {
                        "calculated_status": calc_state.calculated_status,
                        "recurrence_pattern_status": calc_state.recurrence_pattern_status,
                        "membership_authority": calc_state.membership_authority,
                        "recurring_amount": float(calc_state.recurring_amount or 0),
                        "payment_interval": calc_state.payment_interval or "",
                        "rule_version_id": calc_state.rule_version_id,
                    },
                    "matrix_state_before": before_state_id,
                    "matrix_state_after": after_state_id,
                    "reason_code": "STATE_MISMATCH",
                    "chapter_reevaluation_needed": cs_diff,
                    "changed_fields": [],
                }
                if cs_diff:
                    repair_record["changed_fields"].append("calculated_status")
                if rps_diff:
                    repair_record["changed_fields"].append("recurrence_pattern_status")
                if ma_diff:
                    repair_record["changed_fields"].append("membership_authority")
                if amt_diff:
                    repair_record["changed_fields"].append("recurring_amount")
                if pi_diff:
                    repair_record["changed_fields"].append("payment_interval")
                if rv_diff:
                    repair_record["changed_fields"].append("rule_version")

                manifest["repairs"].append(repair_record)
            else:
                manifest["summary"]["unchanged_current_rows"] += 1

        # Write manifest
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)

        s = manifest["summary"]
        self.stdout.write(
            f"Dry run complete. Examined: {s['total_examined']}, "
            f"Changed: {s['existing_current_rows_to_demote']}, "
            f"Unchanged: {s['unchanged_current_rows']}"
        )
        self.stdout.write(f"Manifest written to {output_path}")
