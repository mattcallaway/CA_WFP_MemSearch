"""Pure amendment disposition selector.

Zero ORM access. Accepts only immutable inputs, returns operative contribution IDs.
"""
from dataclasses import dataclass
from enum import Enum

class ReviewStatus(Enum):
    PENDING = 'PENDING'
    ACCEPTED = 'ACCEPTED'
    REJECTED = 'REJECTED'

@dataclass(frozen=True)
class AmendmentRecord:
    amendment_id: int
    original_contribution_id: int
    replacement_contribution_id: int
    review_status: str  # PENDING, ACCEPTED, REJECTED

def select_operative_contributions(
    all_contribution_ids: tuple[int, ...],
    amendments: tuple[AmendmentRecord, ...],
) -> tuple[int, ...]:
    """Returns only operative contribution IDs, honoring amendment dispositions.
    
    Disposition policy:
    - PENDING: Original operative; replacement excluded from totals/recurrence
    - ACCEPTED: Replacement operative; original superseded (excluded)
    - REJECTED: Original operative; replacement excluded
    
    Args:
        all_contribution_ids: All contribution IDs assigned to an entity.
        amendments: Amendment relationships involving these contributions.
    
    Returns:
        Tuple of operative contribution IDs.
    """
    excluded = set()
    
    for a in amendments:
        if a.review_status == 'PENDING':
            # Original stays operative, replacement excluded
            excluded.add(a.replacement_contribution_id)
        elif a.review_status == 'ACCEPTED':
            # Replacement is operative, original superseded
            excluded.add(a.original_contribution_id)
        elif a.review_status == 'REJECTED':
            # Original stays operative, replacement excluded
            excluded.add(a.replacement_contribution_id)
    
    return tuple(cid for cid in all_contribution_ids if cid not in excluded)


def validate_amendment_chain(
    amendments: tuple[AmendmentRecord, ...],
) -> list[str]:
    """Validates amendment chain for cycles and conflicts.
    
    Returns list of error messages (empty if valid).
    """
    errors = []
    
    # Check for self-referential
    for a in amendments:
        if a.original_contribution_id == a.replacement_contribution_id:
            errors.append(f"Amendment {a.amendment_id}: self-referential")
    
    # Check for cycles: build directed graph original -> replacement
    graph = {}
    for a in amendments:
        graph.setdefault(a.original_contribution_id, []).append(a.replacement_contribution_id)
    
    def has_cycle(start, visited=None, path=None):
        if visited is None:
            visited = set()
        if path is None:
            path = set()
        visited.add(start)
        path.add(start)
        for neighbor in graph.get(start, []):
            if neighbor in path:
                return True
            if neighbor not in visited:
                if has_cycle(neighbor, visited, path):
                    return True
        path.discard(start)
        return False
    
    for node in graph:
        if has_cycle(node):
            errors.append(f"Cyclic amendment chain detected involving contribution {node}")
            break
    
    # Check for multiple accepted replacements for same original
    accepted_per_original = {}
    for a in amendments:
        if a.review_status == 'ACCEPTED':
            accepted_per_original.setdefault(a.original_contribution_id, []).append(a.amendment_id)
    
    for orig_id, amendment_ids in accepted_per_original.items():
        if len(amendment_ids) > 1:
            errors.append(
                f"Multiple accepted amendments for contribution {orig_id}: {amendment_ids}"
            )
    
    return errors
