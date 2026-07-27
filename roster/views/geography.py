import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import HttpResponse, Http404
from django.db import transaction
from django.core.files.storage import FileSystemStorage
from roster.models import (
    GeographyDataset, GeographyImportBatch, RawGeographyRecord,
    GeographyMappingProfile, County, GeographicPlace, PostalArea,
    CountySourceRecord, PlaceSourceRecord, PostalAreaSourceRecord,
    GeographyIdentifier, PlaceCountyAssociation, PostalCountyAssociation,
    PostalPlaceAssociation, GeographyAlias, GeographyResolutionRun,
    LocationGeographyResolution, GeographyResolutionCandidate, Location,
    AuditEvent
)
from roster.services.geo_importer import import_geography_file, rollback_geography_batch, restore_geography_batch
from roster.services.geo_lifecycle import activate_geography_dataset
from roster.services.geo_resolver import resolve_geographic_locations

# 1. Dataset Directory and Detail views
@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def geography_datasets_list(request):
    datasets = GeographyDataset.objects.all().order_by('-created_at')
    batches = GeographyImportBatch.objects.all().order_by('-started_time')
    return render(request, 'geography/datasets_list.html', {
        'datasets': datasets,
        'batches': batches
    })

@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def geography_dataset_detail(request, dataset_id):
    dataset = get_object_or_404(GeographyDataset, id=dataset_id)
    batches = GeographyImportBatch.objects.filter(dataset=dataset).order_by('-started_time')
    pending_runs = GeographyResolutionRun.objects.filter(dataset=dataset, status='PENDING')
    return render(request, 'geography/dataset_detail.html', {
        'dataset': dataset,
        'batches': batches,
        'pending_runs': pending_runs
    })

# 2. Upload / Mapping / Process views
@login_required
@permission_required('roster.import_geography_reference', raise_exception=True)
def geography_import_upload(request):
    """
    Handles geography reference file uploads and column header previews.
    """
    if request.method == 'POST' and request.FILES.get('file'):
        uploaded_file = request.FILES['file']
        fs = FileSystemStorage(location='scratch')
        filename = fs.save(uploaded_file.name, uploaded_file)
        file_path = fs.path(filename)
        
        # Read headers
        try:
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f) if False else None
                # Let's parse headers manually or with standard csv
                import csv as pycsv
                f.seek(0)
                reader = pycsv.reader(f)
                headers = next(reader)
        except Exception as e:
            fs.delete(filename)
            messages.error(request, f"Failed to read file headers: {str(e)}")
            return redirect('geography_import_upload')

        # Get profiles of similar type
        profiles = GeographyMappingProfile.objects.filter(is_active=True)
        
        return render(request, 'geography/import_mapping.html', {
            'file_name': uploaded_file.name,
            'temp_path': file_path,
            'headers': headers,
            'profiles': profiles,
            'types': GeographyDataset._meta.get_field('dataset_type').choices if False else [
                ('COUNTY_LIST', 'County Reference List'),
                ('PLACE_LIST', 'Place Reference List'),
                ('POSTAL_LIST', 'Postal Area Reference List'),
                ('ZIP_COUNTY_CROSSWALK', 'ZIP-to-County Crosswalk'),
                ('ZIP_PLACE_CROSSWALK', 'ZIP-to-Place Crosswalk'),
                ('PLACE_COUNTY_CROSSWALK', 'Place-to-County Crosswalk'),
                ('ALIAS_LIST', 'Alias List'),
                ('IDENTIFIER_LIST', 'Identifier List')
            ]
        })
        
    return render(request, 'geography/import_upload.html')

@login_required
@permission_required('roster.import_geography_reference', raise_exception=True)
@require_POST
def geography_import_execute(request):
    """
    Executes geography import mapping and runs validation.
    """
    file_name = request.POST.get('file_name')
    file_path = request.POST.get('temp_path')
    dataset_name = request.POST.get('dataset_name')
    dataset_type = request.POST.get('dataset_type')
    version = request.POST.get('version', '1.0')
    source_org = request.POST.get('source_org', '')
    desc = request.POST.get('description', '')
    override_dup = request.POST.get('override_duplicate') == 'true'

    if not file_path or not os.path.exists(file_path):
        messages.error(request, "Uploaded file not found.")
        return redirect('geography_import_upload')

    # Read selected column mappings from form POST
    mapping_rules = {}
    for key, val in request.POST.items():
        if key.startswith('map_') and val:
            field_name = key[4:]
            mapping_rules[field_name] = val

    # Create/fetch versioned dataset
    with transaction.atomic():
        dataset, _ = GeographyDataset.objects.get_or_create(
            name=dataset_name,
            dataset_type=dataset_type,
            version=version,
            defaults={
                'source_organization': source_org,
                'source_description': desc,
                'file_name': file_name,
                'file_hash': 'PENDING_HASH',
                'imported_by': request.user.username
            }
        )

    # Save a mapping profile snapshot
    profile = GeographyMappingProfile.objects.create(
        name=f"Mapping for {dataset_name}",
        source_type=dataset_type,
        version=version,
        mapping_rules=mapping_rules,
        owner=request.user,
        is_active=True
    )

    try:
        batch = import_geography_file(
            file_path=file_path,
            file_name=file_name,
            dataset_id=dataset.id,
            actor=request.user.username,
            override_duplicate=override_dup
        )
        
        # Cleanup temp file
        if os.path.exists(file_path):
            os.remove(file_path)
            
        messages.success(request, f"Successfully imported geography file (Batch {batch.id}).")
        return redirect('geography_dataset_detail', dataset_id=dataset.id)
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        messages.error(request, f"Geography import failed: {str(e)}")
        return redirect('geography_import_upload')

