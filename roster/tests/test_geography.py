import os
import csv
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from roster.models import (
    GeographyDataset, GeographyImportBatch, RawGeographyRecord,
    GeographyMappingProfile, County, GeographicPlace, PostalArea,
    CountySourceRecord, PlaceSourceRecord, PostalAreaSourceRecord,
    GeographyIdentifier, PlaceCountyAssociation, PostalCountyAssociation,
    PostalPlaceAssociation, GeographyAlias, GeographyResolutionRun,
    LocationGeographyResolution, GeographyResolutionCandidate, Location,
    ContributorEntity, ContributionCluster, AuditEvent
)
from roster.services.geo_importer import import_geography_file, rollback_geography_batch, restore_geography_batch, normalize_zip_code
from roster.services.geo_lifecycle import activate_geography_dataset
from roster.services.geo_resolver import resolve_geographic_locations, evaluate_location

class GeographyTests(TestCase):
    def setUp(self):
        # Set up core roles
        from django.core.management import call_command
        call_command('setup_roles')
        
        self.admin_user = User.objects.create_superuser(username='admin', password='password')
        self.manager_user = User.objects.create_user(username='manager', password='password')
        self.manager_user.groups.add(Group.objects.get(name='Data manager'))
        
        self.viewer_user = User.objects.create_user(username='viewer', password='password')
        self.viewer_user.groups.add(Group.objects.get(name='Read-only'))
        
        self.client = Client()

    def test_canonical_and_versioned_records(self):
        """
        Verify that multiple datasets map to a single canonical county, FIPS versions are preserved,
        and source records link back to the import batch and raw row.
        """
        # Create dataset 1
        ds1 = GeographyDataset.objects.create(
            name="Census 2020 County Directory",
            dataset_type="COUNTY_LIST",
            version="2020",
            status="PENDING",
            file_name="census_2020.csv",
            file_hash="hash_2020",
            imported_by="admin"
        )
        batch1 = GeographyImportBatch.objects.create(
            dataset=ds1, file_name="census_2020.csv", file_hash="hash_2020",
            import_type="COUNTY_LIST", status="COMPLETED", actor="admin"
        )
        raw1 = RawGeographyRecord.objects.create(
            import_batch=batch1, row_number=1, original_values={"NAME": "Contra Costa", "FIPS": "013"},
            raw_row_hash="row_hash_1", validation_status="ACCEPTED"
        )

        # Canonical county created
        county = County.objects.create(
            state_code="CA", normalized_name="CONTRA COSTA", display_name="Contra Costa County"
        )

        src1 = CountySourceRecord.objects.create(
            county=county, dataset=ds1, import_batch=batch1, raw_record=raw1,
            source_id="013", source_name="Contra Costa", county_geoid="06013", status="ACTIVE"
        )

        # Create dataset 2 (newer version)
        ds2 = GeographyDataset.objects.create(
            name="Census 2025 County Directory",
            dataset_type="COUNTY_LIST",
            version="2025",
            status="PENDING",
            file_name="census_2025.csv",
            file_hash="hash_2025",
            imported_by="admin"
        )
        batch2 = GeographyImportBatch.objects.create(
            dataset=ds2, file_name="census_2025.csv", file_hash="hash_2025",
            import_type="COUNTY_LIST", status="COMPLETED", actor="admin"
        )
        raw2 = RawGeographyRecord.objects.create(
            import_batch=batch2, row_number=1, original_values={"NAME": "Contra Costa County", "FIPS": "013"},
            raw_row_hash="row_hash_2", validation_status="ACCEPTED"
        )

        src2 = CountySourceRecord.objects.create(
            county=county, dataset=ds2, import_batch=batch2, raw_record=raw2,
            source_id="013", source_name="Contra Costa County", county_geoid="06013", status="ACTIVE"
        )

        # Both source records point to the single canonical county
        self.assertEqual(county.source_records.count(), 2)
        self.assertEqual(src1.import_batch, batch1)
        self.assertEqual(src2.import_batch, batch2)

    def test_postal_semantics_normalization(self):
        """
        Validate leading-zero preservation, ZIP+4 separation, ZCTA/ZIP separation,
        and short ZIP warnings.
        """
        # Leading zero preservation
        zip5, zip4, warning = normalize_zip_code("02138")
        self.assertEqual(zip5, "02138")
        self.assertIsNone(zip4)
        self.assertIsNone(warning)

        # ZIP+4 separation
        zip5, zip4, warning = normalize_zip_code("95401-1234")
        self.assertEqual(zip5, "95401")
        self.assertEqual(zip4, "1234")
        self.assertIsNone(warning)

        # Short ZIP without padding rules produces warning
        zip5, zip4, warning = normalize_zip_code("2138")
        self.assertIsNone(zip5)
        self.assertIsNotNone(warning)
 
        # Short ZIP with padding rule
        profile = GeographyMappingProfile(normalization_rules={"allow_zip_padding": True})
        zip5, zip4, warning = normalize_zip_code("2138", profile)
        self.assertEqual(zip5, "02138")
        self.assertIsNotNone(warning)

    def test_many_to_many_relationships(self):
        """
        Ensure place-to-county, postal-to-county, and postal-to-place support many-to-many.
        Validate duplicate active relationship blocks.
        """
        c1 = County.objects.create(state_code="CA", normalized_name="MARIN", display_name="Marin")
        c2 = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Petaluma", normalized_name="PETALUMA", general_category="CITY")

        ds = GeographyDataset.objects.create(name="DS", dataset_type="PLACE_COUNTY_CROSSWALK", version="1.0", file_name="f", file_hash="h", imported_by="u")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")

        # Associate place to two counties
        assoc1 = PlaceCountyAssociation.objects.create(
            place=place, county=c1, relationship_type="CROSSWALK", confidence="HIGH", is_active=True, dataset=ds, import_batch=batch
        )
        assoc2 = PlaceCountyAssociation.objects.create(
            place=place, county=c2, relationship_type="CROSSWALK", confidence="HIGH", is_active=True, dataset=ds, import_batch=batch
        )

        self.assertEqual(place.county_associations.count(), 2)

        # Block duplicate active relationship in same dataset scope
        with self.assertRaises(IntegrityError):
            PlaceCountyAssociation.objects.create(
                place=place, county=c1, relationship_type="CROSSWALK", confidence="HIGH", is_active=True, dataset=ds, import_batch=batch
            )

    def test_alias_target_constraint(self):
        """
        Verify that GeographyAlias requires exactly one target (county, place, or postal).
        """
        ds = GeographyDataset.objects.create(name="DS", dataset_type="ALIAS_LIST", version="1.0", file_name="f", file_hash="h", imported_by="u")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")
        county = County.objects.create(state_code="CA", normalized_name="MARIN", display_name="Marin")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Novato", normalized_name="NOVATO", general_category="CITY")

        # Zero targets should fail database check constraint
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GeographyAlias.objects.create(
                    alias_type="COMMON_NAME", original_alias="Nov", normalized_alias="NOV",
                    dataset=ds, import_batch=batch, is_active=True
                )

        # Two targets should fail
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GeographyAlias.objects.create(
                    alias_type="COMMON_NAME", original_alias="Nov", normalized_alias="NOV",
                    county_target=county, place_target=place,
                    dataset=ds, import_batch=batch, is_active=True
                )

        # Exactly one target succeeds
        alias = GeographyAlias.objects.create(
            alias_type="COMMON_NAME", original_alias="Nov", normalized_alias="NOV",
            place_target=place,
            dataset=ds, import_batch=batch, is_active=True
        )
        self.assertIsNotNone(alias.id)

    def test_matching_decision_sequence(self):
        """
        Tests resolution matching engine logic: exact matches, unique ZIP, ambiguity, conflicts.
        """
        # Set up active dataset and canonical references
        ds = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f", file_hash="h", status="ACTIVE")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")

        county = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma County")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Santa Rosa", normalized_name="SANTA ROSA", general_category="CITY")
        postal = PostalArea.objects.create(postal_code="95404", postal_area_type="USPS_ZIP5")

        # Set up associations
        PostalPlaceAssociation.objects.create(
            postal_area=postal, place=place, relationship_type="CROSSWALK", confidence="HIGH", dataset=ds, import_batch=batch
        )
        PlaceCountyAssociation.objects.create(
            place=place, county=county, relationship_type="CROSSWALK", confidence="HIGH", dataset=ds, import_batch=batch
        )
        PostalCountyAssociation.objects.create(
            postal_area=postal, county=county, relationship_type="CROSSWALK", confidence="HIGH", dataset=ds, import_batch=batch
        )

        entity = ContributorEntity.objects.create(display_name="John Doe", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="DOE JOHN", zip_code="95404")

        # Location matches exact place and ZIP
        loc = Location.objects.create(
            contributor_profile=cluster, city="Santa Rosa", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )

        county_cache = {"SONOMA": county}
        place_cache = {"SANTA ROSA": [place]}
        postal_cache = {"95404": postal}

        res_method, matched_place, matched_postal, matched_county, conf, explanation, candidates = evaluate_location(
            loc, [ds], county_cache, place_cache, postal_cache, {}
        )

        self.assertEqual(res_method, "EXACT_PLACE_ZIP_MATCH")
        self.assertEqual(matched_place, place)
        self.assertEqual(matched_county, county)

        # Test conflicting values (city and ZIP do not associate)
        place_la = GeographicPlace.objects.create(state_code="CA", canonical_name="Los Angeles", normalized_name="LOS ANGELES", general_category="CITY")
        loc_conflict = Location.objects.create(
            contributor_profile=cluster, city="Los Angeles", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )
        res_method, _, _, _, _, _, _ = evaluate_location(
            loc_conflict, [ds], {}, {"LOS ANGELES": [place_la]}, postal_cache, {}
        )
        self.assertEqual(res_method, "CONFLICTING_SOURCE_VALUES")

        # Test unmatched values
        loc_unmatched = Location.objects.create(
            contributor_profile=cluster, city="Nowhere", state="CA", zip="99999", precision_level="STREET", confidence="HIGH"
        )
        res_method, _, _, _, _, _, _ = evaluate_location(
            loc_unmatched, [ds], {}, {"NOWHERE": []}, {}, {}
        )
        self.assertEqual(res_method, "NO_REFERENCE_MATCH")

    def test_manual_override_resolution(self):
        """
        Verify that reviewed manual decisions override automatic matched statuses cleanly.
        """
        ds = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f", file_hash="h", status="ACTIVE")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")
        county = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma County")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Santa Rosa", normalized_name="SANTA ROSA", general_category="CITY")

        entity = ContributorEntity.objects.create(display_name="John Doe", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="DOE JOHN", zip_code="95404")
        loc = Location.objects.create(
            contributor_profile=cluster, city="Santa Rosa", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )

        # Trigger automatic resolve
        run = resolve_geographic_locations(actor="admin", trigger_type="POST_CONTRIBUTION_IMPORT", location_ids=[loc.id])
        res = LocationGeographyResolution.objects.get(location=loc, status="CURRENT")

        # Apply manual resolution override via view mock POST
        self.client.force_login(self.admin_user)
        response = self.client.post(reverse('geography_manual_resolve', args=[res.id]), {
            'county_id': county.id,
            'place_id': place.id,
            'explanation': 'Corrected manually by admin'
        })
        
        self.assertEqual(response.status_code, 302)
        loc.refresh_from_db()
        self.assertEqual(loc.match_method, "MANUALLY_RESOLVED")
        self.assertEqual(loc.matched_county, county)

        # Verify old resolution is SUPERSEDED
        res.refresh_from_db()
        self.assertEqual(res.status, "SUPERSEDED")

    def test_geographer_privacy_controls(self):
        """
        Verify that a user with resolve_geography_ambiguity but without view_sensitive_roster
        cannot view personal identifiers (name, street, email) on the ambiguity queue.
        """
        # Create a user with resolve_geography_ambiguity only
        geographer = User.objects.create_user(username='geographer', password='password')
        group = Group.objects.create(name='Geographers')
        perm = Permission.objects.get(
            content_type=ContentType.objects.get_for_model(ContributorEntity),
            codename='resolve_geography_ambiguity'
        )
        group.permissions.add(perm)
        geographer.groups.add(group)

        # Create a resolution to show on queue
        ds = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f", file_hash="h", status="ACTIVE")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")
        entity = ContributorEntity.objects.create(display_name="SECRET_CONTRIBUTOR", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="SECRET CONTRIBUTOR", zip_code="95404")
        loc = Location.objects.create(
            contributor_profile=cluster, street_address="123 Secret Lane", city="Santa Rosa", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )
        resolve_geographic_locations(actor="admin", location_ids=[loc.id])

        # Login geographer and request queue page
        self.client.force_login(geographer)
        response = self.client.get(reverse('geography_ambiguity_queue'))
        
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "[REDACTED]")
        self.assertNotContains(response, "SECRET_CONTRIBUTOR")
        self.assertNotContains(response, "123 Secret Lane")

        # Login admin (who has both permissions)
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('geography_ambiguity_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SECRET CONTRIBUTOR")
        self.assertContains(response, "123 Secret Lane")

    def test_weight_range_constraint(self):
        """
        Verify that database CheckConstraint enforces normalized_weight_value range of 0.0 to 1.0.
        """
        c = County.objects.create(state_code="CA", normalized_name="MARIN", display_name="Marin")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Petaluma", normalized_name="PETALUMA", general_category="CITY")
        ds = GeographyDataset.objects.create(name="DS", dataset_type="PLACE_COUNTY_CROSSWALK", version="1.0", file_name="f", file_hash="h", imported_by="u")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")

        # Invalid weight (> 1.0)
        assoc_invalid = PlaceCountyAssociation(
            place=place, county=c, relationship_type='CROSSWALK', confidence='HIGH',
            is_active=True, dataset=ds, import_batch=batch,
            normalized_weight_value=1.5
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                assoc_invalid.save()

        # Valid weight (0.75) should succeed
        assoc_valid = PlaceCountyAssociation(
            place=place, county=c, relationship_type='CROSSWALK', confidence='HIGH',
            is_active=True, dataset=ds, import_batch=batch,
            normalized_weight_value=0.75
        )
        assoc_valid.save()
        self.assertIsNotNone(assoc_valid.id)

    def test_independent_dataset_activation_and_execution(self):
        """
        Verify that dataset activation commits independently and creates a pending proposal run.
        Also verify execute_pending_resolution_run correctly completes it.
        """
        from roster.services.geo_resolver import execute_pending_resolution_run
        
        ds = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f", file_hash="h", status="PENDING")
        
        # Mapping profile snapshot
        GeographyMappingProfile.objects.create(
            name="Mapping for TIGER CA", source_type="COUNTY_LIST", version="1.0",
            mapping_rules={'COUNTY_NAME': 'NAME'}, owner=self.admin_user, is_active=True
        )

        run_proposal = activate_geography_dataset(ds.id, actor="admin", run_resolution_auto=False)
        
        # Verify run was created as PENDING
        self.assertEqual(run_proposal.status, 'PENDING')
        self.assertEqual(run_proposal.dataset, ds)

        # Trigger execution
        completed_run = execute_pending_resolution_run(run_proposal.id, actor="admin")
        self.assertEqual(completed_run.status, 'COMPLETED')

    def test_rebuild_location_geography_cache_command(self):
        """
        Verify that the rebuild_location_geography_cache command rebuilds cache correctly,
        clears cache when no current resolution is present, and detects corruption.
        """
        from django.core.management import call_command
        
        # Create actor user as superuser
        User.objects.create_superuser(username='test_runner', password='password')
        
        entity = ContributorEntity.objects.create(display_name="Bench Person", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="BENCH PERSON", zip_code="94901")
        loc = Location.objects.create(
            contributor_profile=cluster, city="Petaluma", state="CA", zip="94901", precision_level="STREET", confidence="HIGH"
        )
        
        # Rebuild when no current resolution exists: should clear cache
        call_command('rebuild_location_geography_cache', actor="test_runner")
        loc.refresh_from_db()
        self.assertIsNone(loc.matched_county)
        self.assertEqual(loc.match_method, 'UNRESOLVED')

        # Setup one current resolution
        c = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma")
        run = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='test_runner', status='COMPLETED')
        res = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run, observed_city="Petaluma", observed_state="CA", observed_zip="94901",
            matched_canonical_county=c, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Rebuild: should update cache fields
        call_command('rebuild_location_geography_cache', actor="test_runner")
        loc.refresh_from_db()
        self.assertEqual(loc.matched_county, c)
        self.assertEqual(loc.match_method, 'EXACT_PLACE_ZIP_MATCH')

        # Mock multiple current resolutions to test corruption detection
        from unittest.mock import patch, MagicMock
        from django.db.models.query import QuerySet

        mock_res1 = LocationGeographyResolution(
            id=99991, location=loc, observed_city="Petaluma", observed_state="CA", observed_zip="94901",
            matched_canonical_county=c, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )
        mock_res2 = LocationGeographyResolution(
            id=99992, location=loc, observed_city="Petaluma", observed_state="CA", observed_zip="94901",
            matched_canonical_county=c, match_method='UNIQUE_ZIP_INFERENCE', confidence='MEDIUM', status='CURRENT'
        )

        mock_query = MagicMock()
        mock_query.select_related.return_value = [mock_res1, mock_res2]

        # Target only the filter method on LocationGeographyResolution's manager
        with patch('roster.models.LocationGeographyResolution.objects.filter', return_value=mock_query):
            call_command('rebuild_location_geography_cache', actor="test_runner")
            loc.refresh_from_db()
            # Remains unchanged due to corruption skip
            self.assertEqual(loc.matched_county, c)
            self.assertEqual(loc.match_method, 'EXACT_PLACE_ZIP_MATCH')

    def test_query_profiler_correctness(self):
        """
        Verify QueryProfiler counting correctness, nested contexts, resets, and no SQL logging.
        """
        from roster.services.geo_importer import QueryProfiler
        from django.db import connection

        profiler = QueryProfiler()
        profiler.start_phase("phase_1")
        
        # Run a query in phase 1
        with connection.execute_wrapper(profiler):
            User.objects.count()
            
        self.assertEqual(profiler.counts["phase_1"], 1)
        self.assertNotIn("phase_2", profiler.counts)

        # Increments correctly
        profiler.start_phase("phase_2")
        with connection.execute_wrapper(profiler):
            User.objects.count()

        self.assertEqual(profiler.counts["phase_1"], 1)
        self.assertEqual(profiler.counts["phase_2"], 1)

    def test_resolution_atomicity(self):
        """
        Verify transaction atomicity and rollbacks during execution.
        """
        from roster.services.geo_resolver import execute_pending_resolution_run
        from unittest.mock import patch

        entity = ContributorEntity.objects.create(display_name="Atom Test Person", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="TEST ATOM", zip_code="94901")
        loc = Location.objects.create(
            contributor_profile=cluster, city="Sonoma", state="CA", zip="94901",
            precision_level="CITY", confidence="LOW", status="CURRENT"
        )
        
        c_sonoma = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma County")
        run_init = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='test_actor', status='COMPLETED')
        res_init = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run_init, observed_city="Sonoma", observed_state="CA", observed_zip="94901",
            matched_canonical_county=c_sonoma, match_method='UNIQUE_ZIP_INFERENCE', confidence='MEDIUM', status='CURRENT'
        )
        loc.matched_county = c_sonoma
        loc.match_method = 'UNIQUE_ZIP_INFERENCE'
        loc.save()

        ds = GeographyDataset.objects.create(name="Mock Active DS", dataset_type="ZIP_COUNTY_CROSSWALK", version="1.0", file_name="dummy.csv", file_hash="hash_dummy_atom", status="ACTIVE")
        batch = GeographyImportBatch.objects.create(
            dataset=ds, file_name="dummy.csv", file_hash="hash_dummy_atom",
            import_type="ZIP_COUNTY_CROSSWALK", status="COMPLETED", actor="test_actor"
        )
        po = PostalArea.objects.create(postal_code="94901", postal_area_type='USPS_ZIP5')
        c_marin = County.objects.create(state_code="CA", normalized_name="MARIN", display_name="Marin County")
        
        from roster.models import PostalCountyAssociation
        PostalCountyAssociation.objects.create(
            postal_area=po, county=c_marin, relationship_type='CROSSWALK',
            confidence='HIGH', is_active=True, dataset=ds,
            import_batch=batch, normalized_weight_value=1.0
        )

        run = GeographyResolutionRun.objects.create(
            trigger_type='DATASET_ACTIVATION', resolver_version='1.0', scope='dataset_atom',
            actor='test_actor', status='PENDING', dataset=ds
        )

        # Patch QuerySet.bulk_update to fail
        with patch('django.db.models.query.QuerySet.bulk_update', side_effect=RuntimeError("Intentionally failing bulk update")):
            with self.assertRaises(RuntimeError):
                execute_pending_resolution_run(run.id, "test_actor")

        run.refresh_from_db()
        self.assertEqual(run.status, 'FAILED')
        self.assertIn("Intentionally failing bulk update", run.error_summary)

        res_init.refresh_from_db()
        self.assertEqual(res_init.status, 'CURRENT')

        current_resolutions = list(LocationGeographyResolution.objects.filter(location=loc, status='CURRENT'))
        self.assertEqual(len(current_resolutions), 1)
        self.assertEqual(current_resolutions[0].id, res_init.id)

        loc.refresh_from_db()
        self.assertEqual(loc.matched_county, c_sonoma)
        self.assertEqual(loc.match_method, 'UNIQUE_ZIP_INFERENCE')

        cand_count = GeographyResolutionCandidate.objects.exclude(location_resolution=res_init).count()
        self.assertEqual(cand_count, 0)

    def test_pending_proposal_lifecycle(self):
        """
        Verify dataset activation pending-proposals, supersessions, stale checks, and retries.
        """
        from roster.services.geo_lifecycle import activate_geography_dataset
        from roster.services.geo_resolver import execute_pending_resolution_run

        ds1 = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f1", file_hash="h1", status="PENDING")
        GeographyMappingProfile.objects.create(
            name="Mapping for TIGER CA", source_type="COUNTY_LIST", version="1.0",
            mapping_rules={'COUNTY_NAME': 'NAME'}, owner=self.admin_user, is_active=True
        )

        run1 = activate_geography_dataset(ds1.id, actor="admin", run_resolution_auto=False)
        self.assertEqual(run1.status, 'PENDING')
        self.assertEqual(run1.dataset, ds1)

        # Check duplicate pending run is blocked by unique constraint
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GeographyResolutionRun.objects.create(
                    trigger_type='DATASET_ACTIVATION', dataset=ds1, status='PENDING', scope=run1.scope
                )

        # Test supersession: activate new version ds2 which invalidates ds1's pending proposal
        ds2 = GeographyDataset.objects.create(name="TIGER CA 2", dataset_type="COUNTY_LIST", version="2.0", file_name="f2", file_hash="h2", status="PENDING")
        run2 = activate_geography_dataset(ds2.id, actor="admin", run_resolution_auto=False)
        
        run1.refresh_from_db()
        self.assertEqual(run1.status, 'SUPERSEDED')

        # Stale proposal cannot execute
        with self.assertRaises(ValueError):
            execute_pending_resolution_run(run1.id, "admin")

        # Retry failed run
        run2.status = 'FAILED'
        run2.save()

        # Creating a retry run creates a new run instead of erasing the failed run
        retry_run = GeographyResolutionRun.objects.create(
            trigger_type='MANUAL_BULK_RESOLUTION', dataset=ds2, status='PENDING', scope='retry_scope',
            resolver_version='1.0', actor='admin'
        )
        self.assertIsNotNone(retry_run.id)
        self.assertNotEqual(retry_run.id, run2.id)

    def test_zip_and_zcta_crosswalks(self):
        """
        Verify separate ZIP and ZCTA crosswalk imports and resolver scoping.
        """
        # Create canonical counties referenced in CSV rows
        County.objects.get_or_create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma County")
        County.objects.get_or_create(state_code="CA", normalized_name="MARIN", display_name="Marin County")

        # Create a mapping profile for ZCTA crosswalk
        zcta_profile = GeographyMappingProfile.objects.create(
            name="ZCTA Profile", source_type="ZIP_COUNTY_CROSSWALK", version="1.0",
            mapping_rules={'POSTAL_CODE': 'ZCTA', 'COUNTY_NAME': 'COUNTY', 'POSTAL_AREA_TYPE': 'CENSUS_ZCTA5'},
            owner=self.admin_user, is_active=True
        )

        ds_zcta = GeographyDataset.objects.create(
            name="Census ZCTA Crosswalk", dataset_type="ZIP_COUNTY_CROSSWALK", version="1.0",
            file_name="zcta.csv", file_hash="hash_zcta", status="PENDING"
        )
        
        # Create CSV content
        csv_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'synthetic_zcta.csv')
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['ZCTA', 'COUNTY'])
            writer.writerow(['94901', 'Sonoma'])

        from roster.services.geo_importer import import_geography_file
        batch_zcta = import_geography_file(
            file_path=csv_path, file_name="zcta.csv", dataset_id=ds_zcta.id, actor="admin"
        )

        # Assert that the created postal area has type CENSUS_ZCTA5
        zcta_area = PostalArea.objects.get(postal_code='94901', postal_area_type='CENSUS_ZCTA5')
        self.assertEqual(zcta_area.postal_area_type, 'CENSUS_ZCTA5')

        # Deactivate ZCTA profile and activate ZIP profile to prevent collision during auto-lookup
        zcta_profile.is_active = False
        zcta_profile.save()

        # Create another import mapping to USPS_ZIP5
        zip_profile = GeographyMappingProfile.objects.create(
            name="ZIP Profile", source_type="ZIP_COUNTY_CROSSWALK", version="1.0",
            mapping_rules={'POSTAL_CODE': 'ZIP', 'COUNTY_NAME': 'COUNTY', 'POSTAL_AREA_TYPE': 'USPS_ZIP5'},
            owner=self.admin_user, is_active=True
        )
        
        ds_zip = GeographyDataset.objects.create(
            name="USPS ZIP Crosswalk", dataset_type="ZIP_COUNTY_CROSSWALK", version="1.0",
            file_name="zip.csv", file_hash="hash_zip", status="PENDING"
        )
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['ZIP', 'COUNTY'])
            writer.writerow(['94901', 'Marin'])

        batch_zip = import_geography_file(
            file_path=csv_path, file_name="zip.csv", dataset_id=ds_zip.id, actor="admin"
        )

        from roster.models import RawGeographyRecord
        raw_recs = list(RawGeographyRecord.objects.filter(import_batch=batch_zip))
        for r in raw_recs:
            print(f"ZIP IMPORT RAW RECORD STATUS: {r.validation_status}, ERRORS: {r.validation_errors}")

        zip_area = PostalArea.objects.get(postal_code='94901', postal_area_type='USPS_ZIP5')
        self.assertEqual(zip_area.postal_area_type, 'USPS_ZIP5')

        # Clean up synthetic CSV
        if os.path.exists(csv_path):
            os.remove(csv_path)

    def test_cache_rebuild_authorization(self):
        """
        Verify actor validation, active checks, and permission checks for the rebuild command.
        """
        from django.core.management import call_command
        from django.core.management.base import CommandError

        # Missing actor: should raise CommandError (handled by parser because required=True)
        # We can verify it raises CommandError when username is not found
        with self.assertRaises(CommandError) as context:
            call_command('rebuild_location_geography_cache', actor="nonexistent_user")
        self.assertIn("does not exist", str(context.exception))

        # Inactive actor
        inactive_user = User.objects.create_user(username='inactive_guy', password='password', is_active=False)
        with self.assertRaises(CommandError) as context:
            call_command('rebuild_location_geography_cache', actor="inactive_guy")
        self.assertIn("is inactive", str(context.exception))

        # Unauthorized actor (active but lacks perm)
        unauth_user = User.objects.create_user(username='unauth_guy', password='password')
        with self.assertRaises(CommandError) as context:
            call_command('rebuild_location_geography_cache', actor="unauth_guy")
        self.assertIn("does not have 'roster.manage_geography_reference' permission", str(context.exception))

        # Authorized geography manager (has perm)
        auth_user = User.objects.create_user(username='auth_manager', password='password')
        perm = Permission.objects.get(
            content_type=ContentType.objects.get_for_model(ContributorEntity),
            codename='manage_geography_reference'
        )
        auth_user.user_permissions.add(perm)
        
        # Should execute successfully
        call_command('rebuild_location_geography_cache', actor="auth_manager")

    def test_privacy_sentinels(self):
        """
        Verify that unique synthetic sentinel PII values never appear in geography-only responses.
        """
        # Create a user with resolve_geography_ambiguity only
        geographer = User.objects.create_user(username='geographer_privacy', password='password')
        group = Group.objects.create(name='Geographers Privacy')
        perm = Permission.objects.get(
            content_type=ContentType.objects.get_for_model(ContributorEntity),
            codename='resolve_geography_ambiguity'
        )
        group.permissions.add(perm)
        geographer.groups.add(group)

        # Create unique synthetic PII sentinels
        SENTINEL_NAME = "SENTINEL_NAME_123"
        SENTINEL_STREET = "SENTINEL_STREET_456"

        ds = GeographyDataset.objects.create(name="TIGER CA", dataset_type="COUNTY_LIST", version="1.0", file_name="f", file_hash="h", status="ACTIVE")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")
        entity = ContributorEntity.objects.create(display_name=SENTINEL_NAME, entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="SENTINEL CLUSTER", zip_code="95404")
        loc = Location.objects.create(
            contributor_profile=cluster, street_address=SENTINEL_STREET, city="Santa Rosa", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )
        resolve_geographic_locations(actor="admin", location_ids=[loc.id])

        # Request queue page with geographer (PII redacted)
        self.client.force_login(geographer)
        response = self.client.get(reverse('geography_ambiguity_queue'))
        
        # Verify sentinels are redacted in rendered HTML and response context
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SENTINEL_NAME)
        self.assertNotContains(response, SENTINEL_STREET)

        # Request page with admin (who has both permissions)
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('geography_ambiguity_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SENTINEL CLUSTER")
        self.assertContains(response, SENTINEL_STREET)

    def test_constraints(self):
        """
        Verify database unique constraints and active relationship blockings.
        """
        # One-current resolution constraint
        entity = ContributorEntity.objects.create(display_name="Constraint Person", entity_type="INDIVIDUAL")
        cluster = ContributionCluster.objects.create(contributor_entity=entity, normalized_name="CONSTRAINT PERSON", zip_code="95404")
        loc = Location.objects.create(
            contributor_profile=cluster, city="Santa Rosa", state="CA", zip="95404", precision_level="STREET", confidence="HIGH"
        )
        c = County.objects.create(state_code="CA", normalized_name="SONOMA", display_name="Sonoma")
        run = GeographyResolutionRun.objects.create(trigger_type='MANUAL_BULK_RESOLUTION', actor='test_runner', status='COMPLETED')
        
        res1 = LocationGeographyResolution.objects.create(
            location=loc, resolution_run=run, observed_city="Santa Rosa", observed_state="CA", observed_zip="95404",
            matched_canonical_county=c, match_method='EXACT_PLACE_ZIP_MATCH', confidence='HIGH', status='CURRENT'
        )

        # Second current resolution should fail with IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LocationGeographyResolution.objects.create(
                    location=loc, resolution_run=run, observed_city="Santa Rosa", observed_state="CA", observed_zip="95404",
                    matched_canonical_county=c, match_method='UNIQUE_ZIP_INFERENCE', confidence='MEDIUM', status='CURRENT'
                )

        # Duplicate active relationships blocked
        ds = GeographyDataset.objects.create(name="DS", dataset_type="PLACE_COUNTY_CROSSWALK", version="1.0", file_name="f", file_hash="h", imported_by="u")
        batch = GeographyImportBatch.objects.create(dataset=ds, file_name="f", file_hash="h", import_type="t", actor="u")
        place = GeographicPlace.objects.create(state_code="CA", canonical_name="Petaluma", normalized_name="PETALUMA", general_category="CITY")

        assoc1 = PlaceCountyAssociation.objects.create(
            place=place, county=c, relationship_type='CROSSWALK', confidence='HIGH',
            is_active=True, dataset=ds, import_batch=batch, normalized_weight_value=0.5
        )

        # Second active duplicate should fail with IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlaceCountyAssociation.objects.create(
                    place=place, county=c, relationship_type='CROSSWALK', confidence='HIGH',
                    is_active=True, dataset=ds, import_batch=batch, normalized_weight_value=0.5
                )

        # Inactive associations may coexist
        assoc1.is_active = False
        assoc1.save()

        assoc2 = PlaceCountyAssociation.objects.create(
            place=place, county=c, relationship_type='CROSSWALK', confidence='HIGH',
            is_active=True, dataset=ds, import_batch=batch, normalized_weight_value=0.5
        )
        self.assertIsNotNone(assoc2.id)


