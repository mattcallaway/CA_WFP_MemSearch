from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from roster.models import (
    Location, County, GeographicPlace, PostalArea,
    PlaceCountyAssociation, PostalCountyAssociation, PostalPlaceAssociation,
    GeographyAlias, GeographyResolutionRun, LocationGeographyResolution,
    GeographyResolutionCandidate, GeographyDataset, AuditEvent
)
from roster.services.geo_importer import normalize_zip_code

def chunk_list(lst, chunk_size):
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]

def execute_pending_resolution_run(run_id, actor, authorized_types=None):
    """
    Executes a previously created pending resolution run proposal.
    """
    if authorized_types is None:
        authorized_types = ['USPS_ZIP5']
    try:
        run = GeographyResolutionRun.objects.get(id=run_id)
    except GeographyResolutionRun.DoesNotExist:
        raise ValueError(f"Resolution run {run_id} not found")

    if run.status != 'PENDING':
        raise ValueError(f"Resolution run {run_id} is not in PENDING status (current: {run.status})")

    run.status = 'RUNNING'
    run.started_time = timezone.now()
    run.save()

    try:
        # Determine scope locations
        if run.scope and run.scope.startswith("dataset_"):
            locations_qs = Location.objects.filter(status='CURRENT')
        else:
            locations_qs = Location.objects.filter(status='CURRENT')

        locations = list(locations_qs)
        run.locations_considered = len(locations)
        run.save()

        resolved, ambiguous, conflict, unmatched = run_resolution_chunks(run, locations, actor, authorized_types=authorized_types)

        run.status = 'COMPLETED'
        run.resolved_count = resolved
        run.ambiguous_count = ambiguous
        run.conflict_count = conflict
        run.unmatched_count = unmatched
        run.completed_time = timezone.now()
        run.save()

        AuditEvent.objects.create(
            event_type='GEOGRAPHY_RESOLUTION_RUN_COMPLETED',
            description=f"Completed pending resolution run {run.id}. Considered: {len(locations)}, Resolved: {resolved}.",
            actor=actor
        )

    except Exception as e:
        run.status = 'FAILED'
        run.error_summary = str(e)
        run.completed_time = timezone.now()
        run.save()

        AuditEvent.objects.create(
            event_type='GEOGRAPHY_RESOLUTION_RUN_FAILED',
            description=f"Geography resolution run {run.id} failed: {str(e)}",
            actor=actor
        )
        raise e

    return run

def resolve_geographic_locations(actor, trigger_type='MANUAL_BULK_RESOLUTION', location_ids=None, authorized_types=None):
    """
    Triggers a new geographic resolution run.
    """
    if authorized_types is None:
        authorized_types = ['USPS_ZIP5']
    active_datasets = list(GeographyDataset.objects.filter(status='ACTIVE'))
    
    scope = f"bulk_{timezone.now().date()}" if not location_ids else f"locations_{len(location_ids)}"
    
    run = GeographyResolutionRun.objects.filter(status='PENDING', scope=scope).first()
    if not run:
        run = GeographyResolutionRun.objects.create(
            trigger_type=trigger_type,
            actor=actor,
            scope=scope,
            status='PENDING'
        )
        
    return execute_pending_resolution_run(run.id, actor, authorized_types=authorized_types)