# 3. Activation, Rollback, Restore
@login_required
@permission_required('roster.manage_geography_reference', raise_exception=True)
@require_POST
def geography_dataset_activate(request, dataset_id):
    priority = int(request.POST.get('priority', 100))
    try:
        run = activate_geography_dataset(dataset_id, request.user.username, priority)
        messages.success(request, f"Dataset activated. Created a pending resolution run proposal #{run.id}.")
    except Exception as e:
        messages.error(request, f"Activation failed: {str(e)}")
    return redirect('geography_dataset_detail', dataset_id=dataset_id)

@login_required
@permission_required('roster.rollback_geography_import', raise_exception=True)
@require_POST
def geography_batch_rollback(request, batch_id):
    batch = get_object_or_404(GeographyImportBatch, id=batch_id)
    try:
        rollback_geography_batch(batch_id, request.user.username)
        messages.success(request, f"Successfully rolled back batch {batch_id}.")
    except Exception as e:
        messages.error(request, f"Rollback failed: {str(e)}")
    return redirect('geography_dataset_detail', dataset_id=batch.dataset_id)

@login_required
@permission_required('roster.rollback_geography_import', raise_exception=True)
@require_POST
def geography_batch_restore(request, batch_id):
    batch = get_object_or_404(GeographyImportBatch, id=batch_id)
    try:
        restore_geography_batch(batch_id, request.user.username)
        messages.success(request, f"Successfully restored batch {batch_id}.")
    except Exception as e:
        messages.error(request, f"Restoration failed: {str(e)}")
    return redirect('geography_dataset_detail', dataset_id=batch.dataset_id)

# 4. Directories
@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def county_directory(request):
    counties = County.objects.all().order_by('normalized_name')
    return render(request, 'geography/county_directory.html', {
        'counties': counties
    })

@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def place_directory(request):
    places = GeographicPlace.objects.all().order_by('normalized_name')
    return render(request, 'geography/place_directory.html', {
        'places': places
    })

@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def postal_area_directory(request):
    postal_areas = PostalArea.objects.all().order_by('postal_code')
    return render(request, 'geography/postal_area_directory.html', {
        'postal_areas': postal_areas
    })

@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def geography_alias_directory(request):
    aliases = GeographyAlias.objects.all().order_by('normalized_alias')
    return render(request, 'geography/alias_directory.html', {
        'aliases': aliases
    })

# 5. Ambiguity queue & manual resolution views
@login_required
@permission_required('roster.resolve_geography_ambiguity', raise_exception=True)
def geography_ambiguity_queue(request):
    """
    Renders locations that have not been resolved cleanly, with strict privacy filtering.
    """
    can_view_identity = request.user.has_perm('roster.view_sensitive_roster')

    if not can_view_identity:
        # Use database-level .values() projection to load only allowed geography attributes
        resolutions_values = LocationGeographyResolution.objects.filter(
            status='CURRENT'
        ).exclude(
            match_method__in=['EXACT_PLACE_ZIP_MATCH', 'EXACT_ALIAS_ZIP_MATCH', 'UNIQUE_ZIP_INFERENCE', 'MANUALLY_RESOLVED']
        ).values('id', 'location_id', 'observed_city', 'observed_state', 'observed_zip', 'match_method', 'explanation')

        res_ids = [rv['id'] for rv in resolutions_values]
        candidates = GeographyResolutionCandidate.objects.filter(
            location_resolution_id__in=res_ids
        ).select_related('candidate_county', 'candidate_place')
        
        cand_map = {}
        for c in candidates:
            cand_map.setdefault(c.location_resolution_id, []).append(c)

        payload = []
        for rv in resolutions_values:
            payload.append({
                'res_id': rv['id'],
                'loc_id': rv['location_id'],
                'observed_city': rv['observed_city'],
                'observed_state': rv['observed_state'],
                'observed_zip': rv['observed_zip'],
                'match_method': rv['match_method'],
                'explanation': rv['explanation'],
                'candidates': cand_map.get(rv['id'], []),
                'contributor_name': '[REDACTED - SENSITIVE ROSTER VIEW PERMISSION REQUIRED]',
                'street_address': '[REDACTED]'
            })
    else:
        # User has full access to roster: load full model instances
        resolutions = LocationGeographyResolution.objects.filter(
            status='CURRENT'
        ).exclude(
            match_method__in=['EXACT_PLACE_ZIP_MATCH', 'EXACT_ALIAS_ZIP_MATCH', 'UNIQUE_ZIP_INFERENCE', 'MANUALLY_RESOLVED']
        ).select_related('location__contributor_profile').prefetch_related('candidates__candidate_county', 'candidates__candidate_place')

        payload = []
        for res in resolutions:
            payload.append({
                'res_id': res.id,
                'loc_id': res.location.id,
                'observed_city': res.observed_city,
                'observed_state': res.observed_state,
                'observed_zip': res.observed_zip,
                'match_method': res.match_method,
                'explanation': res.explanation,
                'candidates': list(res.candidates.all()),
                'contributor_name': res.location.contributor_profile.normalized_name if res.location.contributor_profile else 'Unknown',
                'street_address': res.location.street_address or ''
            })

    return render(request, 'geography/ambiguity_queue.html', {
        'resolutions': payload,
        'can_view_identity': can_view_identity
    })

