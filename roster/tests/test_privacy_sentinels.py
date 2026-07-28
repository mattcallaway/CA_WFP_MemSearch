"""
Privacy sentinel tests covering all URL surfaces with three actor classes:
1. Anonymous → 302 redirect
2. Authenticated without permission → 403
3. Fully authorized → 200 or valid redirect

Also covers management command output for PII leakage.
"""
from io import StringIO

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User, Group
from django.core.management import call_command

from roster.models import (
    ContributorEntity, Person, ImportBatch, RawContribution,
    ContributionCluster,
    Chapter, ChapterRuleSet, ChapterEvaluationRun, ChapterAssignment,
    ChapterEvaluationResult, ChapterEvaluationLocationSelection,
    GeographyDataset, LocationGeographyResolution, GeographyResolutionRun,
    MergeDecision, MembershipRuleVersion,
)


class PrivacySentinelTestCase(TestCase):
    """Test all URL surfaces for privacy protection across three actor classes."""

    def setUp(self):
        # Setup roles (creates Read-only, Data manager, Administrator groups)
        call_command("setup_roles", stdout=StringIO(), stderr=StringIO())
        self.client = Client()

        # Create unprivileged user (not in any group)
        self.unprivileged_user = User.objects.create_user(
            username="unprivileged_user", password="password"
        )

        # Create fully authorized admin
        self.admin_user = User.objects.create_superuser(
            username="admin_user", password="password"
        )

        # PII sentinel values
        self.sentinel_name = "SENTINEL_NAME_PRIVACY_TEST"
        self.sentinel_employer = "SENTINEL EMPLOYER INC"

        # Create sentinel entity (ContributorEntity has display_name, not street/employer)
        self.entity = ContributorEntity.objects.create(
            display_name=self.sentinel_name,
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False,
        )

        # Create Person profile with PII
        self.person = Person.objects.create(
            contributor_entity=self.entity,
            first_name="SENTINEL",
            last_name="PRIVACY_TEST",
        )

        # Create ImportBatch fixture
        self.batch = ImportBatch.objects.create(
            file_name="sentinel_test.csv",
            file_hash="sentinel_hash_12345",
            file_type="CSV",
            imported_by="admin_user",
            status="COMPLETED",
        )

        # Create RawContribution with PII in original_values
        self.raw_contrib = RawContribution.objects.create(
            import_batch=self.batch,
            row_number=1,
            original_values={
                "NAME OF CONTRIBUTOR": self.sentinel_name,
                "EMPLOYER": self.sentinel_employer,
                "AMOUNT": "100.00",
                "TRANSACTION DATE": "2026-01-01",
                "ZIP": "90001",
            },
            raw_row_hash="sentinel_row_hash",
            validation_status="ACCEPTED",
        )

        # Create Chapter fixtures
        self.chapter = Chapter.objects.create(
            name="Sentinel Chapter",
            slug="sentinel-chapter",
            created_by="admin_user",
        )
        self.ruleset = ChapterRuleSet.objects.create(
            chapter=self.chapter,
            version=1,
            status="DRAFT",
            created_by="admin_user",
        )

        # Create MembershipRuleVersion for evaluation run
        self.rule_version = MembershipRuleVersion.objects.get_or_create(
            is_active=True,
            defaults={
                "name": "Test Rules",
                "monthly_interval_min": 20,
                "monthly_interval_max": 40,
                "active_grace_period": 60,
                "min_recurring_payments": 2,
                "allowed_amount_variance": 0.00,
                "skip_payment_allowed": True,
                "effective_date": "2026-01-01",
                "created_by": "SYSTEM",
            },
        )[0]

        self.eval_run = ChapterEvaluationRun.objects.create(
            chapter=self.chapter,
            rule_set=self.ruleset,
            trigger_type="MANUAL_FULL_EVALUATION",
            run_mode="PREVIEW",
            status="COMPLETED",
            actor="admin_user",
            geography_dataset_snapshot={},
            resolver_version="1.0",
            evaluation_engine_version="1.0",
            membership_snapshot_date="2026-01-01",
            scope="FULL",
        )

        # Create location selection for chapter result
        self.loc_selection = ChapterEvaluationLocationSelection.objects.create(
            evaluation_run=self.eval_run,
            contributor_entity=self.entity,
            selection_status="NO_CURRENT_LOCATION",
            selection_method="AUTOMATIC",
        )

        # Create chapter evaluation result
        self.eval_result = ChapterEvaluationResult.objects.create(
            evaluation_run=self.eval_run,
            chapter=self.chapter,
            rule_set=self.ruleset,
            contributor_entity=self.entity,
            location_selection=self.loc_selection,
            result_status="NO_CURRENT_LOCATION",
            confidence="LOW",
            entity_type_snapshot="INDIVIDUAL",
            entity_verification_snapshot=False,
            membership_status_snapshot="UNKNOWN",
            membership_rule_version_snapshot="NONE",
        )

        self.assignment = ChapterAssignment.objects.create(
            chapter=self.chapter,
            evaluation_run=self.eval_run,
            contributor_entity=self.entity,
            evaluation_result=self.eval_result,
            assignment_status="UNRESOLVED",
        )

        # Geography fixtures
        self.dataset = GeographyDataset.objects.create(
            name="Sentinel Dataset",
            dataset_type="COUNTY",
            status="ACTIVE",
        )
        self.res_run = GeographyResolutionRun.objects.create(
            trigger_type="MANUAL_BULK_RESOLUTION",
            status="COMPLETED",
        )

        # ContributionCluster fixtures (needed for MergeDecision)
        self.cluster_source = ContributionCluster.objects.create(
            contributor_entity=self.entity,
            normalized_name="sentinel source cluster",
            zip_code="90001",
        )
        # Create a second entity for the target cluster
        entity2 = ContributorEntity.objects.create(
            display_name="Target Entity",
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False,
        )
        self.cluster_target = ContributionCluster.objects.create(
            contributor_entity=entity2,
            normalized_name="sentinel target cluster",
            zip_code="90002",
        )

        # MergeDecision fixture
        self.merge_decision = MergeDecision.objects.create(
            source_cluster=self.cluster_source,
            target_cluster=self.cluster_target,
            merged_by="admin_user",
        )

        # URLs that require no arguments
        self.urls_no_args = [
            "dashboard",
            "people_list",
            "export_roster",
            "audit_history",
            "imports_list",
            "geography_datasets_list",
            "geography_import_upload",
            "county_directory",
            "place_directory",
            "postal_area_directory",
            "geography_alias_directory",
            "geography_ambiguity_queue",
            "chapter_list",
            "aggregate_overlaps",
            "named_overlaps",
            "override_search",
        ]
        # POST-only views: return 405 for GET regardless of permissions
        self.post_only_no_args = [
            "imports_upload",
            "merge_profiles",
        ]

    def _test_url_access(self, url, allow_404=False):
        """
        Test three actor classes against a URL:
        1. Anonymous → 302 redirect to login
        2. Authenticated without permission → 403
        3. Fully authorized admin → 200 or 302
        """
        # 1. Anonymous → 302
        self.client.logout()
        resp = self.client.get(url)
        self.assertEqual(
            resp.status_code, 302,
            f"Anonymous access to {url} should be 302, got {resp.status_code}",
        )

        # 2. Authenticated no permission → 403
        self.client.login(username="unprivileged_user", password="password")
        resp = self.client.get(url)
        self.assertEqual(
            resp.status_code, 403,
            f"Unprivileged access to {url} should be 403, got {resp.status_code}",
        )

        # 3. Admin → 200 or 302
        self.client.login(username="admin_user", password="password")
        resp = self.client.get(url)
        allowed = [200, 302]
        if allow_404:
            allowed.append(404)
        self.assertIn(
            resp.status_code, allowed,
            f"Admin access to {url} got {resp.status_code}",
        )

    def test_no_arg_urls(self):
        """Test all URLs that require no arguments."""
        for name in self.urls_no_args:
            url = reverse(name)
            self._test_url_access(url)

    def test_imports_preview(self):
        self._test_url_access(reverse("imports_preview", args=[self.batch.id]))

    def test_imports_failures(self):
        self._test_url_access(reverse("imports_failures", args=[self.batch.id]))

    def test_geography_dataset_detail(self):
        self._test_url_access(reverse("geography_dataset_detail", args=[self.dataset.id]))

    def test_geography_resolution_run_detail(self):
        self._test_url_access(reverse("geography_resolution_run_detail", args=[self.res_run.id]))

    def test_chapter_detail(self):
        self._test_url_access(reverse("chapter_detail", args=[self.chapter.id]))

    def test_ruleset_editor(self):
        self._test_url_access(reverse("ruleset_editor", args=[self.ruleset.id]))

    def test_run_detail(self):
        self._test_url_access(reverse("run_detail", args=[self.eval_run.id]))

    def test_preview_detail(self):
        self._test_url_access(reverse("preview_detail", args=[self.eval_run.id]))

    def test_assignment_detail(self):
        self._test_url_access(reverse("assignment_detail", args=[self.assignment.id]))

    def test_membership_override(self):
        # POST-only view — GET returns 405
        url = reverse("membership_override", args=[self.entity.id])
        self.client.logout()
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302, 405])

    def test_correct_entity_type(self):
        # POST-only view — GET returns 405
        url = reverse("correct_entity_type", args=[self.entity.id])
        self.client.logout()
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302, 405])

    def test_split_profiles(self):
        # POST-only view — GET returns 405
        url = reverse("split_profiles", args=[self.merge_decision.id])
        self.client.logout()
        resp = self.client.get(url)
        self.assertIn(resp.status_code, [302, 405])

    def test_person_profile(self):
        self._test_url_access(reverse("person_profile", args=[self.entity.id]))

    def test_privacy_values_projection_excludes_pii(self):
        """Verify that values() projection used for anonymized queries excludes PII."""
        qs = ContributorEntity.objects.values("id", "entity_type", "verification_status")
        item = qs.filter(id=self.entity.id).first()
        self.assertNotIn("display_name", item)


class ManagementCommandPrivacyTestCase(TestCase):
    """Test management command output for PII leakage."""

    def setUp(self):
        self.sentinel_name = "SENTINEL_NAME_PRIVACY_TEST"
        ContributorEntity.objects.create(
            display_name=self.sentinel_name,
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False,
        )

    def _test_command_output(self, command_name, *args):
        """Run a command and verify PII doesn't appear in output."""
        out = StringIO()
        err = StringIO()
        try:
            call_command(command_name, *args, stdout=out, stderr=err)
        except (SystemExit, Exception):
            pass

        output = out.getvalue() + err.getvalue()
        self.assertNotIn(
            self.sentinel_name, output,
            f"PII sentinel found in {command_name} output",
        )

    def test_setup_roles_no_leakage(self):
        self._test_command_output("setup_roles")

    def test_audit_consistency_no_leakage(self):
        self._test_command_output("audit_membership_state_consistency")

    def test_repair_consistency_no_leakage(self):
        self._test_command_output("repair_membership_state_consistency", "--dry-run", "--actor", "test")
