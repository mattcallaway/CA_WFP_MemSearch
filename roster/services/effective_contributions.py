"""Centralized effective-contribution service.

All financial totals and recurrence calculations must use this service
rather than raw contribution lists, to honor amendment dispositions.

Architecture:
- Pure selector: select_operative_contributions() in amendment_disposition.py
- ORM loader: load_amendment_records() loads from database
- ORM service: get_effective_contributions() combines both
"""
from roster.models import Contribution, AmendmentRelationship, ContributionClusterAssignment
from roster.services.amendment_disposition import (
    AmendmentRecord,
    select_operative_contributions,
)


def load_amendment_records(contribution_ids: list[int]) -> tuple['AmendmentRecord', ...]:
    """Load amendment relationships from ORM for given contribution IDs.
    
    Returns immutable AmendmentRecord tuples suitable for the pure selector.
    """
    if not contribution_ids:
        return ()
    
    amendments = AmendmentRelationship.objects.filter(
        original_contribution_id__in=contribution_ids
    ) | AmendmentRelationship.objects.filter(
        replacement_contribution_id__in=contribution_ids
    )
    
    return tuple(
        AmendmentRecord(
            amendment_id=a.id,
            original_contribution_id=a.original_contribution_id,
            replacement_contribution_id=a.replacement_contribution_id,
            review_status=a.review_status,
        )
        for a in amendments
    )


def get_effective_contributions(cluster):
    """Returns only operative contributions for financial totals and recurrence.
    
    Uses the pure amendment disposition selector via ORM-loaded amendment records.
    
    Excludes:
    - Contributions that are replacement_contributions in PENDING amendments
    - Contributions that are original_contributions in ACCEPTED amendments
    - Contributions that are replacement_contributions in REJECTED amendments
    """
    # Get all active contributions for this cluster
    assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster=cluster,
        is_active=True,
        contribution__raw_contribution__import_batch__status='COMPLETED',
    ).select_related('contribution')
    
    contribution_ids = [a.contribution_id for a in assignments]
    
    if not contribution_ids:
        return Contribution.objects.none()
    
    # Load amendment records and select operative contributions
    amendment_records = load_amendment_records(contribution_ids)
    operative_ids = select_operative_contributions(
        all_contribution_ids=tuple(contribution_ids),
        amendments=amendment_records,
    )
    
    return Contribution.objects.filter(id__in=operative_ids).order_by('transaction_date')


def get_effective_contribution_ids_for_entity(entity_id: int) -> list[int]:
    """Returns operative contribution IDs for an entity across all clusters."""
    assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster__contributor_entity_id=entity_id,
        is_active=True,
        contribution__raw_contribution__import_batch__status='COMPLETED',
    ).select_related('contribution')
    
    contribution_ids = [a.contribution_id for a in assignments]
    
    if not contribution_ids:
        return []
    
    amendment_records = load_amendment_records(contribution_ids)
    operative_ids = select_operative_contributions(
        all_contribution_ids=tuple(contribution_ids),
        amendments=amendment_records,
    )
    
    return list(operative_ids)
