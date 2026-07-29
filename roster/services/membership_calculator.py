"""
Pure Membership Calculator

This module implements the membership calculation logic as pure functions.
It has ZERO ORM access, ZERO database reads, and uses deeply immutable inputs
and outputs. All state calculations are purely deterministic based on the
provided frozen dataclasses.

Purity guarantees:
- calculate_membership_state() performs zero database queries
- All inputs and outputs are frozen dataclasses
- Contribution sorting is deterministic (transaction_date, amount, source_record_id)
- All amounts use Decimal, never float
- Explanations contain no PII (names, addresses, emails, phone, employer, occupation)
- Evidence summaries contain only counts and intervals

membership_authority = whether the calculated classification is sufficiently
evidenced to be used as authoritative. It does NOT mean the entity is
necessarily a current member.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

@dataclass(frozen=True)
class MembershipEntityInput:
    entity_id: int
    entity_type: str            # INDIVIDUAL, ORGANIZATION, JOINT, UNKNOWN
    verification_status: str    # VERIFIED, UNVERIFIED


@dataclass(frozen=True)
class MembershipContributionInput:
    transaction_date: date
    amount: Decimal
    transaction_type: str       # CONTRIBUTION, REFUND, REVERSAL, ADJUSTMENT
    source_record_id: int


@dataclass(frozen=True)
class MembershipRuleInput:
    rule_version_id: int
    monthly_interval_min: int
    monthly_interval_max: int
    active_grace_period: int
    min_recurring_payments: int
    skip_payment_allowed: bool


@dataclass(frozen=True)
class CoverageInput:
    through_date: date | None
    is_complete: bool


@dataclass(frozen=True)
class MembershipEvidenceSummary:
    contribution_count: int
    positive_contribution_count: int
    refund_count: int
    interval_days: tuple[int, ...]
    coverage_age_days: int | None
    coverage_is_complete: bool


@dataclass(frozen=True)
class MembershipState:
    calculated_status: str
    recurrence_pattern_status: str
    membership_authority: str
    explanation: str
    evidence_summary: MembershipEvidenceSummary
    rule_version_id: int
    evaluation_date: date
    chapter_reevaluation_required: bool
    recurring_amount: Decimal
    payment_interval: str


def calculate_net_payments(contributions: tuple[MembershipContributionInput, ...]) -> tuple[dict, ...]:
    """
    - Group contributions by date
    - CONTRIBUTION type -> positive
    - All other types -> negative (use abs(amount))
    - Net per date = positives - negatives
    - Only keep dates with net > 0
    - Sort chronologically
    """
    date_sums = {}
    for c in contributions:
        dt = c.transaction_date
        # CONTRIBUTION type -> positive, all other types -> negative
        val = c.amount if c.transaction_type == "CONTRIBUTION" else -abs(c.amount)
        if dt not in date_sums:
            date_sums[dt] = Decimal('0.00')
        date_sums[dt] += val
    
    net_list = []
    for dt, net_amt in date_sums.items():
        if net_amt > Decimal('0.00'):
            net_list.append({"transaction_date": dt, "amount": net_amt})
            
    net_list.sort(key=lambda x: x["transaction_date"])
    return tuple(net_list)


def calculate_membership_state(
    *, 
    entity: MembershipEntityInput, 
    contributions: tuple[MembershipContributionInput, ...], 
    rule: MembershipRuleInput, 
    coverage: CoverageInput, 
    evaluation_date: date
) -> MembershipState:
    
    # 1. Non-individual entities
    if entity.entity_type in ("ORGANIZATION", "JOINT", "UNKNOWN"):
        return MembershipState(
            calculated_status='UNKNOWN',
            recurrence_pattern_status='INSUFFICIENT_HISTORY',
            membership_authority='INELIGIBLE',
            explanation=f"Entity type {entity.entity_type} is ineligible for membership.",
            evidence_summary=MembershipEvidenceSummary(
                contribution_count=len(contributions),
                positive_contribution_count=0,
                refund_count=sum(1 for c in contributions if c.transaction_type != "CONTRIBUTION"),
                interval_days=(),
                coverage_age_days=None,
                coverage_is_complete=coverage.is_complete
            ),
            rule_version_id=rule.rule_version_id,
            evaluation_date=evaluation_date,
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval='None'
        )
        
    net_payments = calculate_net_payments(contributions)
    num_net_payments = len(net_payments)
    
    refund_cnt = sum(1 for c in contributions if c.transaction_type != "CONTRIBUTION")
    coverage_age_days = (evaluation_date - coverage.through_date).days if coverage.through_date else None
    
    # coverage staleness
    is_stale_coverage = False
    if coverage_age_days is not None and coverage_age_days > 60:
        is_stale_coverage = True
    if not coverage.is_complete:
        is_stale_coverage = True
        
    # 2. Individual with zero contributions
    if num_net_payments == 0:
        return MembershipState(
            calculated_status='UNKNOWN',
            recurrence_pattern_status='INSUFFICIENT_HISTORY',
            membership_authority='PROVISIONAL',
            explanation="No net positive contributions found.",
            evidence_summary=MembershipEvidenceSummary(
                contribution_count=len(contributions),
                positive_contribution_count=0,
                refund_count=refund_cnt,
                interval_days=(),
                coverage_age_days=coverage_age_days,
                coverage_is_complete=coverage.is_complete
            ),
            rule_version_id=rule.rule_version_id,
            evaluation_date=evaluation_date,
            chapter_reevaluation_required=True,
            recurring_amount=Decimal('0.00'),
            payment_interval='None'
        )

    # Intervals calculation for >1 contributions
    intervals = []
    if num_net_payments > 1:
        for i in range(1, num_net_payments):
            intervals.append((net_payments[i]["transaction_date"] - net_payments[i-1]["transaction_date"]).days)
            
    evidence = MembershipEvidenceSummary(
        contribution_count=len(contributions),
        positive_contribution_count=num_net_payments,
        refund_count=refund_cnt,
        interval_days=tuple(intervals),
        coverage_age_days=coverage_age_days,
        coverage_is_complete=coverage.is_complete
    )
    
    # 3. Individual with one contribution
    if num_net_payments == 1:
        if entity.verification_status == 'VERIFIED':
            if not is_stale_coverage:
                return MembershipState(
                    calculated_status='ONE_TIME',
                    recurrence_pattern_status='NO_RECURRING_PATTERN',
                    membership_authority='AUTHORITATIVE',
                    explanation="Single verified contribution with current coverage.",
                    evidence_summary=evidence,
                    rule_version_id=rule.rule_version_id,
                    evaluation_date=evaluation_date,
                    chapter_reevaluation_required=True,
                    recurring_amount=Decimal('0.00'),
                    payment_interval='None'
                )
            else:
                return MembershipState(
                    calculated_status='DATASET_TOO_STALE',
                    recurrence_pattern_status='NO_RECURRING_PATTERN',
                    membership_authority='AUTHORITATIVE',
                    explanation="Single verified contribution but dataset is stale.",
                    evidence_summary=evidence,
                    rule_version_id=rule.rule_version_id,
                    evaluation_date=evaluation_date,
                    chapter_reevaluation_required=True,
                    recurring_amount=Decimal('0.00'),
                    payment_interval='None'
                )
        else:
            return MembershipState(
                calculated_status='PROVISIONAL',
                recurrence_pattern_status='INSUFFICIENT_HISTORY',
                membership_authority='PROVISIONAL',
                explanation="Single unverified contribution.",
                evidence_summary=evidence,
                rule_version_id=rule.rule_version_id,
                evaluation_date=evaluation_date,
                chapter_reevaluation_required=True,
                recurring_amount=Decimal('0.00'),
                payment_interval='None'
            )
            
    # 4 & 5. Multiple contributions
    valid_intervals = 0
    skipped_intervals = 0
    
    for diff in intervals:
        if rule.monthly_interval_min <= diff <= rule.monthly_interval_max:
            valid_intervals += 1
        elif rule.skip_payment_allowed and 50 <= diff <= 80:
            skipped_intervals += 1
            
    is_recurring = (valid_intervals + skipped_intervals) >= (rule.min_recurring_payments - 1)
    
    recent_payment = net_payments[-1]["transaction_date"]
    recent_days = (evaluation_date - recent_payment).days
    is_recent = recent_days <= rule.active_grace_period
    
    if is_recurring:
        pattern_status = 'RECURRING_PATTERN'
        # Recurring amount: average of last three net-positive payment dates
        last_three = net_payments[-3:]
        recurring_amount = sum(p["amount"] for p in last_three) / Decimal(len(last_three)) if last_three else Decimal('0.00')
        payment_interval = 'Monthly'
        if entity.verification_status == 'VERIFIED':
            authority = 'AUTHORITATIVE'
            if is_recent:
                status = 'ACTIVE'
                explanation = "Verified recurring donor with recent payment."
            else:
                if is_stale_coverage:
                    status = 'PREVIOUSLY_RECURRING'
                    explanation = "Verified recurring donor, no recent payment, stale coverage."
                else:
                    status = 'LAPSED'
                    explanation = "Verified recurring donor, no recent payment, current coverage."
        else:
            authority = 'PROVISIONAL'
            status = 'PROVISIONAL'
            explanation = "Unverified recurring donor."
    else:
        pattern_status = 'IRREGULAR_PATTERN'
        # Non-recurring: recurring_amount and payment_interval are not applicable.
        # Only RECURRING_PATTERN states may carry these fields.
        recurring_amount = Decimal('0.00')
        payment_interval = 'None'
        if entity.verification_status == 'VERIFIED':
            authority = 'AUTHORITATIVE'
            status = 'INSUFFICIENT_HISTORY'
            explanation = "Verified irregular donor."
        else:
            authority = 'PROVISIONAL'
            status = 'UNKNOWN'
            explanation = "Unverified irregular donor."
            
    return MembershipState(
        calculated_status=status,
        recurrence_pattern_status=pattern_status,
        membership_authority=authority,
        explanation=explanation,
        evidence_summary=evidence,
        rule_version_id=rule.rule_version_id,
        evaluation_date=evaluation_date,
        chapter_reevaluation_required=True,
        recurring_amount=recurring_amount,
        payment_interval=payment_interval
    )


def build_calculator_inputs(entity, contributions_qs, rule, coverage):
    """
    ORM adapter: converts Django model instances to immutable calculator inputs.

    This is the ONLY function in this module that touches Django ORM objects.
    It validates that the database compatibility field (is_verified) agrees
    with verification_status before constructing calculator inputs.

    Raises:
        ValueError: If entity.is_verified disagrees with
            (entity.verification_status == 'VERIFIED' and
             entity.verification_method != 'NONE').
    """
    is_verified_status = (
        entity.verification_status == 'VERIFIED'
        and entity.verification_method != 'NONE'
    )
    if entity.is_verified != is_verified_status:
        raise ValueError(
            f"Entity {entity.id}: is_verified={entity.is_verified} disagrees with "
            f"verification_status={entity.verification_status}, "
            f"verification_method={entity.verification_method}. "
            f"Expected is_verified={is_verified_status}."
        )

    entity_input = MembershipEntityInput(
        entity_id=entity.id,
        entity_type=entity.entity_type,
        verification_status=entity.verification_status,
    )

    contribs = []
    for c in sorted(
        list(contributions_qs),
        key=lambda x: (x.transaction_date, x.amount, x.id),
    ):
        contribs.append(MembershipContributionInput(
            transaction_date=c.transaction_date,
            amount=Decimal(str(c.amount)),
            transaction_type=c.transaction_type,
            source_record_id=c.id,
        ))

    rule_input = MembershipRuleInput(
        rule_version_id=rule.id,
        monthly_interval_min=rule.monthly_interval_min,
        monthly_interval_max=rule.monthly_interval_max,
        active_grace_period=rule.active_grace_period,
        min_recurring_payments=rule.min_recurring_payments,
        skip_payment_allowed=rule.skip_payment_allowed,
    )

    # DatasetCoverageMetadata uses coverage_complete_through and coverage_status
    is_complete = False
    through_date = None
    if coverage:
        through_date = coverage.coverage_complete_through
        is_complete = coverage.coverage_status in (
            'CONFIRMED_COMPLETE', 'APPARENTLY_CONTINUOUS',
        )

    coverage_input = CoverageInput(
        through_date=through_date,
        is_complete=is_complete,
    )

    return entity_input, tuple(contribs), rule_input, coverage_input


# ---------------------------------------------------------------------------
# Membership classification predicates
#
# All predicates receive BOTH MembershipState AND MembershipEntityInput.
# They are the single source of truth for downstream code (chapter engine,
# directories, exports, overlap reporting). No downstream code should check
# membership_authority or calculated_status alone.
# ---------------------------------------------------------------------------


def is_current_authoritative_member(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """True only for a verified individual with active recurring membership.

    ONE_TIME + AUTHORITATIVE is NOT a current member.
    DATASET_TOO_STALE + AUTHORITATIVE is NOT a current member.
    INSUFFICIENT_HISTORY + AUTHORITATIVE is NOT a current member.
    Organizations, joint, and unknown types cannot be current members.
    """
    return (
        entity.entity_type == "INDIVIDUAL"
        and entity.verification_status == "VERIFIED"
        and state.calculated_status == "ACTIVE"
        and state.recurrence_pattern_status == "RECURRING_PATTERN"
        and state.membership_authority == "AUTHORITATIVE"
    )


def is_provisional_possible_member(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """True only for an unverified individual with a recurring pattern.

    An unverified person with one contribution and INSUFFICIENT_HISTORY
    does NOT satisfy this predicate (S09 must return False).
    """
    return (
        entity.entity_type == "INDIVIDUAL"
        and entity.verification_status == "UNVERIFIED"
        and state.calculated_status == "PROVISIONAL"
        and state.recurrence_pattern_status == "RECURRING_PATTERN"
        and state.membership_authority == "PROVISIONAL"
    )


def is_previously_recurring_donor(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """Verified individual whose recurring pattern has gone stale."""
    return (
        entity.entity_type == "INDIVIDUAL"
        and state.calculated_status == "PREVIOUSLY_RECURRING"
    )


def is_lapsed_donor(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """Verified recurring donor confirmed lapsed with current coverage."""
    return (
        entity.entity_type == "INDIVIDUAL"
        and state.calculated_status == "LAPSED"
    )


def is_one_time_donor(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """Verified individual with a single contribution and current coverage."""
    return (
        entity.entity_type == "INDIVIDUAL"
        and state.calculated_status == "ONE_TIME"
    )


def is_ineligible_entity(
    state: MembershipState,
    entity: MembershipEntityInput,
) -> bool:
    """Non-individual entity type ineligible for membership assessment."""
    return state.membership_authority == "INELIGIBLE"
