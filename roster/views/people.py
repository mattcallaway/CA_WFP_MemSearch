import csv
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.db.models import Q, Sum, Max, Count
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.core.exceptions import PermissionDenied

from roster.models import (
    ContributorEntity, Person, Contribution, ContributionCluster, 
    ContributionClusterAssignment, MembershipAssessment, Location, 
    AuditEvent, MergeDecision, RawContribution, MembershipRuleVersion, Organization
)
from roster.services.resolver import merge_clusters, split_cluster, normalize_name
from roster.services.membership import evaluate_membership_for_entity, evaluate_cluster_recurrence_bulk, evaluate_membership_for_entities

def sanitize_csv_cell(value):
    """
    Escapes spreadsheet formula characters (=, +, -, @) to prevent CSV Injection.
    """
    if value is None:
        return ""
    val_str = str(value)
    if val_str and val_str[0] in ['=', '+', '-', '@']:
        return "'" + val_str
    return val_str

@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def people_list(request):
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    # We query verified or unverified ContributorEntity of type INDIVIDUAL
    people = ContributorEntity.objects.filter(entity_type='INDIVIDUAL')
    
    if status_filter:
        latest_ids = MembershipAssessment.objects.values('contributor_entity_id').annotate(max_id=Max('id')).values('max_id')
        if status_filter == 'UNKNOWN':
            assessed_entity_ids = MembershipAssessment.objects.values_list('contributor_entity_id', flat=True).distinct()
            matching_entity_ids = list(MembershipAssessment.objects.filter(
                id__in=latest_ids,
                calculated_status='UNKNOWN'
            ).values_list('contributor_entity_id', flat=True))
            people = people.filter(
                Q(id__in=matching_entity_ids) | ~Q(id__in=assessed_entity_ids)
            )
        else:
            matching_entity_ids = list(MembershipAssessment.objects.filter(
                id__in=latest_ids,
                calculated_status=status_filter
            ).values_list('contributor_entity_id', flat=True))
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

from decimal import Decimal

@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
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
    
    positives = active_contribs.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    negatives = active_contribs.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
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
@permission_required('roster.override_membership', raise_exception=True)
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
@permission_required('roster.manage_identity', raise_exception=True)
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
@permission_required('roster.manage_identity', raise_exception=True)
def split_profiles(request, merge_decision_id):
    merge_dec = get_object_or_404(MergeDecision, id=merge_decision_id)
    split_cluster(merge_dec.id, actor=request.user.username)
    evaluate_membership_for_entity(merge_dec.source_cluster.contributor_entity_id)
    evaluate_membership_for_entity(merge_dec.target_cluster.contributor_entity_id)
    messages.success(request, "Profiles split successfully.")
    return redirect('person_profile', entity_id=merge_dec.target_cluster.contributor_entity_id)

@login_required
@permission_required('roster.export_sensitive_data', raise_exception=True)
def export_roster(request):
    """
    Exports individual contributor roster to CSV.
    Neutralizes CSV formula injection characters (=, +, -, @).
    """
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="wfp_roster_export.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Entity ID',
        'Display Name',
        'Entity Type',
        'Verified Status',
        'Membership Status',
        'Recurring Amount',
        'Payment Interval'
    ])
    
    entities = ContributorEntity.objects.filter(entity_type='INDIVIDUAL').order_by('display_name')
    record_count = 0
    
    for ent in entities:
        latest_ass = MembershipAssessment.objects.filter(contributor_entity=ent).order_by('-calculation_date').first()
        status = latest_ass.calculated_status if latest_ass else 'UNKNOWN'
        rec_amt = latest_ass.recurring_amount if latest_ass else 0.00
        interval = latest_ass.payment_interval if latest_ass else ''
        
        writer.writerow([
            sanitize_csv_cell(ent.id),
            sanitize_csv_cell(ent.display_name),
            sanitize_csv_cell(ent.entity_type),
            sanitize_csv_cell(ent.is_verified),
            sanitize_csv_cell(status),
            sanitize_csv_cell(rec_amt),
            sanitize_csv_cell(interval)
        ])
        record_count += 1
        
    # Log export audit event
    AuditEvent.objects.create(
        event_type="EXPORT_ROSTER",
        description=f"Exported individual contributor roster to CSV. Records: {record_count}.",
        actor=request.user.username
    )
    
    return response