def run_resolution_chunks(run, locations, actor, authorized_types=None):
    """
    Resolves locations in scoped chunks. Each chunk runs inside its own transaction.
    """
    if authorized_types is None:
        authorized_types = ['USPS_ZIP5']
    active_datasets = list(GeographyDataset.objects.filter(status='ACTIVE'))
    resolved = 0
    ambiguous = 0
    conflict = 0
    unmatched = 0

    chunk_size = 500
    for chunk_locs in chunk_list(locations, chunk_size):
        with transaction.atomic():
            # Collect cache keys for this chunk
            observed_zips = set()
            observed_cities = set()
            for loc in chunk_locs:
                zip5, _, _ = normalize_zip_code(loc.zip)
                if zip5:
                    observed_zips.add(zip5)
                if loc.city:
                    observed_cities.add(loc.city.upper().strip())

            # Load scoped cache maps
            county_cache = {c.normalized_name: c for c in County.objects.filter(is_active=True)}
            
            place_candidates_list = GeographicPlace.objects.filter(normalized_name__in=observed_cities, is_active=True)
            place_cache = {}
            for p in place_candidates_list:
                place_cache.setdefault(p.normalized_name, []).append(p)

            postal_cache = {p.postal_code: p for p in PostalArea.objects.filter(postal_code__in=observed_zips, postal_area_type__in=authorized_types, is_active=True)}

            alias_candidates_list = GeographyAlias.objects.filter(
                normalized_alias__in=observed_cities, is_active=True, dataset__in=active_datasets
            )
            alias_cache = {}
            for a in alias_candidates_list:
                alias_cache.setdefault(a.normalized_alias, []).append(a)

            # Bulk fetch associations linked to chunk candidates
            postal_places = PostalPlaceAssociation.objects.filter(
                postal_area__in=postal_cache.values(), is_active=True, dataset__in=active_datasets
            ).select_related('place', 'dataset')
            postal_place_map = {}
            for ppa in postal_places:
                postal_place_map.setdefault(ppa.postal_area_id, []).append(ppa)

            all_place_ids = {p.id for p_list in place_cache.values() for p in p_list}
            for a in alias_candidates_list:
                if a.place_target_id:
                    all_place_ids.add(a.place_target_id)
            
            place_counties = PlaceCountyAssociation.objects.filter(
                place_id__in=all_place_ids, is_active=True, dataset__in=active_datasets
            ).select_related('county', 'dataset')
            place_county_map = {}
            for pca in place_counties:
                place_county_map.setdefault(pca.place_id, []).append(pca)

            postal_counties = PostalCountyAssociation.objects.filter(
                postal_area__in=postal_cache.values(), is_active=True, dataset__in=active_datasets
            ).select_related('county', 'dataset')
            postal_county_map = {}
            for pca in postal_counties:
                postal_county_map.setdefault(pca.postal_area_id, []).append(pca)

            resolutions_to_create = []
            candidates_to_create = []
            locations_to_update = []

            # Mark previous current resolutions as superseded for this chunk
            LocationGeographyResolution.objects.filter(location__in=chunk_locs, status='CURRENT').update(status='SUPERSEDED')

            # Evaluate each location using the scoped cache
            for loc in chunk_locs:
                res_method, matched_place, matched_postal, matched_county, conf, explanation, candidates = evaluate_location(
                    loc, active_datasets, county_cache, place_cache, postal_cache, alias_cache,
                    postal_place_map, place_county_map, postal_county_map
                )

                if 'AMBIGUOUS' in res_method:
                    ambiguous += 1
                elif 'CONFLICTING' in res_method:
                    conflict += 1
                elif res_method == 'NO_REFERENCE_MATCH' or res_method == 'UNRESOLVED':
                    unmatched += 1
                else:
                    resolved += 1

                resolution = LocationGeographyResolution(
                    location=loc,
                    resolution_run=run,
                    observed_city=loc.city,
                    observed_state=loc.state,
                    observed_zip=loc.zip,
                    matched_canonical_county=matched_county,
                    matched_canonical_place=matched_place,
                    matched_postal_area=matched_postal,
                    match_method=res_method,
                    confidence=conf,
                    explanation=explanation,
                    origin='AUTOMATIC',
                    actor=run.actor,
                    status='CURRENT'
                )
                resolutions_to_create.append((resolution, candidates))

                loc.matched_place = matched_place
                loc.matched_postal_area = matched_postal
                loc.matched_county = matched_county
                loc.geography_dataset = active_datasets[0] if active_datasets else None
                loc.match_method = res_method
                loc.geo_confidence = conf
                loc.geo_ambiguity_status = 'RESOLVED' if res_method in ['EXACT_PLACE_ZIP_MATCH', 'EXACT_ALIAS_ZIP_MATCH', 'UNIQUE_ZIP_INFERENCE', 'MANUALLY_RESOLVED'] else 'UNRESOLVED'
                loc.geo_explanation = explanation
                locations_to_update.append(loc)

            # Bulk create resolution records first to get their IDs
            to_insert_res = [res_obj for res_obj, cands in resolutions_to_create]
            if to_insert_res:
                inserted_res = LocationGeographyResolution.objects.bulk_create(to_insert_res)
                
                # Create candidates with correct resolution foreign keys
                for index, (res_obj, cands) in enumerate(resolutions_to_create):
                    db_res_id = inserted_res[index].id
                    for cand in cands:
                        candidates_to_create.append(
                            GeographyResolutionCandidate(
                                location_resolution_id=db_res_id,
                                candidate_county=cand.get('county'),
                                candidate_place=cand.get('place'),
                                candidate_postal_area=cand.get('postal'),
                                supporting_rule=cand.get('rule', ''),
                                dataset=cand.get('dataset'),
                                confidence=cand.get('confidence', 'LOW'),
                                status=cand.get('status', 'PENDING'),
                                explanation=cand.get('explanation', '')
                            )
                        )

            if candidates_to_create:
                GeographyResolutionCandidate.objects.bulk_create(candidates_to_create)

            if locations_to_update:
                Location.objects.bulk_update(locations_to_update, fields=[
                    'matched_place', 'matched_postal_area', 'matched_county', 'geography_dataset',
                    'match_method', 'geo_confidence', 'geo_ambiguity_status', 'geo_explanation'
                ])

    return resolved, ambiguous, conflict, unmatched

