"""Audit membership state consistency with dual recomputation modes.

Provides two independent validation modes:

1. Historical parity: did the stored assessment match the calculator output
   at the time it was produced?
2. Current repair: does the stored assessment match what the calculator
   would produce NOW with current rule/coverage/contributions?

Also checks matrix-tuple compliance and evidence-condition compliance.

Usage:
    python manage.py audit_membership_state_consistency --json
    python manage.py audit_membership_state_consistency --json --output release/g06_audit.json
"""
import json
import os
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand

from roster.models import (
    MembershipAssessment,
    ContributorEntity,
    ContributionClusterAssignment,
)
from roster.services.membership import get_active_rule, get_active_coverage
from roster.services.membership_calculator import (
    build_calculator_inputs,
    calculate_membership_state,
)


def _normalize_nullable(val):
    """Normalize null-equivalent values to None.

    Treats None, "", and "None" as equivalent non-applicable values.
    """
    if val is None or val == "" or val == "None":
        return None
    return val


class Command(BaseCommand):
    help = "Audit membership state consistency with dual recomputation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json", action="store_true",
            help="Output in JSON format",
        )
        parser.add_argument(
            "--output", type=str, default=None,
            help="Write JSON results to file",
        )

    def handle(self, *args, **options):
        # Load matrix
        base_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        matrix_path = os.path.join(base_dir, "fixtures", "allowed_state_matrix.json")
        with open(matrix_path, "r") as f:
            matrix = json.load(f)

        allowed_states = matrix["states"]
        rejected_rules = matrix.get("rejected_combinations", [])

        # Use lowercase key names (support both old and new format)
        def _state_id(s):
            return s.get("id", s.get("ID", ""))

        def _rule_id(r):
            return r.get("id", r.get("ID", ""))

        def _rule_fields(r):
            return r.get("fields", r.get("Fields", {}))

        def _rule_name(r):
            return r.get("rule", r.get("Rule", ""))

        # Fetch current assessments
        assessments = (
            MembershipAssessment.objects
            .filter(is_current=True)
            .select_related("contributor_entity", "rule_version")
        )

        # Fetch all entities
        entity_ids = [a.contributor_entity_id for a in assessments]
        entities = {
            e.id: e
            for e in ContributorEntity.objects.filter(id__in=entity_ids)
            .prefetch_related("clusters")
        }

        # Current rule and coverage for repair mode
        current_rule = get_active_rule()
        current_coverage = get_active_coverage()
        repair_eval_date = current_coverage.coverage_complete_through

        # Fetch active assignments for all entities
        all_assignments = (
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
        for assign in all_assignments:
            ent_id = assign.contribution_cluster.contributor_entity_id
            entity_contribs.setdefault(ent_id, []).append(assign.contribution)

        # Diagnostic accumulators
        records = []
        summary = {
            "total_assessments": 0,
            "matrix_tuple_violations": 0,
            "evidence_context_violations": 0,
            "historical_parity_mismatches": 0,
            "current_repair_mismatches": 0,
            "fully_matching": 0,
            "field_level_diffs": {
                "calculated_status": 0,
                "recurrence_pattern_status": 0,
                "membership_authority": 0,
                "recurring_amount": 0,
                "payment_interval": 0,
                "rule_version": 0,
                "evaluation_date": 0,
            },
            "historical_parity_classification": {
                "PROVEN_HISTORICAL_MISMATCH": 0,
                "HISTORICAL_MATCH": 0,
                "BEST_EFFORT_RECONSTRUCTION_MISMATCH": 0,
                "INSUFFICIENT_HISTORICAL_EVIDENCE": 0,
            },
        }

        def _match_stored_matrix_state(entity, assessment):
            for state in allowed_states:
                et = state.get("entity_type")
                et_match = (
                    (entity.entity_type in et)
                    if isinstance(et, list)
                    else (entity.entity_type == et or et == "ANY")
                )
                vs = state.get("verification_status", "ANY")
                vs_match = entity.verification_status == vs or vs == "ANY"
                cs_match = assessment.calculated_status == state["calculated_status"]
                rps_match = assessment.recurrence_pattern_status == state["recurrence_pattern_status"]
                ma_match = assessment.membership_authority == state["membership_authority"]
                if et_match and vs_match and cs_match and rps_match and ma_match:
                    return _state_id(state)
            return "UNMATCHED"

        def _match_computed_matrix_state(entity, computed_state):
            for state in allowed_states:
                et = state.get("entity_type")
                et_match = (
                    (entity.entity_type in et)
                    if isinstance(et, list)
                    else (entity.entity_type == et or et == "ANY")
                )
                vs = state.get("verification_status", "ANY")
                vs_match = entity.verification_status == vs or vs == "ANY"
                cs_match = computed_state.calculated_status == state["calculated_status"]
                rps_match = computed_state.recurrence_pattern_status == state["recurrence_pattern_status"]
                ma_match = computed_state.membership_authority == state["membership_authority"]
                if et_match and vs_match and cs_match and rps_match and ma_match:
                    return _state_id(state)
            return "UNMATCHED"

        def _fmt_amt(val):
            return format(Decimal(val or 0).quantize(Decimal("0.01")), ".2f")

        for a in assessments:
            summary["total_assessments"] += 1
            entity = entities.get(a.contributor_entity_id)
            if not entity:
                continue

            contributions = entity_contribs.get(entity.id, [])

            # --- Matrix tuple check ---
            tuple_match = False
            matched_state_id = None
            for state in allowed_states:
                et = state.get("entity_type")
                et_match = (
                    (entity.entity_type in et)
                    if isinstance(et, list)
                    else (entity.entity_type == et or et == "ANY")
                )
                vs = state.get("verification_status", "ANY")
                vs_match = entity.verification_status == vs or vs == "ANY"
                cs_match = a.calculated_status == state["calculated_status"]
                rps_match = (
                    a.recurrence_pattern_status
                    == state["recurrence_pattern_status"]
                )
                ma_match = a.membership_authority == state["membership_authority"]

                if et_match and vs_match and cs_match and rps_match and ma_match:
                    tuple_match = True
                    matched_state_id = _state_id(state)
                    break

            if not tuple_match:
                summary["matrix_tuple_violations"] += 1

            # --- Evidence condition check ---
            evidence_match = True
            if tuple_match and matched_state_id:
                matched_state = next(
                    (s for s in allowed_states if _state_id(s) == matched_state_id),
                    None,
                )
                if matched_state and "evidence_conditions" in matched_state:
                    ec = matched_state["evidence_conditions"]
                    # Count positive contributions
                    pos_count = sum(
                        1
                        for c in contributions
                        if c.transaction_type == "CONTRIBUTION" and c.amount > 0
                    )
                    ec_min = ec.get("positive_contribution_count_min")
                    ec_max = ec.get("positive_contribution_count_max")
                    if ec_min is not None and pos_count < ec_min:
                        evidence_match = False
                    if ec_max is not None and pos_count > ec_max:
                        evidence_match = False

                if not evidence_match:
                    summary["evidence_context_violations"] += 1

            # --- Current repair recomputation ---
            try:
                ent_input, contrib_inputs, rule_input, cov_input = (
                    build_calculator_inputs(
                        entity=entity,
                        contributions_qs=contributions,
                        rule=current_rule,
                        coverage=current_coverage,
                    )
                )
                repair_state = calculate_membership_state(
                    entity=ent_input,
                    contributions=contrib_inputs,
                    rule=rule_input,
                    coverage=cov_input,
                    evaluation_date=repair_eval_date,
                )
                repair_error = None
            except Exception as e:
                repair_state = None
                repair_error = str(e)

            # --- Historical parity recomputation ---
            # Use stored assessment's evaluation date and rule version
            try:
                hist_eval_date = (
                    a.calculation_date.date()
                    if a.calculation_date
                    else repair_eval_date
                )
                hist_rule = a.rule_version if a.rule_version else current_rule
                ent_input_h, contrib_inputs_h, rule_input_h, cov_input_h = (
                    build_calculator_inputs(
                        entity=entity,
                        contributions_qs=contributions,
                        rule=hist_rule,
                        coverage=current_coverage,  # Best available
                    )
                )
                hist_state = calculate_membership_state(
                    entity=ent_input_h,
                    contributions=contrib_inputs_h,
                    rule=rule_input_h,
                    coverage=cov_input_h,
                    evaluation_date=hist_eval_date,
                )
                hist_error = None
            except Exception as e:
                hist_state = None
                hist_error = str(e)

            # --- Compare fields ---
            def _compare_repair(stored_a, computed):
                if computed is None:
                    return {"error": repair_error}
                diffs = {}
                if stored_a.calculated_status != computed.calculated_status:
                    diffs["calculated_status"] = {
                        "stored": stored_a.calculated_status,
                        "computed": computed.calculated_status,
                    }
                if (
                    stored_a.recurrence_pattern_status
                    != computed.recurrence_pattern_status
                ):
                    diffs["recurrence_pattern_status"] = {
                        "stored": stored_a.recurrence_pattern_status,
                        "computed": computed.recurrence_pattern_status,
                    }
                if stored_a.membership_authority != computed.membership_authority:
                    diffs["membership_authority"] = {
                        "stored": stored_a.membership_authority,
                        "computed": computed.membership_authority,
                    }
                stored_amt = Decimal(stored_a.recurring_amount or 0).quantize(Decimal("0.01"))
                computed_amt = Decimal(computed.recurring_amount or 0).quantize(Decimal("0.01"))
                if stored_amt != computed_amt:
                    diffs["recurring_amount"] = {
                        "stored": format(stored_amt, ".2f"),
                        "computed": format(computed_amt, ".2f"),
                    }
                stored_pi = _normalize_nullable(stored_a.payment_interval)
                computed_pi = _normalize_nullable(computed.payment_interval)
                if stored_pi != computed_pi:
                    diffs["payment_interval"] = {
                        "stored": stored_pi,
                        "computed": computed_pi,
                    }
                stored_rv = (
                    stored_a.rule_version_id if stored_a.rule_version_id else None
                )
                if stored_rv != computed.rule_version_id:
                    diffs["rule_version"] = {
                        "stored": stored_rv,
                        "computed": computed.rule_version_id,
                    }
                return diffs

            repair_diffs = _compare_repair(a, repair_state)
            hist_diffs = _compare_repair(a, hist_state) if hist_state else {"error": hist_error}

            repair_match = isinstance(repair_diffs, dict) and len(repair_diffs) == 0
            hist_match = isinstance(hist_diffs, dict) and len(hist_diffs) == 0

            if not repair_match:
                summary["current_repair_mismatches"] += 1
                if isinstance(repair_diffs, dict):
                    for field_name in repair_diffs:
                        if field_name in summary["field_level_diffs"]:
                            summary["field_level_diffs"][field_name] += 1

            if not hist_match:
                summary["historical_parity_mismatches"] += 1
                # Classify the historical-parity mismatch
                # Historical coverage is unavailable (using current coverage as proxy)
                # Contributions are not filtered to those operative at the historical date
                # Amendment disposition at the historical date is not tracked
                has_stored_rule = a.rule_version_id is not None
                has_stored_eval_date = a.calculation_date is not None
                if not has_stored_eval_date and not has_stored_rule:
                    classification = "INSUFFICIENT_HISTORICAL_EVIDENCE"
                else:
                    # Coverage and amendment-disposition history are never available
                    classification = "BEST_EFFORT_RECONSTRUCTION_MISMATCH"
                summary["historical_parity_classification"][classification] += 1
            else:
                summary["historical_parity_classification"]["HISTORICAL_MATCH"] += 1

            if repair_match and tuple_match and evidence_match:
                summary["fully_matching"] += 1

            # Stored matrix state
            stored_matrix_state_id = matched_state_id or "UNMATCHED"
            # Repair matrix state
            repair_matrix_state_id = "UNMATCHED"
            if repair_state:
                repair_matrix_state_id = _match_computed_matrix_state(entity, repair_state)

            record = {
                "entity_id": entity.id,
                "assessment_id": a.id,
                "entity_type": entity.entity_type,
                "verification_status": entity.verification_status,
                "matrix_tuple_match": tuple_match,
                "matched_state_id": matched_state_id,
                "stored_matrix_state_id": stored_matrix_state_id,
                "repair_matrix_state_id": repair_matrix_state_id,
                "evidence_condition_match": evidence_match,
                "historical_parity_match": hist_match,
                "current_repair_match": repair_match,
                "stored_calculated_status": a.calculated_status,
                "stored_recurrence_pattern_status": a.recurrence_pattern_status,
                "stored_membership_authority": a.membership_authority,
                "stored_state": {
                    "calculated_status": a.calculated_status,
                    "recurrence_pattern_status": a.recurrence_pattern_status,
                    "membership_authority": a.membership_authority,
                    "recurring_amount": _fmt_amt(a.recurring_amount),
                    "payment_interval": _normalize_nullable(a.payment_interval),
                    "rule_version_id": a.rule_version_id,
                    "evaluation_date": str(a.calculation_date.date() if a.calculation_date else repair_eval_date),
                },
            }
            if repair_state:
                record["repair_calculated_status"] = repair_state.calculated_status
                record["repair_recurrence_pattern_status"] = repair_state.recurrence_pattern_status
                record["repair_membership_authority"] = repair_state.membership_authority
                record["repair_state"] = {
                    "calculated_status": repair_state.calculated_status,
                    "recurrence_pattern_status": repair_state.recurrence_pattern_status,
                    "membership_authority": repair_state.membership_authority,
                    "recurring_amount": _fmt_amt(repair_state.recurring_amount),
                    "payment_interval": _normalize_nullable(repair_state.payment_interval),
                    "rule_version_id": repair_state.rule_version_id,
                    "evaluation_date": str(repair_eval_date),
                }

            if not repair_match and isinstance(repair_diffs, dict):
                record["repair_field_diffs"] = repair_diffs
            if not hist_match and isinstance(hist_diffs, dict):
                record["historical_field_diffs"] = hist_diffs
                record["historical_parity_classification"] = classification

            records.append(record)

        output_data = {
            "summary": summary,
            "records": records,
        }

        if options.get("output"):
            os.makedirs(os.path.dirname(options["output"]) or ".", exist_ok=True)
            with open(options["output"], "w") as f:
                json.dump(output_data, f, indent=2, default=str)
            self.stdout.write(f"Audit written to {options['output']}")

        if options["json"]:
            self.stdout.write(json.dumps(output_data, indent=2, default=str))
        else:
            s = summary
            self.stdout.write(f"Total Assessments: {s['total_assessments']}")
            self.stdout.write(f"Matrix Tuple Violations: {s['matrix_tuple_violations']}")
            self.stdout.write(
                f"Evidence Context Violations: {s['evidence_context_violations']}"
            )
            self.stdout.write(
                f"Historical Parity Mismatches: {s['historical_parity_mismatches']}"
            )
            self.stdout.write(
                f"Current Repair Mismatches: {s['current_repair_mismatches']}"
            )
            self.stdout.write(f"Fully Matching: {s['fully_matching']}")
            self.stdout.write("Field-Level Diffs:")
            for k, v in s["field_level_diffs"].items():
                self.stdout.write(f"  {k}: {v}")