@login_required
@require_POST
@permission_required('roster.manage_identity', raise_exception=True)
def correct_entity_type(request, entity_id):
    """
    Structurally safe correction of contributor entity type.
    """
    entity = get_object_or_404(ContributorEntity, id=entity_id)
    new_type = request.POST.get('entity_type')
    reason = request.POST.get('reason', '').strip()
    
    if not reason:
        return HttpResponseBadRequest("A reason for entity reclassification is required.")
        
    if new_type not in dict(ContributorEntity.ENTITY_TYPE_CHOICES):
        return HttpResponseBadRequest("Invalid entity type selection.")
        
    old_type = entity.entity_type
    if old_type == new_type:
        messages.info(request, "Entity type matches current type. No changes made.")
        return redirect('person_profile', entity_id=entity.id)
        
    # Process reclassification inside a transaction
    from django.db import transaction
    with transaction.atomic():
        entity.entity_type = new_type
        entity.save()
        
        # Extensions Cleanup/Setup
        if new_type == 'INDIVIDUAL':
            # Delete organization profiles
            Organization.objects.filter(contributor_entity=entity).delete()
            # Construct Person profile
            parsed = normalize_name(entity.display_name)
            Person.objects.update_or_create(
                contributor_entity=entity,
                defaults={
                    'first_name': parsed['first_name'],
                    'middle_name': parsed['middle_name'],
                    'last_name': parsed['last_name'],
                    'suffix': parsed['suffix']
                }
            )
        else:
            # Delete Person profile
            Person.objects.filter(contributor_entity=entity).delete()
            if new_type == 'ORGANIZATION':
                Organization.objects.update_or_create(
                    contributor_entity=entity,
                    defaults={'legal_name': entity.display_name}
                )
            else:
                # Joint or Unknown has no Organization profile extension
                Organization.objects.filter(contributor_entity=entity).delete()
                
            # Declassify Individual Membership
            # Since non-individuals cannot have individual membership, set status to UNKNOWN
            rule = MembershipRuleVersion.objects.filter(is_active=True).first()
            MembershipAssessment.objects.create(
                contributor_entity=entity,
                calculated_status='UNKNOWN',
                recurring_amount=0.00,
                payment_interval="Reclassified",
                rule_version=rule,
                manual_override=False,
                explanation=f"Reclassified from INDIVIDUAL to {new_type}. Membership invalidated."
            )
            
            # Remove any ProfilePatternAssessments for this entity's clusters
            from roster.models import ProfilePatternAssessment
            ProfilePatternAssessment.objects.filter(contribution_cluster__contributor_entity=entity).delete()
            
        # Re-run assessment engines on all entity's clusters
        cluster_ids = list(entity.clusters.values_list('id', flat=True))
        evaluate_cluster_recurrence_bulk(cluster_ids)
        evaluate_membership_for_entities([entity.id])
        
        # Audit Log
        AuditEvent.objects.create(
            event_type="CORRECT_ENTITY_TYPE",
            description=f"Corrected entity type for Entity {entity.id} from '{old_type}' to '{new_type}'. Reason: {reason}.",
            actor=request.user.username
        )
        
    messages.success(request, f"Entity type reclassified to {new_type}.")
    return redirect('person_profile', entity_id=entity.id)

@login_required
@permission_required('roster.view_audit', raise_exception=True)
def audit_history(request):
    """
    Renders system audit history events logs view.
    """
    events = AuditEvent.objects.all().order_by('-timestamp')
    return render(request, 'audit_history.html', {'events': events})
