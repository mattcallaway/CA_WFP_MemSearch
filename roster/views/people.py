from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.db.models import Q, Sum, Max, Count
from django.contrib import messages
from roster.models import (
    ContributorEntity, Person, Contribution, ContributionCluster, 
    ContributionClusterAssignment, MembershipAssessment, Location, 
    AuditEvent, MergeDecision, RawContribution, MembershipRuleVersion
)
from roster.services.resolver import merge_clusters, split_cluster
from roster.services.membership import evaluate_membership_for_entity

@login_required
@permission_required('roster.view_contributorentity', raise_exception=True)
def people_list(request):
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    # We query verified or unverified ContributorEntity of type INDIVIDUAL
    people = ContributorEntity.objects.filter(entity_type='INDIVIDUAL')
    
    if status_filter:
        latest_assessments = MembershipAssessment.objects.order_by('contributor_entity', '-calculation_date').distinct('contributor_entity')
        matching_entity_ids = [ass.contributor_entity_id for ass in latest_assessments if ass.calculated_status == status_filter]
        people = people.filter(id__in=matching_entity_ids)
        
    if query:
        people = people.filter(
            Q(display_name__icontains=query) |
            Q(person_profile__first_name__icontains=query) |
            Q(person_profile__last_name__icontains=query) |
            Q(clusters__zip_code__icontains=query) |
            Q(clusters__locations__city__icontains=query) |
            Q(clusters__assignments__contribution__employer__icontains=query) |
            Q(clusters__assignments__contribution__occupation__icontains=query) |
            Q(clusters__assignments__contribution__transaction_number__icontains=query)
        ).distinct()
        
    people_data = []
    for entity in people:
        latest_ass = MembershipAssessment.objects.filter(contributor_entity=entity).order_by('-calculation_date').first()
        status = latest_ass.calculated_status if latest_ass else 'UNKNOWN'
        rec_amt = latest_ass.recurring_amount if latest_ass else 0.00
        
        active_contribs = Contribution.objects.filter(
            assignments__contribution_cluster__contributor_entity=entity,
            raw_contribution__import_batch__status='COMPLETED',
            status='ACTIVE'
        )
        
        totals = active_contribs.aggregate(
            total=Sum('amount'),
            last_date=Max('transaction_date'),
            count=Count('id')
        )
        
        total_contrib = totals['total'] or 0.00
        last_date = totals['last_date']
        contrib_count = totals['count']
        
        loc = Location.objects.filter(
            contributor_profile__contributor_entity=entity,
            status='CURRENT'
        ).first()
        city = loc.city if loc else ''
        zip_code = loc.zip if loc else ''
        
        people_data.append({
            'entity': entity,
            'status': status,
            'recurring_amount': rec_amt,
            'last_contribution': last_date,
            'total_contributed': total_contrib,
            'city': city,
            'zip_code': zip_code,
            'contribution_count': contrib_count,
            'flags': entity.data_quality_flags
        })
        
    context = {
        'people': people_data,
        'query': query,
        'status_filter': status_filter,
        'statuses': MembershipAssessment.STATUS_CHOICES
    }
    return render(request, 'people/list.html', context)

