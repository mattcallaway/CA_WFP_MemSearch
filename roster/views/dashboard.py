from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Sum
from roster.models import ImportBatch, Contribution, ContributorEntity, MembershipAssessment, RawContribution

def dashboard_redirect(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return redirect('login')

@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def dashboard(request):
    # Filter active contributions (excluding rolled-back batches)
    active_contribs = Contribution.objects.filter(
        raw_contribution__import_batch__status='COMPLETED',
        status='ACTIVE'
    )
    
    total_txns = active_contribs.count()
    total_volume = active_contribs.aggregate(total=Sum('amount'))['total'] or 0.00
    
    # Individuals count
    unique_people_count = ContributorEntity.objects.filter(entity_type='INDIVIDUAL').count()
    
    # Membership Status Breakdown (taking the latest assessment per entity)
    # For database compatibility, order by date and filter unique records in Python
    all_assessments = MembershipAssessment.objects.filter(
        contributor_entity__entity_type='INDIVIDUAL'
    ).order_by('contributor_entity_id', '-calculation_date')
    
    latest_assessments = []
    seen_entities = set()
    for ass in all_assessments:
        if ass.contributor_entity_id not in seen_entities:
            seen_entities.add(ass.contributor_entity_id)
            latest_assessments.append(ass)
    
    status_counts = {
        'ACTIVE': 0,
        'PROVISIONAL': 0,
        'LIKELY': 0,
        'PREVIOUSLY_RECURRING': 0,
        'LAPSED': 0,
        'ONE_TIME': 0,
        'UNKNOWN': 0,
        'INSUFFICIENT_HISTORY': 0,
        'DATASET_TOO_STALE': 0,
    }
    
    for ass in latest_assessments:
        status = ass.calculated_status
        if status in status_counts:
            status_counts[status] += 1
            
    # Failures and duplicates count from active batches
    failed_rows = RawContribution.objects.filter(
        validation_status='VALIDATION_FAILURE'
    ).count()
    
    duplicate_rows = RawContribution.objects.filter(
        validation_status='EXACT_DUPLICATE'
    ).count()
    
    # Recent imports
    recent_imports = ImportBatch.objects.all().order_by('-import_date')[:5]
    
    context = {
        'total_transactions': total_txns,
        'total_volume': total_volume,
        'unique_people': unique_people_count,
        'status_counts': status_counts,
        'failed_rows': failed_rows,
        'duplicate_rows': duplicate_rows,
        'recent_imports': recent_imports,
    }
    
    return render(request, 'dashboard.html', context)
