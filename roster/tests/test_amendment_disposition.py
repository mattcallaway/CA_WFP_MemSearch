from django.test import TestCase
from roster.services.amendment_disposition import (
    AmendmentRecord,
    select_operative_contributions,
    validate_amendment_chain,
)


class PureDispositionSelectorTests(TestCase):
    """Tests for the pure amendment disposition selector."""
    
    def test_no_amendments(self):
        """All contributions pass through when no amendments exist."""
        ids = (1, 2, 3)
        result = select_operative_contributions(ids, ())
        self.assertEqual(result, (1, 2, 3))
    
    def test_pending_excludes_replacement(self):
        """PENDING: original operative, replacement excluded."""
        ids = (1, 2, 3)
        amendments = (AmendmentRecord(10, 1, 2, 'PENDING'),)
        result = select_operative_contributions(ids, amendments)
        self.assertIn(1, result)  # Original stays
        self.assertNotIn(2, result)  # Replacement excluded
        self.assertIn(3, result)  # Unrelated stays
    
    def test_accepted_excludes_original(self):
        """ACCEPTED: replacement operative, original superseded."""
        ids = (1, 2, 3)
        amendments = (AmendmentRecord(10, 1, 2, 'ACCEPTED'),)
        result = select_operative_contributions(ids, amendments)
        self.assertNotIn(1, result)  # Original superseded
        self.assertIn(2, result)  # Replacement operative
        self.assertIn(3, result)  # Unrelated stays
    
    def test_rejected_excludes_replacement(self):
        """REJECTED: original operative, replacement excluded."""
        ids = (1, 2, 3)
        amendments = (AmendmentRecord(10, 1, 2, 'REJECTED'),)
        result = select_operative_contributions(ids, amendments)
        self.assertIn(1, result)  # Original stays
        self.assertNotIn(2, result)  # Replacement excluded
        self.assertIn(3, result)  # Unrelated stays
    
    def test_amendment_chain(self):
        """Chain: A amended by B (accepted), B amended by C (pending)."""
        ids = (1, 2, 3, 4)
        amendments = (
            AmendmentRecord(10, 1, 2, 'ACCEPTED'),  # B replaces A
            AmendmentRecord(11, 2, 3, 'PENDING'),    # C proposed for B
        )
        result = select_operative_contributions(ids, amendments)
        self.assertNotIn(1, result)  # A superseded by accepted B
        self.assertIn(2, result)     # B still operative (C is pending)
        self.assertNotIn(3, result)  # C excluded (pending replacement)
        self.assertIn(4, result)     # Unrelated
    
    def test_decision_reversal(self):
        """If an accepted amendment is later rejected, original returns."""
        ids = (1, 2)
        # After reversal, status is now REJECTED
        amendments = (AmendmentRecord(10, 1, 2, 'REJECTED'),)
        result = select_operative_contributions(ids, amendments)
        self.assertIn(1, result)
        self.assertNotIn(2, result)
    
    def test_multiple_proposed_replacements(self):
        """Multiple pending replacements for same original."""
        ids = (1, 2, 3)
        amendments = (
            AmendmentRecord(10, 1, 2, 'PENDING'),
            AmendmentRecord(11, 1, 3, 'PENDING'),
        )
        result = select_operative_contributions(ids, amendments)
        self.assertIn(1, result)     # Original stays
        self.assertNotIn(2, result)  # Both replacements excluded
        self.assertNotIn(3, result)
    
    def test_no_double_counting_financial(self):
        """Accepted amendment: only replacement counted."""
        ids = (1, 2)
        amendments = (AmendmentRecord(10, 1, 2, 'ACCEPTED'),)
        result = select_operative_contributions(ids, amendments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], 2)
    
    def test_no_double_counting_recurrence(self):
        """Pending amendment: only original counted for recurrence."""
        ids = (1, 2)
        amendments = (AmendmentRecord(10, 1, 2, 'PENDING'),)
        result = select_operative_contributions(ids, amendments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], 1)
    
    def test_ambiguous_candidate_no_relationship(self):
        """When no amendment record exists, all contributions pass through."""
        ids = (1, 2, 3)
        result = select_operative_contributions(ids, ())
        self.assertEqual(len(result), 3)


class AmendmentChainValidationTests(TestCase):
    """Tests for amendment chain validation."""
    
    def test_valid_chain(self):
        amendments = (
            AmendmentRecord(10, 1, 2, 'ACCEPTED'),
            AmendmentRecord(11, 2, 3, 'PENDING'),
        )
        errors = validate_amendment_chain(amendments)
        self.assertEqual(errors, [])
    
    def test_self_referential_detected(self):
        amendments = (AmendmentRecord(10, 1, 1, 'PENDING'),)
        errors = validate_amendment_chain(amendments)
        self.assertTrue(any('self-referential' in e for e in errors))
    
    def test_cycle_detected(self):
        amendments = (
            AmendmentRecord(10, 1, 2, 'ACCEPTED'),
            AmendmentRecord(11, 2, 3, 'ACCEPTED'),
            AmendmentRecord(12, 3, 1, 'PENDING'),
        )
        errors = validate_amendment_chain(amendments)
        self.assertTrue(any('Cyclic' in e for e in errors))
    
    def test_multiple_accepted_for_same_original(self):
        amendments = (
            AmendmentRecord(10, 1, 2, 'ACCEPTED'),
            AmendmentRecord(11, 1, 3, 'ACCEPTED'),
        )
        errors = validate_amendment_chain(amendments)
        self.assertTrue(any('Multiple accepted' in e for e in errors))
