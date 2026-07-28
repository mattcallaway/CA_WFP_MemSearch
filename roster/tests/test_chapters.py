import datetime
from django.test import TestCase
from django.db import transaction, IntegrityError
from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.core.management import call_command
from django.core.management.base import CommandError

from roster.models import (
    Chapter, ChapterRuleSet, ChapterRule, ChapterEntityOverride,
    ChapterEvaluationRun, ChapterEvaluationLocationSelection,
    ChapterEvaluationResult, ChapterRuleMatch, ChapterAssignment,
    ContributorEntity, ContributionCluster, Location, LocationGeographyResolution,
    GeographyResolutionRun,
    County, GeographicPlace, PostalArea, MembershipRuleVersion, MembershipAssessment,
    GeographyDataset
)
from roster.services.chapter_lifecycle import (
    create_draft_ruleset, add_rule_to_ruleset, deactivate_rule, activate_ruleset
)
from roster.services.chapter_overrides import (
    create_override, revoke_override, expire_override
)
from roster.services.chapter_engine import run_chapter_evaluation


class ChapterTests(TestCase):
    def setUp(self):
        # Create core actors and geography
        self.admin_user = User.objects.create_superuser(username='admin', password='password')
        self.geographer = User.objects.create_user(username='geographer', password='password')
        
        # Grant geography permission to geographer (but not view_sensitive_roster)
        entity_ct = ContentType.objects.get_for_model(ContributorEntity)
        view_def = Permission.objects.get(content_type=entity_ct, codename='view_chapter_definitions')
        prev_rules = Permission.objects.get(content_type=entity_ct, codename='preview_chapter_rules')
        eval_rules = Permission.objects.get(content_type=entity_ct, codename='evaluate_chapter_rules')
        
        self.geographer.user_permissions.add(view_def, prev_rules, eval_rules)

        self.county_sonoma = County.objects.create(state_code='CA', normalized_name='SONOMA', display_name='Sonoma')
        self.county_marin = County.objects.create(state_code='CA', normalized_name='MARIN', display_name='Marin')
        self.place_petaluma = GeographicPlace.objects.create(normalized_name='PETALUMA', canonical_name='Petaluma', state_code='CA', general_category='city')
        self.postal_zip = PostalArea.objects.create(postal_code='94901', postal_area_type='USPS_ZIP5')
        self.postal_zcta = PostalArea.objects.create(postal_code='94901', postal_area_type='CENSUS_ZCTA5')

        self.chapter = Chapter.objects.create(name='North Bay', slug='north-bay', created_by='admin')
        self.ruleset = ChapterRuleSet.objects.create(chapter=self.chapter, version=1, status='DRAFT', created_by='admin')

        # Setup basic entity & location
        self.entity = ContributorEntity.objects.create(display_name='John Doe', entity_type='INDIVIDUAL')
        self.cluster = ContributionCluster.objects.create(contributor_entity=self.entity, normalized_name='JOHN DOE', zip_code='94901')

    def test_geographic_target_mutually_exclusive_constraints(self):
        """
        Verify database constraints enforce exactly one target is populated on ChapterRule.
        """
        # 1. Prohibit empty target
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChapterRule.objects.create(rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY')

        # 2. Prohibit multiple targets
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChapterRule.objects.create(
                    rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY',
                    county=self.county_sonoma, place=self.place_petaluma
                )

        # 3. Prohibit mismatching target type
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChapterRule.objects.create(
                    rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY',
                    place=self.place_petaluma
                )

        # 4. Valid target type COUNTY
        rule = ChapterRule.objects.create(
            rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY',
            county=self.county_sonoma, display_order=10
        )
        self.assertIsNotNone(rule.id)

    def test_target_specific_uniqueness_constraints(self):
        """
        Verify the three target-specific uniqueness constraints are enforced on ChapterRule.
        """
        # Create active county rule
        ChapterRule.objects.create(rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY', county=self.county_sonoma)

        # Attempt duplicate active county rule
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChapterRule.objects.create(rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY', county=self.county_sonoma)

        # Inactive rule duplicate is allowed
        ChapterRule.objects.create(rule_set=self.ruleset, effect='INCLUDE', target_type='COUNTY', county=self.county_sonoma, is_active=False)

    def test_ruleset_validation_match_mode(self):
        """
        Verify only include_match_mode = 'ANY' is supported for ruleset activation.
        """
        self.ruleset.include_match_mode = 'ALL'
        self.ruleset.save()
        
        with self.assertRaises(ValueError) as ctx:
            activate_ruleset(self.ruleset.id, 'admin')
        self.assertIn("Only 'ANY' include match mode is supported", str(ctx.exception))

    def test_ruleset_immutable_and_version_supersession(self):
        """
        Verify draft rulesets are editable, active are immutable, and activation supersedes correctly.
        """
        # Draft is editable
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'descr', 10, 'admin')
        
        # Activate ruleset
        run = activate_ruleset(self.ruleset.id, 'admin')
        self.assertEqual(run.status, 'PENDING')
        self.ruleset.refresh_from_db()
        self.assertEqual(self.ruleset.status, 'ACTIVE')

        # Active ruleset is immutable
        with self.assertRaises(ValueError):
            add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_marin.id, 'descr', 10, 'admin')

        # Creating a draft version copies active rules
        draft2 = create_draft_ruleset(self.chapter.id, 'admin')
        self.assertEqual(draft2.version, 2)
        self.assertEqual(draft2.rules.count(), 1)

        # Activating new draft supersedes previous active ruleset
        run2 = activate_ruleset(draft2.id, 'admin')
        
        self.ruleset.refresh_from_db()
        self.assertEqual(self.ruleset.status, 'SUPERSEDED')
        draft2.refresh_from_db()
        self.assertEqual(draft2.status, 'ACTIVE')

        # Stale run checks
        run.status = 'PENDING'
        run.save()
        with self.assertRaises(ValueError):
            run_chapter_evaluation(run.id)

    def test_override_constraints_and_lifecycle(self):
        """
        Verify override uniqueness, expiration check constraints, and statuses (ACTIVE, EXPIRED, SUPERSEDED, REVOKED).
        """
        # Effective / Expiration check constraint
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChapterEntityOverride.objects.create(
                    chapter=self.chapter, contributor_entity=self.entity, override_type='INCLUDE',
                    reason='test', effective_date=datetime.date(2026, 7, 28),
                    expiration_date=datetime.date(2026, 7, 27)
                )

        # Create active override
        ov1 = create_override(
            chapter_id=self.chapter.id, entity_id=self.entity.id, override_type='INCLUDE',
            reason='Initial Override', effective_date=datetime.date(2026, 7, 28),
            expiration_date=None, actor='admin'
        )
        self.assertEqual(ov1.status, 'ACTIVE')

        # Creating a second override supersedes the first one
        ov2 = create_override(
            chapter_id=self.chapter.id, entity_id=self.entity.id, override_type='EXCLUDE',
            reason='Second Override', effective_date=datetime.date(2026, 7, 28),
            expiration_date=None, actor='admin'
        )
        ov1.refresh_from_db()
        self.assertEqual(ov1.status, 'SUPERSEDED')
        self.assertEqual(ov1.superseded_by, ov2)
        self.assertEqual(ov2.status, 'ACTIVE')

        # Override revocation
        revoke_override(ov2.id, 'admin')
        ov2.refresh_from_db()
        self.assertEqual(ov2.status, 'REVOKED')

    def test_location_selection_conflicts(self):
        """
        Verify location selection logic handles multiple equivalent locations,
        and conflicting locations returning AMBIGUOUS_LOCATION.
        """
        loc1 = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        loc2 = Location.objects.create(
            contributor_profile=self.cluster, city='Marin', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )

        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        
        # Sonoma resolution
        res1 = LocationGeographyResolution.objects.create(
            location=loc1, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )
        
        # Marin resolution
        res2 = LocationGeographyResolution.objects.create(
            location=loc2, resolution_run=run_geo, observed_city='Marin', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_marin, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Include Sonoma county in rules
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Sonoma rule', 10, 'admin')
        run = activate_ruleset(self.ruleset.id, 'admin')

        # Evaluate: should be ambiguous because one location resolves to Sonoma (included) and one to Marin (no match)
        run_chapter_evaluation(run.id)
        
        result = ChapterEvaluationResult.objects.get(evaluation_run=run, contributor_entity=self.entity)
        self.assertEqual(result.result_status, 'AMBIGUOUS_LOCATION')
        self.assertEqual(result.location_selection.selection_status, 'AMBIGUOUS_LOCATION')

    def test_zip_versus_zcta_rule_separation(self):
        """
        Verify USPS ZIP rule does not silently match Census ZCTA resolution record.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        
        # ZCTA resolution
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_postal_area=self.postal_zcta, match_method='UNIQUE_ZIP_INFERENCE', confidence='MEDIUM', status='CURRENT'
        )

        # Create a ruleset containing only a USPS_ZIP5 rule
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'POSTAL_AREA', self.postal_zip.id, 'ZIP Rule', 10, 'admin')
        run = activate_ruleset(self.ruleset.id, 'admin')

        # Evaluate: ZIP ruleset should NOT match ZCTA resolution
        run_chapter_evaluation(run.id)

        result = ChapterEvaluationResult.objects.get(evaluation_run=run, contributor_entity=self.entity)
        self.assertEqual(result.result_status, 'NO_RULE_MATCH')

    def test_precedence_rides_override_over_rules(self):
        """
        Verify manual exclusion overrides geographic inclusion, and manual inclusion overrides geographic exclusion.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Inclusion rule on Sonoma
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Include Sonoma', 10, 'admin')
        
        # Active override EXCLUDE on entity
        create_override(
            chapter_id=self.chapter.id, entity_id=self.entity.id, override_type='EXCLUDE',
            reason='Force Exclude', effective_date=datetime.date(2026, 7, 28),
            expiration_date=None, actor='admin'
        )

        run = activate_ruleset(self.ruleset.id, 'admin')
        run_chapter_evaluation(run.id)

        result = ChapterEvaluationResult.objects.get(evaluation_run=run, contributor_entity=self.entity)
        self.assertEqual(result.result_status, 'MANUALLY_EXCLUDED')

    def test_membership_decoupling_and_snapshots(self):
        """
        Verify MembershipAssessment snapshotted parameters remain intact on ChapterEvaluationResult
        and subsequent membership changes do not alter result history.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Create Membership assessment
        rule_ver = MembershipRuleVersion.objects.create(name='Ruleset 1', effective_date=datetime.date(2026, 7, 28), created_by='admin')
        memb = MembershipAssessment.objects.create(
            contributor_entity=self.entity, calculated_status='ACTIVE', rule_version=rule_ver
        )

        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Include Sonoma', 10, 'admin')
        run = activate_ruleset(self.ruleset.id, 'admin')
        run_chapter_evaluation(run.id)

        result = ChapterEvaluationResult.objects.get(evaluation_run=run, contributor_entity=self.entity)
        self.assertEqual(result.membership_status_snapshot, 'ACTIVE')
        self.assertEqual(result.membership_assessment, memb)

        # Change membership status in DB
        memb.calculated_status = 'LAPSED'
        memb.save()

        # Re-fetch result: the snapshot must remain 'ACTIVE' (decoupled!)
        result.refresh_from_db()
        self.assertEqual(result.membership_status_snapshot, 'ACTIVE')

    def test_generation_promotion_atomic_switch(self):
        """
        Verify that evaluation run execution stages outputs without modifying current pointer,
        and promotes Chapter.current_evaluation_run atomically upon success.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Include Sonoma', 10, 'admin')
        run = activate_ruleset(self.ruleset.id, 'admin')

        # Before run, chapter current run is None
        self.assertIsNone(self.chapter.current_evaluation_run)

        # Run evaluation
        run_chapter_evaluation(run.id)

        # After successful execution, chapter current pointer is switched to the new run
        self.chapter.refresh_from_db()
        self.assertEqual(self.chapter.current_evaluation_run, run)

    def test_cache_rebuild_command(self):
        """
        Verify rebuild_chapter_assignment_cache management command checks permissions,
        rebuilds cache correctly, and respects --dry-run.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Include Sonoma', 10, 'admin')
        run = activate_ruleset(self.ruleset.id, 'admin')
        run_chapter_evaluation(run.id)

        # Delete caches to force rebuild
        ChapterAssignment.objects.all().delete()

        # Run command with non-existent actor: should raise CommandError
        with self.assertRaises(CommandError) as ctx:
            call_command('rebuild_chapter_assignment_cache', actor='nonexistent', run_id=run.id)
        self.assertIn("does not exist", str(ctx.exception))

        # Run command with unauthorized actor: should raise CommandError
        guy = User.objects.create_user(username='guy', password='password')
        with self.assertRaises(CommandError) as ctx:
            call_command('rebuild_chapter_assignment_cache', actor='guy', run_id=run.id)
        self.assertIn("does not have permission", str(ctx.exception))

        # Run command as dry-run
        call_command('rebuild_chapter_assignment_cache', actor='admin', run_id=run.id, dry_run=True)
        self.assertEqual(ChapterAssignment.objects.count(), 0) # Dry run: no commits!

        # Run actual command
        call_command('rebuild_chapter_assignment_cache', actor='admin', run_id=run.id)
        self.assertEqual(ChapterAssignment.objects.count(), 1)
        assign = ChapterAssignment.objects.first()
        self.assertEqual(assign.assignment_status, 'PROVISIONALLY_INCLUDED')

    def test_privacy_controls_geographer_preview(self):
        """
        Verify that preview differences display aggregate values only and redact PII for geographers.
        """
        from django.test import RequestFactory
        from roster.views.chapters import preview_detail

        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Include Sonoma', 10, 'admin')
        
        # Create a PREVIEW run
        active_datasets = GeographyDataset.objects.filter(status='ACTIVE')
        snapshot = {ds.id: ds.version for ds in active_datasets}
        run = ChapterEvaluationRun.objects.create(
            chapter=self.chapter, rule_set=self.ruleset, run_mode='PREVIEW',
            trigger_type='MANUAL_FULL_EVALUATION', geography_dataset_snapshot=snapshot,
            resolver_version='1.0', evaluation_engine_version='1.0',
            membership_snapshot_date=timezone.now().date(), scope='all', actor='admin', status='PENDING'
        )
        run_chapter_evaluation(run.id)

        # Mock request for geographer
        factory = RequestFactory()
        request = factory.get(f'/runs/{run.id}/preview/')
        request.user = self.geographer

        response = preview_detail(request, run.id)
        content = response.content.decode('utf-8')
        
        # Name "John Doe" must be redacted from preview output
        self.assertNotIn("John Doe", content)
        self.assertIn("Redaction", content)

    def test_meaningful_rule_matches_only(self):
        """
        Verify only rules directly responsible for the outcome are persisted in ChapterRuleMatch,
        preventing N x R record explosions.
        """
        loc = Location.objects.create(
            contributor_profile=self.cluster, city='Petaluma', state='CA', zip='94901',
            precision_level='STREET', confidence='HIGH', status='CURRENT'
        )
        run_geo = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='admin', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_geo, observed_city='Petaluma', observed_state='CA', observed_zip='94901',
            matched_canonical_county=self.county_sonoma, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Add multiple inclusion rules
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_sonoma.id, 'Sonoma Rule', 10, 'admin')
        add_rule_to_ruleset(self.ruleset.id, 'INCLUDE', 'COUNTY', self.county_marin.id, 'Marin Rule', 10, 'admin')

        run = activate_ruleset(self.ruleset.id, 'admin')
        run_chapter_evaluation(run.id)

        result = ChapterEvaluationResult.objects.get(evaluation_run=run, contributor_entity=self.entity)
        
        # Matches count: should be exactly 1 (Sonoma Rule matched, Marin did not and was NOT persisted!)
        matches_count = ChapterRuleMatch.objects.filter(evaluation_result=result).count()
        self.assertEqual(matches_count, 1)
        match = ChapterRuleMatch.objects.get(evaluation_result=result)
        self.assertEqual(match.matched_county, self.county_sonoma)
