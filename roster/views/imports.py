import os
import csv
import hashlib
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.core.files.storage import FileSystemStorage
from django.contrib import messages
from django.conf import settings
from django.core.exceptions import PermissionDenied

from roster.models import ImportBatch, ImportMappingProfile, RawContribution, Contribution, ImportAttempt, AuditEvent
from roster.services.importer import import_csv_file, rollback_batch, restore_batch

# Security upload settings
ALLOWED_EXTENSIONS = ['.csv']
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def imports_list(request):
    batches = ImportBatch.objects.all().order_by('-import_date')
    mapping_profiles = ImportMappingProfile.objects.all()
    context = {
        'batches': batches,
        'mapping_profiles': mapping_profiles
    }
    return render(request, 'imports/list.html', context)

@login_required
@require_POST
@permission_required('roster.import_contributions', raise_exception=True)
def imports_upload(request):
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, "No file uploaded.")
        return redirect('imports_list')
        
    ext = os.path.splitext(csv_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        messages.error(request, "Invalid file format. Only CSV files are allowed.")
        return redirect('imports_list')
        
    if csv_file.size > MAX_FILE_SIZE:
        messages.error(request, "File exceeds maximum size of 10MB.")
        return redirect('imports_list')
        
    upload_dir = os.path.join(settings.BASE_DIR, 'scratch', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    
    fs = FileSystemStorage(location=upload_dir)
    filename = fs.save(csv_file.name, csv_file)
    file_path = fs.path(filename)
    
    # Calculate file hash
    hasher = hashlib.sha256()
    for chunk in csv_file.chunks():
        hasher.update(chunk)
    file_hash = hasher.hexdigest()
    
    override_requested = request.POST.get('override_duplicate') == 'true'
    is_duplicate = ImportBatch.objects.filter(file_hash=file_hash, status='COMPLETED').exists()
    
    if is_duplicate:
        if not override_requested:
            # Audit failed attempt
            ImportAttempt.objects.create(
                attempted_by=request.user.username,
                action="UPLOAD_FAILED",
                notes=f"Attempted to upload duplicate file hash '{file_hash}' without setting override_duplicate=true."
            )
            messages.error(request, "A file with this content has already been imported. Select 'Reprocess duplicate files' to override.")
            os.remove(file_path)
            return redirect('imports_list')
        else:
            # Must possess both permissions
            if not request.user.has_perm('roster.override_duplicate_file'):
                ImportAttempt.objects.create(
                    attempted_by=request.user.username,
                    action="UPLOAD_OVERRIDE_UNAUTHORIZED",
                    notes=f"Unauthorized override attempt for file hash '{file_hash}'."
                )
                os.remove(file_path)
                messages.error(request, "You do not have permission to override duplicate files.")
                raise PermissionDenied("Lacks override_duplicate_file permission.")
                
    # Create or update pending batch
    if is_duplicate:
        # Reprocess uses the existing batch ID
        batch = ImportBatch.objects.get(file_hash=file_hash, status='COMPLETED')
        # Mark pending for previewing
        batch.status = 'PENDING'
        batch.save()
    else:
        batch = ImportBatch.objects.create(
            file_name=csv_file.name,
            file_hash=file_hash,
            file_type="CSV",
            imported_by=request.user.username,
            status='PENDING'
        )
        
    # Create persistent ImportAttempt audit
    ImportAttempt.objects.create(
        import_batch=batch,
        attempted_by=request.user.username,
        action="UPLOAD_SUCCESS",
        notes=f"Successfully uploaded filename '{csv_file.name}' (size: {csv_file.size} bytes, hash: {file_hash}). Override: {override_requested}."
    )
    
    request.session[f'upload_path_{batch.id}'] = file_path
    if override_requested:
        request.session[f'override_duplicate_{batch.id}'] = True
        
    return redirect('imports_preview', batch_id=batch.id)

@login_required
@permission_required('roster.import_contributions', raise_exception=True)
def imports_preview(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    file_path = request.session.get(f'upload_path_{batch.id}')
    
    if not file_path or not os.path.exists(file_path):
        messages.error(request, "Temp file not found or expired. Please upload again.")
        return redirect('imports_list')
        
    headers = []
    preview_rows = []
    with open(file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
            for _ in range(5):
                row = next(reader, None)
                if row is not None:
                    preview_rows.append(row)
        except StopIteration:
            pass
            
    target_fields = [
        'NAME OF CONTRIBUTOR',
        'PAYMENT TYPE',
        'STREET ADDRESS',
        'CITY',
        'STATE',
        'ZIP',
        'ID NUMBER',
        'EMPLOYER',
        'OCCUPATION',
        'AMOUNT',
        'TRANSACTION DATE',
        'FILED DATE',
        'TRANSACTION NUMBER'
    ]
    
    mapping_profiles = ImportMappingProfile.objects.all()
    
    context = {
        'batch': batch,
        'headers': headers,
        'preview_rows': preview_rows,
        'target_fields': target_fields,
        'mapping_profiles': mapping_profiles
    }
    return render(request, 'imports/preview.html', context)

@login_required
@require_POST
@permission_required('roster.import_contributions', raise_exception=True)
def imports_process(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    file_path = request.session.get(f'upload_path_{batch.id}')
    
    if not file_path or not os.path.exists(file_path):
        messages.error(request, "Temp file not found or expired.")
        return redirect('imports_list')
        
    mappings = {}
    target_fields = request.POST.getlist('target_fields[]')
    csv_columns = request.POST.getlist('csv_columns[]')
    
    for tgt, col in zip(target_fields, csv_columns):
        if col:
            mappings[tgt] = col
            
    if 'NAME OF CONTRIBUTOR' not in mappings or 'AMOUNT' not in mappings or 'TRANSACTION DATE' not in mappings:
        messages.error(request, "You must map Name of Contributor, Amount, and Transaction Date at minimum.")
        return redirect('imports_preview', batch_id=batch.id)
        
    profile_name = request.POST.get('save_profile_name', '').strip()
    if profile_name:
        profile = ImportMappingProfile.objects.create(
            name=profile_name,
            mapping_rules=mappings,
            version='1.0',
            owner=request.user
        )
    else:
        profile_id = request.POST.get('use_profile_id')
        if profile_id:
            profile = ImportMappingProfile.objects.get(id=profile_id)
        else:
            profile = ImportMappingProfile.objects.create(
                name=f"One-off mapping for {batch.file_name}",
                mapping_rules=mappings,
                version='1.0',
                owner=request.user
            )
            
    override_active = request.session.get(f'override_duplicate_{batch.id}') == True
    
    # Process
    try:
        import_csv_file(
            file_path=file_path,
            file_name=batch.file_name,
            mapping_profile_id=profile.id,
            actor=request.user.username,
            override_duplicate=override_active
        )
        messages.success(request, "File successfully processed.")
        
        # Trigger geographic resolution run for new locations after import commits
        from roster.services.geo_resolver import resolve_geographic_locations
        try:
            resolve_geographic_locations(actor=request.user.username, trigger_type='POST_CONTRIBUTION_IMPORT')
        except Exception as resolve_err:
            from roster.models import AuditEvent
            AuditEvent.objects.create(
                event_type='GEOGRAPHY_RESOLUTION_FAILED',
                description=f"Auto resolution run post-import failed: {str(resolve_err)}",
                actor=request.user.username
            )
    except Exception as e:
        batch.status = 'FAILED'
        batch.save()
        messages.error(request, f"Import failed: {str(e)}")
        
    if f'upload_path_{batch.id}' in request.session:
        del request.session[f'upload_path_{batch.id}']
    if f'override_duplicate_{batch.id}' in request.session:
        del request.session[f'override_duplicate_{batch.id}']
        
    try:
        os.remove(file_path)
    except OSError:
        pass
        
    return redirect('imports_list')

@login_required
@require_POST
@permission_required('roster.rollback_import', raise_exception=True)
def imports_rollback(request, batch_id):
    try:
        rollback_batch(batch_id, actor=request.user.username)
        messages.success(request, "Batch rolled back successfully.")
    except Exception as e:
        messages.error(request, f"Rollback failed: {str(e)}")
    return redirect('imports_list')

@login_required
@require_POST
@permission_required('roster.restore_import', raise_exception=True)
def imports_restore(request, batch_id):
    try:
        restore_batch(batch_id, actor=request.user.username)
        messages.success(request, "Batch restored successfully.")
    except Exception as e:
        messages.error(request, f"Restoration failed: {str(e)}")
    return redirect('imports_list')

@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def imports_failures(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    failures = RawContribution.objects.filter(
        import_batch=batch,
        validation_status__in=['VALIDATION_FAILURE', 'VALIDATION_WARNING', 'EXACT_DUPLICATE', 'POSSIBLE_DUPLICATE', 'POSSIBLE_AMENDMENT']
    )
    
    # Audit failure view access
    AuditEvent.objects.create(
        event_type="VIEW_FAILURES",
        description=f"User {request.user.username} viewed failures report for batch {batch_id} ('{batch.file_name}').",
        actor=request.user.username
    )
    
    context = {
        'batch': batch,
        'failures': failures
    }
    return render(request, 'imports/failures.html', context)