def evaluate_location(loc, active_datasets, county_cache, place_cache, postal_cache, alias_cache,
                      postal_place_map=None, place_county_map=None, postal_county_map=None):
    """
    Executes decision matching using pre-loaded cache scopes (no DB queries inside if pre-loaded).
    """
    zip5, _, _ = normalize_zip_code(loc.zip)
    norm_city = loc.city.upper().strip() if loc.city else ''
    state_code = loc.state.upper().strip()[:2] if loc.state else ''

    candidates = []

    postal_area = postal_cache.get(zip5) if zip5 else None
    place_candidates = place_cache.get(norm_city, [])
    place_candidates = [p for p in place_candidates if p.state_code == state_code]

    aliases = alias_cache.get(norm_city, [])
    alias_places = [a.place_target for a in aliases if a.place_target and a.place_target.state_code == state_code]

    if postal_area and active_datasets:
        # Get active associations from maps
        if postal_place_map is None:
            postal_places_qs = PostalPlaceAssociation.objects.filter(
                postal_area=postal_area, is_active=True, dataset__in=active_datasets
            ).select_related('place', 'dataset')
            associated_places = {assoc.place: assoc for assoc in postal_places_qs}
        else:
            postal_places_qs = postal_place_map.get(postal_area.id, [])
            associated_places = {assoc.place: assoc for assoc in postal_places_qs}

        exact_matches = [p for p in place_candidates if p in associated_places]
        alias_matches = [p for p in alias_places if p in associated_places]

        if len(exact_matches) == 1:
            matched_place = exact_matches[0]
            if place_county_map is None:
                place_counties = list(PlaceCountyAssociation.objects.filter(
                    place=matched_place, is_active=True, dataset__in=active_datasets
                ).select_related('county', 'dataset'))
            else:
                place_counties = place_county_map.get(matched_place.id, [])
            
            if len(place_counties) == 1:
                matched_county = place_counties[0].county
                return (
                    'EXACT_PLACE_ZIP_MATCH', matched_place, postal_area, matched_county, 'HIGH',
                    f"County matched from exact place name '{matched_place.canonical_name}' and ZIP '{zip5}' intersection.",
                    []
                )
            elif len(place_counties) > 1:
                explanation = f"Place '{matched_place.canonical_name}' spans multiple counties; county assignment requires review."
                for pc in place_counties:
                    candidates.append({
                        'county': pc.county, 'place': matched_place, 'postal': postal_area,
                        'rule': 'PLACE_COUNTY_SPAN', 'dataset': pc.dataset, 'confidence': 'MEDIUM',
                        'status': 'PENDING', 'explanation': f"County {pc.county.display_name} is one of multiple counties associated with place {matched_place.canonical_name}"
                    })
                return ('AMBIGUOUS_ZIP', matched_place, postal_area, None, 'MEDIUM', explanation, candidates)
            else:
                return (
                    'EXACT_PLACE_ZIP_MATCH', matched_place, postal_area, None, 'MEDIUM',
                    f"Place '{matched_place.canonical_name}' matched but has no registered county association.",
                    []
                )

        if len(alias_matches) == 1:
            matched_place = alias_matches[0]
            if place_county_map is None:
                place_counties = list(PlaceCountyAssociation.objects.filter(
                    place=matched_place, is_active=True, dataset__in=active_datasets
                ).select_related('county', 'dataset'))
            else:
                place_counties = place_county_map.get(matched_place.id, [])
            
            if len(place_counties) == 1:
                matched_county = place_counties[0].county
                return (
                    'EXACT_ALIAS_ZIP_MATCH', matched_place, postal_area, matched_county, 'HIGH',
                    f"County matched from alias name '{norm_city}' mapping to canonical place '{matched_place.canonical_name}' and ZIP '{zip5}' intersection.",
                    []
                )
            elif len(place_counties) > 1:
                explanation = f"Alias '{norm_city}' place '{matched_place.canonical_name}' spans multiple counties; county assignment requires review."
                for pc in place_counties:
                    candidates.append({
                        'county': pc.county, 'place': matched_place, 'postal': postal_area,
                        'rule': 'PLACE_COUNTY_SPAN', 'dataset': pc.dataset, 'confidence': 'MEDIUM',
                        'status': 'PENDING', 'explanation': f"County {pc.county.display_name} is associated with place {matched_place.canonical_name}"
                    })
                return ('AMBIGUOUS_ZIP', matched_place, postal_area, None, 'MEDIUM', explanation, candidates)

    if postal_area and active_datasets:
        if postal_county_map is None:
            postal_counties = list(PostalCountyAssociation.objects.filter(
                postal_area=postal_area, is_active=True, dataset__in=active_datasets
            ).select_related('county', 'dataset'))
        else:
            postal_counties = postal_county_map.get(postal_area.id, [])
            
        if len(postal_counties) == 1:
            matched_county = postal_counties[0].county
            if place_candidates or alias_places:
                explanation = f"Contradictory geographic values: observed city '{loc.city}' does not associate with observed ZIP '{loc.zip}'."
                return ('CONFLICTING_SOURCE_VALUES', None, postal_area, None, 'LOW', explanation, [])
                
            return (
                'UNIQUE_ZIP_INFERENCE', None, postal_area, matched_county, 'MEDIUM',
                f"County '{matched_county.display_name}' inferred through unique association with ZIP '{zip5}'.",
                []
            )
        elif len(postal_counties) > 1:
            explanation = f"ZIP '{zip5}' is associated with more than one county; county assignment requires review."
            for pc in postal_counties:
                candidates.append({
                    'county': pc.county, 'place': None, 'postal': postal_area,
                    'rule': 'ZIP_COUNTY_MULTIPLE', 'dataset': pc.dataset, 'confidence': 'LOW',
                    'status': 'PENDING', 'explanation': f"County {pc.county.display_name} is one of multiple counties associated with ZIP {zip5}"
                })
            return ('AMBIGUOUS_ZIP', None, postal_area, None, 'LOW', explanation, candidates)

    if place_candidates:
        explanation = f"Observed city name '{loc.city}' maps to canonical place, but ZIP '{loc.zip}' reference was missing or not associated."
        return ('AMBIGUOUS_PLACE', None, postal_area, None, 'LOW', explanation, [])

    if alias_places:
        explanation = f"Observed city name '{loc.city}' maps to geographic alias, but ZIP '{loc.zip}' reference was missing or not associated."
        return ('AMBIGUOUS_ALIAS', None, postal_area, None, 'LOW', explanation, [])

    explanation = f"No reference matches found in active datasets for observed city '{loc.city}' and ZIP '{loc.zip}'."
    return ('NO_REFERENCE_MATCH', None, postal_area, None, 'LOW', explanation, [])
