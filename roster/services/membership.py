from datetime import date
from django.db.models import Sum
from roster.models import (
    ContributionCluster, ContributionClusterAssignment, Contribution, 
    ProfilePatternAssessment, MembershipAssessment, MembershipRuleVersion, 
    DatasetCoverageMetadata, ContributorEntity
)

def get_active_contributions_for_cluster(cluster):
    """
    Returns active positive and negative contributions associated with a cluster,
    excluding rolled-back batches.
    """
    if hasattr(cluster, '_prefetched_objects_cache') and 'assignments' in cluster._prefetched_objects_cache:
        assignments = [a for a in cluster.assignments.all() if a.is_active and a.contribution.raw_contribution.import_batch.status == 'COMPLETED']
        return [assign.contribution for assign in assignments]
        
    assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster=cluster,
        is_active=True,
        contribution__raw_contribution__import_batch__status='COMPLETED'
    ).select_related('contribution', 'contribution__raw_contribution__import_batch')
    
    return [assign.contribution for assign in assignments]

def calculate_net_payments(contributions):
    """
    Computes chronological positive contribution events, netting out refunds/reversals.
    """
    positives = {}
    negatives = {}
    
    for c in contributions:
        if c.transaction_type == 'CONTRIBUTION':
            positives[c.transaction_date] = positives.get(c.transaction_date, 0) + c.amount
        else:
            negatives[c.transaction_date] = negatives.get(c.transaction_date, 0) + abs(c.amount)
            
    net_timeline = []
    all_dates = sorted(list(set(list(positives.keys()) + list(negatives.keys()))))
    
    for d in all_dates:
        pos_amt = positives.get(d, 0)
        neg_amt = negatives.get(d, 0)
        net_amt = pos_amt - neg_amt
        if net_amt > 0:
            net_timeline.append({
                'date': d,
                'amount': net_amt
            })
            
    return net_timeline

_active_rule_cache = None
def get_active_rule():
    global _active_rule_cache
    if _active_rule_cache is not None:
        return _active_rule_cache
        
    rule = MembershipRuleVersion.objects.filter(is_active=True).first()
    if not rule:
        # Create a default fallback version
        rule = MembershipRuleVersion.objects.create(
            name="Default Membership Rules",
            monthly_interval_min=20,
            monthly_interval_max=40,
            active_grace_period=60,
            min_recurring_payments=2,
            allowed_amount_variance=0.00,
            skip_payment_allowed=True,
            effective_date=date(2026, 1, 1),
            created_by="SYSTEM",
            is_active=True
        )
    _active_rule_cache = rule
    return rule

_active_coverage_cache = None
def get_active_coverage():
    global _active_coverage_cache
    if _active_coverage_cache is not None:
        return _active_coverage_cache
        
    coverage = DatasetCoverageMetadata.objects.order_by('-coverage_end_date').first()
    if not coverage:
        coverage = DatasetCoverageMetadata.objects.create(
            coverage_start_date=date(2025, 1, 1),
            coverage_end_date=date.today(),
            coverage_complete_through=date.today(),
            coverage_status='UNKNOWN',
            source_obtained_date=date.today()
        )
    _active_coverage_cache = coverage
    return coverage

def clear_membership_caches():
    global _active_rule_cache, _active_coverage_cache
    _active_rule_cache = None
    _active_coverage_cache = None

def evaluate_cluster_recurrence(cluster_id, evaluation_date=None):
    """
    Compatibility wrapper for evaluating cluster recurrence.
    """
    evaluate_cluster_recurrence_bulk([cluster_id], evaluation_date)
    assessment = ProfilePatternAssessment.objects.filter(contribution_cluster_id=cluster_id).first()
    return assessment.calculated_pattern if assessment else 'INSUFFICIENT_HISTORY'

