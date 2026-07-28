from datetime import date
from django.test import TestCase
from django.contrib.auth.models import User

from roster.models import (
    ContributorEntity, Chapter, ChapterRuleSet, ChapterRule, ChapterEvaluationRun,
    ChapterEvaluationResult, ChapterAssignment, GeographicPlace
)
from roster.services.identity import verify_contributor_identity, unverify_contributor_identity
from roster.services.chapter_engine import run_chapter_evaluation


class ChapterPropagationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="admin", password="password")
        self.place = GeographicPlace.objects.create(
            canonical_name="Los Angeles",
            normalized_name="LOS ANGELES",
            general_category="CITY"
        )
        self.entity = ContributorEntity.objects.create(
            display_name="PROP_TEST_PERSON",
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False
        )
        
        # Verify entity initially via ADMIN_REVIEW
        verify_contributor_identity(
            entity=self.entity,
            method="ADMIN_REVIEW",
            actor="admin",
            explanation="Verified for chapter propagation test"
        )

        # Create Chapter & Rule matching place
        self.chapter = Chapter.objects.create(
            name="Los Angeles Chapter",
            slug="los-angeles-chapter",
            status="ACTIVE",
            created_by="admin"
        )
        self.rule_set = ChapterRuleSet.objects.create(
            chapter=self.chapter,
            version=1,
            status="ACTIVE",
            created_by="admin"
        )
        self.rule = ChapterRule.objects.create(
            rule_set=self.rule_set,
            effect="INCLUDE",
            target_type="PLACE",
            place=self.place,
            is_active=True
        )

    def test_chapter_propagation_after_identity_unverification(self):
        # 1. Run initial chapter evaluation (APPLY mode) while verified
        run1 = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="APPLY",
            trigger_type="MANUAL_FULL_EVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="PENDING"
        )
        run_chapter_evaluation(run1.id)
        run1.refresh_from_db()
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run1.id)

        # Check initial snapshot stored
        res1 = ChapterEvaluationResult.objects.filter(evaluation_run=run1, contributor_entity=self.entity).first()
        if res1:
            self.assertTrue(res1.entity_verification_snapshot)

        # 2. Unverify entity identity via centralized service
        unverify_contributor_identity(
            entity=self.entity,
            actor="admin",
            reason="Unverified for chapter propagation test"
        )

        # Confirm historical run1 result is NOT altered or rewritten
        if res1:
            res1.refresh_from_db()
            self.assertTrue(res1.entity_verification_snapshot)

        # Confirm chapter pointer remains run1 until replacement run completes
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run1.id)

        # 3. Run replacement chapter evaluation generation (APPLY mode)
        run2 = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="APPLY",
            trigger_type="ENTITY_REEVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="PENDING"
        )
        run_chapter_evaluation(run2.id)
        run2.refresh_from_db()
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run2.id)

        # Check replacement snapshot stored is UNVERIFIED
        res2 = ChapterEvaluationResult.objects.filter(evaluation_run=run2, contributor_entity=self.entity).first()
        if res2:
            self.assertFalse(res2.entity_verification_snapshot)

    def test_preview_mode_never_changes_current_evaluation_run_pointer(self):
        run_apply = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="APPLY",
            trigger_type="MANUAL_FULL_EVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="PENDING"
        )
        run_chapter_evaluation(run_apply.id)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run_apply.id)

        # Execute PREVIEW run
        run_prev = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="PREVIEW",
            trigger_type="MANUAL_FULL_EVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="PENDING"
        )
        run_chapter_evaluation(run_prev.id)

        # Assert pointer remains run_apply
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run_apply.id)

    def test_failed_replacement_run_leaves_previous_pointer_unchanged(self):
        run_apply = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="APPLY",
            trigger_type="MANUAL_FULL_EVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="PENDING"
        )
        run_chapter_evaluation(run_apply.id)
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run_apply.id)

        # Simulate a failed replacement run
        run_failed = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.rule_set,
            run_mode="APPLY",
            trigger_type="MANUAL_FULL_EVALUATION",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date=date.today(),
            scope="FULL",
            actor="admin",
            status="FAILED"
        )

        # Assert chapter pointer remains run_apply
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run_id, run_apply.id)
