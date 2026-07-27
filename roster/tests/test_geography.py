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