@login_required
@permission_required('roster.resolve_geography_ambiguity', raise_exception=True)
@require_POST
def geography_manual_resolve(request, res_id):
    """
    Accepts administrator override decisions on ambiguous resolutions.
    """
    res = get_object_or_404(LocationGeographyResolution, id=res_id)
    
    county_id = request.POST.get('county_id')
    place_id = request.POST.get('place_id')
    postal_id = request.POST.get('postal_id')
    explanation = request.POST.get('explanation', '')

    with transaction.atomic():
        loc = res.location
        
        matched_county = County.objects.get(id=county_id) if county_id else None
        matched_place = GeographicPlace.objects.get(id=place_id) if place_id else None
        matched_postal = PostalArea.objects.get(id=postal_id) if postal_id else None

        # Supersede the current resolution
        res.status = 'SUPERSEDED'
        res.save()

        # Create new manual resolution run and manual resolution record
        run = GeographyResolutionRun.objects.create(
            trigger_type='SINGLE_LOCATION_REVIEW',
            actor=request.user.username,
            status='COMPLETED',
            locations_considered=1,
            resolved_count=1
        )

        new_res = LocationGeographyResolution.objects.create(
            location=loc,
            resolution_run=run,
            observed_city=loc.city,
            observed_state=loc.state,
            observed_zip=loc.zip,
            matched_canonical_county=matched_county,
            matched_canonical_place=matched_place,
            matched_postal_area=matched_postal,
            match_method='MANUALLY_RESOLVED',
            confidence='HIGH',
            explanation=f"Manual resolution: {explanation}",
            origin='MANUAL',
            actor=request.user.username,
            status='CURRENT',
            superseded_resolution=res
        )

        # Update cache fields on Location
        loc.matched_place = matched_place
        loc.matched_postal_area = matched_postal
        loc.matched_county = matched_county
        loc.match_method = 'MANUALLY_RESOLVED'
        loc.geo_confidence = 'HIGH'
        loc.geo_ambiguity_status = 'RESOLVED'
        loc.geo_explanation = f"Manual resolution: {explanation}"
        loc.save()

        # Create Audit event
        AuditEvent.objects.create(
            event_type='GEOGRAPHY_MANUAL_RESOLUTION',
            description=f"Manually resolved location {loc.id} ({loc.city}, {loc.zip}). Matched County: {matched_county}, Matched Place: {matched_place}.",
            actor=request.user.username
        )

    messages.success(request, "Successfully applied manual geographic resolution.")
    return redirect('geography_ambiguity_queue')

@login_required
@permission_required('roster.view_geography_reference', raise_exception=True)
def geography_resolution_run_detail(request, run_id):
    run = get_object_or_404(GeographyResolutionRun, id=run_id)
    resolutions = LocationGeographyResolution.objects.filter(resolution_run=run).select_related('location')
    return render(request, 'geography/resolution_run_detail.html', {
        'run': run,
        'resolutions': resolutions
    })

@login_required
@permission_required('roster.manage_geography_reference', raise_exception=True)
@require_POST
def geography_run_execute(request, run_id):
    from roster.services.geo_resolver import execute_pending_resolution_run
    try:
        run = execute_pending_resolution_run(run_id, request.user.username)
        messages.success(request, f"Resolution run completed. Considered: {run.locations_considered}, Resolved: {run.resolved_count}.")
    except Exception as e:
        messages.error(request, f"Resolution run failed: {str(e)}")
        # Get run object to redirect to dataset
        run = GeographyResolutionRun.objects.filter(id=run_id).first()
    
    if run and run.dataset_id:
        return redirect('geography_dataset_detail', dataset_id=run.dataset_id)
    return redirect('geography_datasets_list')