def evaluate_cluster_recurrence_bulk(cluster_ids, evaluation_date=None):
    """
    Bulk evaluates profile recurrence for multiple ContributionClusters.
    """
    if not cluster_ids:
        return
        
    clusters = ContributionCluster.objects.filter(id__in=cluster_ids)
    
    # Fetch active assignments for all clusters in one query
    assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster_id__in=cluster_ids,
        is_active=True,
        contribution__raw_contribution__import_batch__status='COMPLETED'
    ).select_related('contribution', 'contribution__raw_contribution__import_batch')
    
    cluster_contribs = {}
    for assign in assignments:
        cluster_contribs.setdefault(assign.contribution_cluster_id, []).append(assign.contribution)
        
    rule = get_active_rule()
    assessments_to_create = []
    
    for cluster in clusters:
        contributions = cluster_contribs.get(cluster.id, [])
        net_timeline = calculate_net_payments(contributions)
        
        eval_date = evaluation_date
        if not eval_date:
            if net_timeline:
                eval_date = max(item['date'] for item in net_timeline)
            else:
                eval_date = date.today()
                
        pattern = 'INSUFFICIENT_HISTORY'
        explanation = ""
        
        if not net_timeline:
            pattern = 'INSUFFICIENT_HISTORY'
            explanation = "No active positive contributions found in this cluster."
        elif len(net_timeline) == 1:
            pattern = 'SINGLE_VISIBLE_CONTRIBUTION'
            explanation = f"Only one contribution of ${net_timeline[0]['amount']:.2f} was received on {net_timeline[0]['date']}."
        else:
            intervals = []
            for i in range(1, len(net_timeline)):
                diff = (net_timeline[i]['date'] - net_timeline[i-1]['date']).days
                intervals.append(diff)
                
            valid_intervals = 0
            skipped_intervals = 0
            invalid_intervals = 0
            
            for diff in intervals:
                if rule.monthly_interval_min <= diff <= rule.monthly_interval_max:
                    valid_intervals += 1
                elif rule.skip_payment_allowed and (50 <= diff <= 80):
                    skipped_intervals += 1
                else:
                    invalid_intervals += 1
                    
            is_recurring = (valid_intervals + skipped_intervals) >= (rule.min_recurring_payments - 1)
            last_payment_date = net_timeline[-1]['date']
            days_since_last = (eval_date - last_payment_date).days
            is_recent = days_since_last <= rule.active_grace_period
            
            if is_recurring:
                if is_recent:
                    pattern = 'POSSIBLE_RECURRING'
                    explanation = f"Possible active recurring pattern: {len(net_timeline)} contributions received at intervals of {', '.join(map(str, intervals))} days."
                else:
                    pattern = 'PREVIOUSLY_RECURRING'
                    explanation = f"Previously recurring pattern: last contribution was {days_since_last} days ago."
            else:
                if cluster.confidence_level == 'LOW':
                    pattern = 'AMBIGUOUS_IDENTITY_CLUSTER'
                    explanation = f"Ambiguous identity cluster: multiple irregular contributions ({len(net_timeline)}) without corroborating evidence."
                else:
                    pattern = 'INSUFFICIENT_HISTORY'
                    explanation = f"Insufficient history: {len(net_timeline)} contributions received with irregular intervals: {', '.join(map(str, intervals))} days."
                    
        assessments_to_create.append(ProfilePatternAssessment(
            contribution_cluster=cluster,
            calculated_pattern=pattern,
            pattern_explanation=explanation
        ))
        
    # Delete existing and bulk create
    ProfilePatternAssessment.objects.filter(contribution_cluster_id__in=cluster_ids).delete()
    ProfilePatternAssessment.objects.bulk_create(assessments_to_create)
    clear_membership_caches()

def evaluate_membership_for_entity(entity_id, evaluation_date=None):
    """
    Compatibility wrapper for evaluating single entity membership.
    """
    evaluate_membership_for_entities([entity_id], evaluation_date)
    assessment = MembershipAssessment.objects.filter(contributor_entity_id=entity_id).order_by('-calculation_date').first()
    return assessment.calculated_status if assessment else 'UNKNOWN'

