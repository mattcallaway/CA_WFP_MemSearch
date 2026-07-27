import os
import csv
from django.shortcuts import render, redirect
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.core.files.storage import FileSystemStorage
from django.contrib import messages
from django.conf import settings
from roster.models import ImportBatch, ImportMappingProfile, RawContribution, Contribution
from roster.services.importer import import_csv_file, rollback_batch, restore_batch

# Security upload settings
ALLOWED_EXTENSIONS = ['.csv']
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

@login_required
@permission_required('roster.view_importbatch', raise_exception=True)
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
@permission_required('roster.add_importbatch', raise_exception=True)
def imports_upload(request):
    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, "No file uploaded.")
        return redirect('imports_list')
        
    # File checks
    ext = os.path.splitext(csv_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        messages.error(request, "Invalid file format. Only CSV files are allowed.")
        return redirect('imports_list')
        
    if csv_file.size > MAX_FILE_SIZE:
        messages.error(request, "File exceeds maximum size of 10MB.")
        return redirect('imports_list')
        
    # Save file to a temporary location inside workspace
    upload_dir = os.path.join(settings.BASE_DIR, 'scratch', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    
    fs = FileSystemStorage(location=upload_dir)
    filename = fs.save(csv_file.name, csv_file)
    file_path = fs.path(filename)
    
    # Create a pending batch
    import hashlib
    hasher = hashlib.sha256()
    for chunk in csv_file.chunks():
        hasher.update(chunk)
    file_hash = hasher.hexdigest()
    
    # If file hash exists and is COMPLETED, block
    if ImportBatch.objects.filter(file_hash=file_hash, status='COMPLETED').exists():
        messages.error(request, "A file with this content has already been imported.")
        os.remove(file_path)
        return redirect('imports_list')
        
    # Create pending batch
    batch = ImportBatch.objects.create(
        file_name=csv_file.name,
        file_hash=file_hash,
        file_type="CSV",
        imported_by=request.user.username,
        status='PENDING'
    )
    
    # Stash the path in session or store on batch
    request.session[f'upload_path_{batch.id}'] = file_path
    
    return redirect('imports_preview', batch_id=batch.id)

@login_required
@permission_required('roster.add_importbatch', raise_exception=True)
def imports_preview(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    file_path = request.session.get(f'upload_path_{batch.id}')
    
    if not file_path or not os.path.exists(file_path):
        messages.error(request, "Temp file not found or expired. Please upload again.")
        return redirect('imports_list')
        
    # Read headers and first few rows
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
            
    # List of WFP standard target fields
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
@permission_required('roster.add_importbatch', raise_exception=True)
def imports_process(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    file_path = request.session.get(f'upload_path_{batch.id}')
    
    if not file_path or not os.path.exists(file_path):
        messages.error(request, "Temp file not found or expired.")
        return redirect('imports_list')
        
    # Retrieve column mappings
    mappings = {}
    target_fields = request.POST.getlist('target_fields[]')
    csv_columns = request.POST.getlist('csv_columns[]')
    
    for tgt, col in zip(target_fields, csv_columns):
        if col: # only map if a CSV header is selected
            mappings[tgt] = col
            
    # Validate that we have the minimum required mappings: Name, Amount, Date
    if 'NAME OF CONTRIBUTOR' not in mappings or 'AMOUNT' not in mappings or 'TRANSACTION DATE' not in mappings:
        messages.error(request, "You must map Name of Contributor, Amount, and Transaction Date at minimum.")
        return redirect('imports_preview', batch_id=batch.id)
        
    # Check if we should save as a new mapping profile
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
            # Create a temporary one-off mapping profile
            profile = ImportMappingProfile.objects.create(
                name=f"One-off mapping for {batch.file_name}",
                mapping_rules=mappings,
                version='1.0',
                owner=request.user
            )
            
    # Process the file
    try:
        import_csv_file(
            file_path=file_path,
            file_name=batch.file_name,
            mapping_profile_id=profile.id,
            actor=request.user.username,
            override_duplicate=True
        )
        batch.delete()
        messages.success(request, "File successfully processed.")
    except Exception as e:
        batch.status = 'FAILED'
        batch.save()
        messages.error(request, f"Import failed: {str(e)}")
        
    # Clean up temp file path in session
    if f'upload_path_{batch.id}' in request.session:
        del request.session[f'upload_path_{batch.id}']
    try:
        os.remove(file_path)
    except OSError:
        pass
        
    return redirect('imports_list')

@login_required
@require_POST
@permission_required('roster.change_importbatch', raise_exception=True)
def imports_rollback(request, batch_id):
    try:
        rollback_batch(batch_id, actor=request.user.username)
        messages.success(request, "Batch rolled back successfully.")
    except Exception as e:
        messages.error(request, f"Rollback failed: {str(e)}")
    return redirect('imports_list')

@login_required
@require_POST
@permission_required('roster.change_importbatch', raise_exception=True)
def imports_restore(request, batch_id):
    try:
        restore_batch(batch_id, actor=request.user.username)
        messages.success(request, "Batch restored successfully.")
    except Exception as e:
        messages.error(request, f"Restoration failed: {str(e)}")
    return redirect('imports_list')

@login_required
@permission_required('roster.view_importbatch', raise_exception=True)
def imports_failures(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    failures = RawContribution.objects.filter(
        import_batch=batch,
        validation_status__in=['VALIDATION_FAILURE', 'VALIDATION_WARNING', 'EXACT_DUPLICATE', 'POSSIBLE_DUPLICATE', 'POSSIBLE_AMENDMENT']
    )
    context = {
        'batch': batch,
        'failures': failures
    }
    return render(request, 'imports/failures.html', context)