@login_required
@permission_required('roster.view_contributorentity', raise_exception=True)
def person_profile(request, entity_id):
    entity = get_object_or_404(ContributorEntity, id=entity_id)
    
    active_contribs = Contribution.objects.filter(
        assignments__contribution_cluster__contributor_entity=entity,
        assignments__is_active=True,
        raw_contribution__import_batch__status='COMPLETED'
    ).order_by('-transaction_date')
    
    totals = active_contribs.aggregate(
        total=Sum('amount'),
        count=Count('id')
    )
    
    positives = active_contribs.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or 0.00
    negatives = active_contribs.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or 0.00
    net_total = positives + negatives
    
    timeline = []
    for c in active_contribs:
        raw_name = c.raw_contribution.original_values.get('NAME OF CONTRIBUTOR', '')
        timeline.append({
            'contribution': c,
            'raw_name': raw_name
        })
        
    locations = Location.objects.filter(
        contributor_profile__contributor_entity=entity
    ).order_by('-effective_date')
    
    assessments = MembershipAssessment.objects.filter(
        contributor_entity=entity
    ).order_by('-calculation_date')
    
    merge_decisions = MergeDecision.objects.filter(
        Q(source_cluster__contributor_entity=entity) | 
        Q(target_cluster__contributor_entity=entity),
        is_active=True
    ).select_related('source_cluster', 'target_cluster')
    
    audit_events = AuditEvent.objects.filter(
        description__contains=f"Entity {entity.id}"
    ).order_by('-timestamp')
    if not audit_events.exists():
        audit_events = AuditEvent.objects.filter(
            description__contains=f"Cluster "
        ).order_by('-timestamp')[:5]
        
    candidates = ContributorEntity.objects.filter(
        entity_type='INDIVIDUAL'
    ).exclude(id=entity.id)
    
    context = {
        'entity': entity,
        'timeline': timeline,
        'total_contributed': positives,
        'net_contributed': net_total,
        'contribution_count': totals['count'],
        'locations': locations,
        'assessments': assessments,
        'latest_assessment': assessments.first(),
        'merge_decisions': merge_decisions,
        'audit_events': audit_events,
        'merge_candidates': candidates
    }
    return render(request, 'people/profile.html', context)

@login_required
@require_POST
@permission_required('roster.change_contributorentity', raise_exception=True)
def membership_override(request, entity_id):
    entity = get_object_or_404(ContributorEntity, id=entity_id)
    status = request.POST.get('status')
    explanation = request.POST.get('explanation', '').strip()
    
    rule = MembershipRuleVersion.objects.filter(is_active=True).first()
    
    MembershipAssessment.objects.create(
        contributor_entity=entity,
        calculated_status=status,
        recurring_amount=0.00,
        payment_interval="Manual",
        rule_version=rule,
        manual_override=True,
        explanation=f"Manual Override: {explanation}"
    )
    
    AuditEvent.objects.create(
        event_type="MANUAL_OVERRIDE",
        description=f"Applied manual membership override for Entity {entity.id} to '{status}'. Reason: {explanation}.",
        actor=request.user.username
    )
    
    messages.success(request, "Manual membership override applied.")
    return redirect('person_profile', entity_id=entity.id)

@login_required
@require_POST
@permission_required('roster.change_contributorentity', raise_exception=True)
def merge_profiles(request):
    source_entity_id = request.POST.get('source_entity_id')
    target_entity_id = request.POST.get('target_entity_id')
    
    source_entity = get_object_or_404(ContributorEntity, id=source_entity_id)
    target_entity = get_object_or_404(ContributorEntity, id=target_entity_id)
    
    source_cluster = source_entity.clusters.first()
    target_cluster = target_entity.clusters.first()
    
    if source_cluster and target_cluster:
        merge_clusters(source_cluster.id, target_cluster.id, actor=request.user.username)
        evaluate_membership_for_entity(target_entity.id)
        evaluate_membership_for_entity(source_entity.id)
        messages.success(request, "Profiles merged successfully.")
        return redirect('person_profile', entity_id=target_entity.id)
    else:
        messages.error(request, "Failed to locate clusters for merging.")
        
    return redirect('people_list')

@login_required
@require_POST
@permission_required('roster.change_contributorentity', raise_exception=True)
def split_profiles(request, merge_decision_id):
    merge_dec = get_object_or_404(MergeDecision, id=merge_decision_id)
    split_cluster(merge_dec.id, actor=request.user.username)
    evaluate_membership_for_entity(merge_dec.source_cluster.contributor_entity_id)
    evaluate_membership_for_entity(merge_dec.target_cluster.contributor_entity_id)
    messages.success(request, "Profiles split successfully.")
    return redirect('person_profile', entity_id=merge_dec.target_cluster.contributor_entity_id)