def evaluate_membership_for_entities(entity_ids, evaluation_date=None):
    """
    Bulk evaluates membership status for multiple entities.
    """
    if not entity_ids:
        return
        
    entities = ContributorEntity.objects.filter(id__in=entity_ids).prefetch_related('clusters')
    rule = get_active_rule()
    coverage = get_active_coverage()
    
    # Query all active assignments for these entities
    assignments = ContributionClusterAssignment.objects.filter(
        contribution_cluster__contributor_entity_id__in=entity_ids,
        is_active=True,
        contribution__raw_contribution__import_batch__status='COMPLETED'
    ).select_related('contribution', 'contribution_cluster')
    
    entity_contribs = {}
    for assign in assignments:
        ent_id = assign.contribution_cluster.contributor_entity_id
        entity_contribs.setdefault(ent_id, []).append(assign.contribution)
        
    assessments_to_create = []
    
    for entity in entities:
        if entity.entity_type in ['ORGANIZATION', 'JOINT', 'UNKNOWN']:
            assessments_to_create.append(MembershipAssessment(
                contributor_entity=entity,
                calculated_status='UNKNOWN',
                explanation=f"Non-individual entity ({entity.entity_type}) excluded from membership assessments.",
                rule_version=rule
            ))
            continue
            
        if not entity.is_verified:
            has_high_confidence = any(c.confidence_level == 'HIGH' for c in entity.clusters.all())
            if has_high_confidence:
                entity.is_verified = True
                entity.save()
                
        if not entity.is_verified:
            assessments_to_create.append(MembershipAssessment(
                contributor_entity=entity,
                calculated_status='UNKNOWN',
                explanation="Contributor profile is provisional and requires administrative verification before membership status can be confirmed.",
                rule_version=rule
            ))
            continue
            
        contributions = entity_contribs.get(entity.id, [])
        net_timeline = calculate_net_payments(contributions)
        
        eval_date = evaluation_date
        if not eval_date:
            eval_date = coverage.coverage_complete_through
            
        status = 'UNKNOWN'
        explanation = ""
        recurring_amount = 0.00
        payment_interval = ""
        
        is_coverage_complete = coverage.coverage_status in ['CONFIRMED_COMPLETE', 'APPARENTLY_CONTINUOUS']
        days_since_coverage = (date.today() - coverage.coverage_complete_through).days
        is_coverage_stale = days_since_coverage > 60 or not is_coverage_complete
        
        if not net_timeline:
            status = 'UNKNOWN'
            explanation = "No contributions found for this individual."
        elif len(net_timeline) == 1:
            last_payment = net_timeline[0]['date']
            days_since_payment = (eval_date - last_payment).days
            if days_since_payment <= rule.active_grace_period:
                status = 'PROVISIONAL'
                explanation = f"New or provisional member: single contribution of ${net_timeline[0]['amount']:.2f} received on {last_payment}."
            else:
                if is_coverage_stale:
                    status = 'DATASET_TOO_STALE'
                    explanation = f"Single contribution received on {last_payment}, but dataset coverage is stale or incomplete (latest data through {coverage.coverage_complete_through})."
                else:
                    status = 'ONE_TIME'
                    explanation = f"One-time donor: single contribution of ${net_timeline[0]['amount']:.2f} received on {last_payment} (more than {rule.active_grace_period} days ago)."
        else:
            intervals = []
            for i in range(1, len(net_timeline)):
                diff = (net_timeline[i]['date'] - net_timeline[i-1]['date']).days
                intervals.append(diff)
                
            valid_intervals = 0
            skipped_intervals = 0
            
            for diff in intervals:
                if rule.monthly_interval_min <= diff <= rule.monthly_interval_max:
                    valid_intervals += 1
                elif rule.skip_payment_allowed and (50 <= diff <= 80):
                    skipped_intervals += 1
                    
            is_recurring = (valid_intervals + skipped_intervals) >= (rule.min_recurring_payments - 1)
            last_payment_date = net_timeline[-1]['date']
            days_since_last = (eval_date - last_payment_date).days
            is_recent = days_since_last <= rule.active_grace_period
            
            if is_recurring:
                recent_payments = [item['amount'] for item in net_timeline[-3:]]
                recurring_amount = sum(recent_payments) / len(recent_payments)
                payment_interval = "Monthly"
                
                if is_recent:
                    status = 'ACTIVE'
                    explanation = f"Active member: {len(net_timeline)} contributions received. Last contribution was ${net_timeline[-1]['amount']:.2f} on {last_payment_date} ({days_since_last} days ago)."
                else:
                    if is_coverage_stale:
                        status = 'PREVIOUSLY_RECURRING'
                        explanation = f"Previously recurring member: last contribution was on {last_payment_date}, but current membership status cannot be verified because the available dataset is stale (coverage ends {coverage.coverage_complete_through})."
                    else:
                        status = 'LAPSED'
                        explanation = f"Lapsed member: previously recurring, but last contribution was {days_since_last} days ago (limit {rule.active_grace_period} days)."
            else:
                status = 'INSUFFICIENT_HISTORY'
                explanation = f"Insufficient history: {len(net_timeline)} contributions received with irregular intervals: {', '.join(map(str, intervals))} days."
                
        assessments_to_create.append(MembershipAssessment(
            contributor_entity=entity,
            calculated_status=status,
            recurring_amount=recurring_amount,
            payment_interval=payment_interval,
            rule_version=rule,
            explanation=explanation
        ))
        
    MembershipAssessment.objects.bulk_create(assessments_to_create)
    clear_membership_caches()
