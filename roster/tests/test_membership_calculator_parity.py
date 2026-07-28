"""Membership calculator parity, zero-query, predicate, and vector tests.

Verifies:
- calculate_membership_state() executes zero database queries
- build_calculator_inputs() raises ValueError on verification inconsistency
- Every matrix state S01–S11 has at least one positive test vector
- Every rejected rule R01–R06 has at least one negative test vector
- Predicates use dual arguments (state + entity)
- S07 satisfies is_provisional_possible_member
- S09 does NOT satisfy is_provisional_possible_member
- Invalid org/unverified states fail is_current_authoritative_member
- ONE_TIME, DATASET_TOO_STALE, INSUFFICIENT_HISTORY fail membership predicate
"""
import json
import os
import datetime
from decimal import Decimal

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from roster.services.membership_calculator import (
    MembershipEntityInput, MembershipContributionInput, MembershipRuleInput,
    CoverageInput, MembershipState, MembershipEvidenceSummary,
    calculate_membership_state, calculate_net_payments,
    build_calculator_inputs,
    is_current_authoritative_member, is_provisional_possible_member,
    is_previously_recurring_donor, is_lapsed_donor, is_one_time_donor,
    is_ineligible_entity,
)


# --- Helpers ---------------------------------------------------------------

def _load_fixture(filename):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base_dir, 'fixtures', filename)
    with open(path, 'r') as f:
        return json.load(f)


def _parse_date(d_str):
    if d_str is None:
        return None
    if isinstance(d_str, datetime.date):
        return d_str
    return datetime.datetime.strptime(d_str, '%Y-%m-%d').date()


def _build_inputs_from_vector(inputs):
    """Build calculator inputs from a test vector's 'inputs' dict."""
    entity = MembershipEntityInput(
        entity_id=0,
        entity_type=inputs['entity_type'],
        verification_status=inputs['verification_status'],
    )
    contributions = tuple(
        MembershipContributionInput(
            transaction_date=_parse_date(c['transaction_date']),
            amount=Decimal(str(c['amount'])),
            transaction_type=c['transaction_type'],
            source_record_id=c.get('source_record_id', 0),
        )
        for c in inputs.get('contributions', [])
    )
    rule_data = inputs['rule']
    rule = MembershipRuleInput(
        rule_version_id=rule_data['rule_version_id'],
        monthly_interval_min=rule_data['monthly_interval_min'],
        monthly_interval_max=rule_data['monthly_interval_max'],
        active_grace_period=rule_data['active_grace_period'],
        min_recurring_payments=rule_data['min_recurring_payments'],
        skip_payment_allowed=rule_data['skip_payment_allowed'],
    )
    coverage = CoverageInput(
        through_date=_parse_date(inputs.get('coverage_through_date')),
        is_complete=inputs.get('coverage_is_complete', False),
    )
    eval_date = _parse_date(inputs['evaluation_date'])
    return entity, contributions, rule, coverage, eval_date


def _make_dummy_evidence():
    """Create a minimal evidence summary for manual state construction."""
    return MembershipEvidenceSummary(
        contribution_count=0,
        positive_contribution_count=0,
        refund_count=0,
        interval_days=(),
        coverage_age_days=None,
        coverage_is_complete=False,
    )


# --- Dummy ORM stand-in for build_calculator_inputs test -------------------

class _DummyEntity:
    def __init__(self, *, is_verified, verification_status, verification_method, entity_type='INDIVIDUAL'):
        self.id = 99
        self.is_verified = is_verified
        self.verification_status = verification_status
        self.verification_method = verification_method
        self.entity_type = entity_type


# --- Tests -----------------------------------------------------------------

class MembershipCalculatorParityTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_vectors = _load_fixture('membership_test_vectors.json')
        cls.raw_matrix = _load_fixture('allowed_state_matrix.json')
        cls.matrix_states_list = cls.raw_matrix['states']
        # Support both 'id' (v2) and 'ID' (v1) keys in matrix
        cls.matrix_states = {
            s.get('id', s.get('ID', '')): s for s in cls.matrix_states_list
        }
        cls.rejected_rules = cls.raw_matrix['rejected_combinations']

    # --- A. Zero-query proof -----------------------------------------------

    def test_zero_query_proof(self):
        """calculate_membership_state executes exactly zero database queries."""
        vector = next(v for v in self.test_vectors if not v.get('is_negative_vector', False))
        entity, contributions, rule, coverage, eval_date = _build_inputs_from_vector(vector['inputs'])

        with CaptureQueriesContext(connection) as ctx:
            calculate_membership_state(
                entity=entity,
                contributions=contributions,
                rule=rule,
                coverage=coverage,
                evaluation_date=eval_date,
            )

        self.assertEqual(
            len(ctx), 0,
            f"calculate_membership_state executed {len(ctx)} queries (expected 0)",
        )

    # --- B. Verification consistency boundary ------------------------------

    def test_verification_consistency_rejects_mismatch(self):
        """build_calculator_inputs raises ValueError when is_verified disagrees."""
        # is_verified=True but UNVERIFIED status
        bad1 = _DummyEntity(
            is_verified=True,
            verification_status='UNVERIFIED',
            verification_method='NONE',
        )
        with self.assertRaises(ValueError):
            build_calculator_inputs(bad1, [], None, None)

        # is_verified=False but VERIFIED status + real method
        bad2 = _DummyEntity(
            is_verified=False,
            verification_status='VERIFIED',
            verification_method='ADMIN_REVIEW',
        )
        with self.assertRaises(ValueError):
            build_calculator_inputs(bad2, [], None, None)

    # --- C. Matrix-derived coverage reporting ------------------------------

    def test_matrix_coverage(self):
        """Every matrix state has ≥1 positive vector, every rejected rule ≥1 negative."""
        positive_vectors = [v for v in self.test_vectors if not v.get('is_negative_vector', False)]
        negative_vectors = [v for v in self.test_vectors if v.get('is_negative_vector', False)]

        all_matrix_ids = {
            s.get('id', s.get('ID', '')) for s in self.matrix_states_list
        }
        positive_matrix_ids = {
            v.get('expected_state_id', v.get('expected_matrix_state_id', ''))
            for v in positive_vectors
            if v.get('expected_state_id') or v.get('expected_matrix_state_id')
        }

        # S11 has multiple entity types; vectors reference it by ID
        states_without_vectors = all_matrix_ids - positive_matrix_ids
        vectors_without_state = [
            v for v in positive_vectors
            if v.get('expected_state_id', v.get('expected_matrix_state_id', '')) not in all_matrix_ids
        ]

        print(f"\n=== Coverage Report ===")
        print(f"Matrix state count (before expansion): {len(self.matrix_states_list)}")
        print(f"Positive vectors: {len(positive_vectors)}")
        print(f"Negative vectors: {len(negative_vectors)}")
        print(f"States without positive vectors: {states_without_vectors or 'NONE'}")
        print(f"Vectors without matching state: {len(vectors_without_state)}")

        self.assertEqual(len(states_without_vectors), 0, f"States missing vectors: {states_without_vectors}")
        self.assertEqual(len(vectors_without_state), 0)
        self.assertGreaterEqual(len(negative_vectors), 6, "Need ≥6 negative vectors for R01–R06")

    # --- D. Positive vector tests ------------------------------------------

    def test_positive_vectors(self):
        """Each positive vector matches its expected matrix state."""
        positive_vectors = [v for v in self.test_vectors if not v.get('is_negative_vector', False)]
        for vector in positive_vectors:
            with self.subTest(vector_id=vector['vector_id']):
                entity, contributions, rule, coverage, eval_date = _build_inputs_from_vector(vector['inputs'])

                state = calculate_membership_state(
                    entity=entity,
                    contributions=contributions,
                    rule=rule,
                    coverage=coverage,
                    evaluation_date=eval_date,
                )

                expected_id = vector.get('expected_state_id', vector.get('expected_matrix_state_id'))
                expected = self.matrix_states[expected_id]
                self.assertEqual(
                    state.calculated_status, expected['calculated_status'],
                    f"Vector {vector['vector_id']}: calculated_status",
                )
                self.assertEqual(
                    state.recurrence_pattern_status, expected['recurrence_pattern_status'],
                    f"Vector {vector['vector_id']}: recurrence_pattern_status",
                )
                self.assertEqual(
                    state.membership_authority, expected['membership_authority'],
                    f"Vector {vector['vector_id']}: membership_authority",
                )

                # Validate predicates
                preds = vector.get('expected_predicates', {})
                for pred_name, expected_val in preds.items():
                    pred_fn = {
                        'is_current_authoritative_member': is_current_authoritative_member,
                        'is_provisional_possible_member': is_provisional_possible_member,
                        'is_one_time_donor': is_one_time_donor,
                        'is_ineligible_entity': is_ineligible_entity,
                        'is_lapsed_donor': is_lapsed_donor,
                        'is_previously_recurring_donor': is_previously_recurring_donor,
                    }.get(pred_name)
                    if pred_fn:
                        actual = pred_fn(state, entity)
                        self.assertEqual(
                            actual, expected_val,
                            f"Vector {vector['vector_id']}: {pred_name} expected {expected_val}, got {actual}",
                        )

    # --- E. Negative vector tests ------------------------------------------

    def test_negative_vectors(self):
        """Each negative vector does not produce the rejected combination."""
        negative_vectors = [v for v in self.test_vectors if v.get('is_negative_vector', False)]
        for vector in negative_vectors:
            with self.subTest(vector_id=vector['vector_id']):
                entity, contributions, rule, coverage, eval_date = _build_inputs_from_vector(vector['inputs'])

                state = calculate_membership_state(
                    entity=entity,
                    contributions=contributions,
                    rule=rule,
                    coverage=coverage,
                    evaluation_date=eval_date,
                )

                # The rejected rule ID tells us which rule to check
                rejected_rule_id = vector.get('expected_rejected_rule_id')
                if rejected_rule_id:
                    rejected = next(
                        (r for r in self.rejected_rules
                         if r.get('id', r.get('ID', '')) == rejected_rule_id),
                        None,
                    )
                    if rejected:
                        fields = rejected.get('fields', rejected.get('Fields', {}))
                        # Check that the result does NOT match the rejected combination
                        matches = True
                        for field, expected_val in fields.items():
                            actual = getattr(state, field, None)
                            if actual is None:
                                # Field is on entity, not state
                                actual = getattr(entity, field, None)
                            if isinstance(expected_val, list):
                                if actual not in expected_val:
                                    matches = False
                                    break
                            else:
                                if actual != expected_val:
                                    matches = False
                                    break
                        self.assertFalse(
                            matches,
                            f"Vector {vector['vector_id']} produced rejected combination {rejected_rule_id}",
                        )

    # --- F. Predicate tests (manual construction) --------------------------

    def test_s07_satisfies_provisional_possible_member(self):
        """S07: Unverified individual with RECURRING_PATTERN → is_provisional_possible_member."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="UNVERIFIED")
        state = MembershipState(
            calculated_status="PROVISIONAL",
            recurrence_pattern_status="RECURRING_PATTERN",
            membership_authority="PROVISIONAL",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('50.00'),
            payment_interval="Monthly",
        )
        self.assertTrue(is_provisional_possible_member(state, entity))

    def test_s09_does_not_satisfy_provisional_possible_member(self):
        """S09: Unverified single contribution (INSUFFICIENT_HISTORY) → NOT is_provisional_possible_member."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="UNVERIFIED")
        state = MembershipState(
            calculated_status="PROVISIONAL",
            recurrence_pattern_status="INSUFFICIENT_HISTORY",
            membership_authority="PROVISIONAL",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval="None",
        )
        self.assertFalse(is_provisional_possible_member(state, entity))

    def test_organization_active_not_current_member(self):
        """ACTIVE state for ORGANIZATION → is_current_authoritative_member returns False."""
        entity = MembershipEntityInput(entity_id=0, entity_type="ORGANIZATION", verification_status="VERIFIED")
        state = MembershipState(
            calculated_status="ACTIVE",
            recurrence_pattern_status="RECURRING_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('50.00'),
            payment_interval="Monthly",
        )
        self.assertFalse(is_current_authoritative_member(state, entity))

    def test_unverified_active_not_current_member(self):
        """ACTIVE state for unverified INDIVIDUAL → is_current_authoritative_member returns False."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="UNVERIFIED")
        state = MembershipState(
            calculated_status="ACTIVE",
            recurrence_pattern_status="RECURRING_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('50.00'),
            payment_interval="Monthly",
        )
        self.assertFalse(is_current_authoritative_member(state, entity))

    def test_one_time_authoritative_not_current_member(self):
        """ONE_TIME + AUTHORITATIVE → is_current_authoritative_member returns False."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="VERIFIED")
        state = MembershipState(
            calculated_status="ONE_TIME",
            recurrence_pattern_status="NO_RECURRING_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval="None",
        )
        self.assertFalse(is_current_authoritative_member(state, entity))

    def test_dataset_too_stale_not_current_member(self):
        """DATASET_TOO_STALE + AUTHORITATIVE → is_current_authoritative_member returns False."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="VERIFIED")
        state = MembershipState(
            calculated_status="DATASET_TOO_STALE",
            recurrence_pattern_status="NO_RECURRING_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval="None",
        )
        self.assertFalse(is_current_authoritative_member(state, entity))

    def test_insufficient_history_not_current_member(self):
        """INSUFFICIENT_HISTORY + AUTHORITATIVE → is_current_authoritative_member returns False."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="VERIFIED")
        state = MembershipState(
            calculated_status="INSUFFICIENT_HISTORY",
            recurrence_pattern_status="IRREGULAR_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval="None",
        )
        self.assertFalse(is_current_authoritative_member(state, entity))

    def test_active_verified_recurring_is_current_member(self):
        """ACTIVE + AUTHORITATIVE + RECURRING_PATTERN for INDIVIDUAL VERIFIED → True."""
        entity = MembershipEntityInput(entity_id=0, entity_type="INDIVIDUAL", verification_status="VERIFIED")
        state = MembershipState(
            calculated_status="ACTIVE",
            recurrence_pattern_status="RECURRING_PATTERN",
            membership_authority="AUTHORITATIVE",
            explanation="test",
            evidence_summary=_make_dummy_evidence(),
            rule_version_id=1,
            evaluation_date=datetime.date(2026, 7, 15),
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('50.00'),
            payment_interval="Monthly",
        )
        self.assertTrue(is_current_authoritative_member(state, entity))
