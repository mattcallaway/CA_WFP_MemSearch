from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.db import transaction, models
from django.contrib import messages
from django.utils import timezone
from django.http import HttpResponseForbidden

from roster.models import (
    Chapter, ChapterRuleSet, ChapterRule, ChapterEntityOverride,
    ChapterEvaluationRun, ChapterEvaluationLocationSelection,
    ChapterEvaluationResult, ChapterRuleMatch, ChapterAssignment,
    ContributorEntity, County, GeographicPlace, PostalArea, AuditEvent,
    GeographyDataset
)
from roster.services.chapter_lifecycle import (
    create_draft_ruleset, add_rule_to_ruleset, deactivate_rule, activate_ruleset
)
from roster.services.chapter_overrides import (
    create_override, revoke_override, expire_override
)
from roster.services.chapter_engine import run_chapter_evaluation, RESOLVER_VERSION, ENGINE_VERSION


@login_required
@permission_required('roster.view_chapter_definitions', raise_exception=True)
def chapter_list(request):
    """
    Renders list of chapters, rulesets, and aggregate metrics.
    """
    chapters = Chapter.objects.all().order_by('name')
    
    # Calculate aggregate counts per active run
    chapter_payloads = []
    active_runs = []
    
    for ch in chapters:
        run = ch.current_evaluation_run
        payload = {
            'chapter': ch,
            'active_ruleset': ch.rule_sets.filter(status='ACTIVE').first(),
            'run': run,
            'counts': {'INCLUDED': 0, 'PROVISIONALLY_INCLUDED': 0, 'EXCLUDED': 0, 'AMBIGUOUS': 0, 'UNRESOLVED': 0, 'NO_MATCH': 0, 'INELIGIBLE': 0}
        }
        if run:
            active_runs.append(run.id)
            assign_counts = ChapterAssignment.objects.filter(evaluation_run=run).values('assignment_status').annotate(total=models.Count('id'))
            for item in assign_counts:
                status = item['assignment_status']
                if status in payload['counts']:
                    payload['counts'][status] = item['total']
        chapter_payloads.append(payload)

    # Compute overlaps dynamically
    overlap_count = 0
    if len(active_runs) > 1:
        overlap_count = ChapterAssignment.objects.filter(
            evaluation_run_id__in=active_runs,
            assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']
        ).values('contributor_entity_id').annotate(cnt=models.Count('id')).filter(cnt__gt=1).count()

    return render(request, 'chapters/list.html', {
        'chapters': chapter_payloads,
        'overlap_count': overlap_count
    })


@login_required
@permission_required('roster.view_chapter_definitions', raise_exception=True)
def chapter_detail(request, chapter_id):
    """
    Renders detail page of a chapter, historical rulesets, runs, and overrides.
    """
    chapter = get_object_or_404(Chapter, id=chapter_id)
    rulesets = chapter.rule_sets.all().order_by('-version')
    active_ruleset = rulesets.filter(status='ACTIVE').first()
    runs = chapter.evaluation_runs.all().order_by('-started_time')
    
    # Preload active rules
    active_rules = []
    if active_ruleset:
        active_rules = active_ruleset.rules.filter(is_active=True).order_by('display_order', 'id')

    # Aggregates
    override_count = chapter.overrides.filter(status='ACTIVE').count()
    
    can_view_identity = request.user.has_perm('roster.view_sensitive_roster')
    can_view_assignments = request.user.has_perm('roster.view_chapter_assignments')

    # Assignments list
    assignments = []
    if chapter.current_evaluation_run:
        assign_qs = ChapterAssignment.objects.filter(
            evaluation_run=chapter.current_evaluation_run
        ).select_related('contributor_entity', 'evaluation_result')
        
        # Redact names if user lacks roster view permission
        for ass in assign_qs:
            name = "[REDACTED - VIEW SENSITIVE ROSTER REQUIRED]"
            if can_view_identity and can_view_assignments:
                name = ass.contributor_entity.display_name
            assignments.append({
                'id': ass.id,
                'entity_id': ass.contributor_entity.id,
                'contributor_name': name,
                'status': ass.assignment_status,
                'result_status': ass.evaluation_result.result_status
            })

    return render(request, 'chapters/detail.html', {
        'chapter': chapter,
        'rulesets': rulesets,
        'active_ruleset': active_ruleset,
        'active_rules': active_rules,
        'runs': runs,
        'override_count': override_count,
        'assignments': assignments,
        'can_view_identity': can_view_identity,
        'can_view_assignments': can_view_assignments
    })


@login_required
@permission_required('roster.manage_chapter_definitions', raise_exception=True)
@require_POST
def ruleset_create_draft(request, chapter_id):
    """
    HTTP POST handler to spawn a new draft ruleset.
    """
    draft = create_draft_ruleset(chapter_id, request.user.username)
    messages.success(request, f"Created draft ruleset version {draft.version}.")
    return redirect('ruleset_editor', ruleset_id=draft.id)


@login_required
@permission_required('roster.manage_chapter_definitions', raise_exception=True)
def ruleset_editor(request, ruleset_id):
    """
    Renders visual interface to edit draft rules.
    """
    ruleset = get_object_or_404(ChapterRuleSet, id=ruleset_id)
    if ruleset.status != 'DRAFT':
        messages.error(request, "Rulesets are immutable once activated/superseded.")
        return redirect('chapter_detail', chapter_id=ruleset.chapter_id)

    rules = ruleset.rules.filter(is_active=True).order_by('display_order', 'id')
    counties = County.objects.all().order_by('display_name')
    places = GeographicPlace.objects.all().order_by('canonical_name')
    postals = PostalArea.objects.all().order_by('postal_code')

    return render(request, 'chapters/ruleset_editor.html', {
        'ruleset': ruleset,
        'rules': rules,
        'counties': counties,
        'places': places,
        'postals': postals
    })


@login_required
@permission_required('roster.manage_chapter_definitions', raise_exception=True)
@require_POST
def rule_create(request, ruleset_id):
    """
    Adds a geographic rule to a ruleset.
    """
    effect = request.POST.get('effect')
    target_type = request.POST.get('target_type')
    target_id = request.POST.get('target_id')
    description = request.POST.get('description', '')
    display_order = int(request.POST.get('display_order', 10))

    try:
        add_rule_to_ruleset(
            ruleset_id=ruleset_id,
            effect=effect,
            target_type=target_type,
            target_id=target_id,
            description=description,
            display_order=display_order,
            actor=request.user.username
        )
        messages.success(request, "Successfully added geographic rule.")
    except Exception as e:
        messages.error(request, f"Failed to add rule: {str(e)}")

    return redirect('ruleset_editor', ruleset_id=ruleset_id)


@login_required
@permission_required('roster.manage_chapter_definitions', raise_exception=True)
@require_POST
def rule_deactivate(request, rule_id):
    """
    Deactivates a rule in a draft ruleset.
    """
    rule = get_object_or_404(ChapterRule, id=rule_id)
    ruleset_id = rule.rule_set_id
    try:
        deactivate_rule(rule_id, request.user.username)
        messages.success(request, "Successfully deactivated geographic rule.")
    except Exception as e:
        messages.error(request, f"Failed to deactivate rule: {str(e)}")

    return redirect('ruleset_editor', ruleset_id=ruleset_id)


@login_required
@permission_required('roster.activate_chapter_rules', raise_exception=True)
@require_POST
def ruleset_activate(request, ruleset_id):
    """
    Activates the ruleset and schedules a pending apply run.
    """
    try:
        run = activate_ruleset(ruleset_id, request.user.username)
        messages.success(request, f"Ruleset activated successfully. Staged pending apply run {run.id}.")
    except Exception as e:
        messages.error(request, f"Failed to activate ruleset: {str(e)}")
        return redirect('ruleset_editor', ruleset_id=ruleset_id)

    return redirect('chapter_detail', chapter_id=run.chapter_id)


@login_required
@permission_required('roster.preview_chapter_rules', raise_exception=True)
@require_POST
def preview_execute(request, ruleset_id):
    """
    Creates and runs a draft ruleset evaluation in PREVIEW mode.
    """
    ruleset = get_object_or_404(ChapterRuleSet, id=ruleset_id)
    
    # Fetch active geography dataset versions
    active_datasets = GeographyDataset.objects.filter(status='ACTIVE')
    snapshot = {ds.id: ds.version for ds in active_datasets}

    run = ChapterEvaluationRun.objects.create(
        chapter=ruleset.chapter,
        rule_set=ruleset,
        run_mode='PREVIEW',
        trigger_type='MANUAL_FULL_EVALUATION',
        geography_dataset_snapshot=snapshot,
        resolver_version=RESOLVER_VERSION,
        evaluation_engine_version=ENGINE_VERSION,
        membership_snapshot_date=timezone.now().date(),
        scope='all',
        actor=request.user.username,
        status='PENDING'
    )

    try:
        run_chapter_evaluation(run.id)
        messages.success(request, f"Preview run {run.id} completed successfully.")
        return redirect('preview_detail', run_id=run.id)
    except Exception as e:
        messages.error(request, f"Preview execution failed: {str(e)}")
        return redirect('ruleset_editor', ruleset_id=ruleset_id)


@login_required
@permission_required('roster.preview_chapter_rules', raise_exception=True)
def preview_detail(request, run_id):
    """
    Calculates and renders differences between preview results and current assignments.
    Redacts identities for actors lacking sensitive roster permission.
    """
    run = get_object_or_404(ChapterEvaluationRun, id=run_id, run_mode='PREVIEW')
    chapter = run.chapter
    
    can_view_identity = request.user.has_perm('roster.view_sensitive_roster')

    # Fetch staged results
    staged_results = ChapterEvaluationResult.objects.filter(evaluation_run=run)
    staged_map = {r.contributor_entity_id: r for r in staged_results}

    # Fetch current results for the chapter's active run
    current_results = {}
    if chapter.current_evaluation_run:
        curr_qs = ChapterEvaluationResult.objects.filter(evaluation_run=chapter.current_evaluation_run)
        current_results = {r.contributor_entity_id: r for r in curr_qs}

    # Compare differences
    new_inclusions = []
    removed_inclusions = []
    new_exclusions = []
    removed_exclusions = []
    new_ambiguity = []
    resolved_ambiguity = []
    new_unresolved = []
    resolved_unresolved = []
    decisive_rule_changes = []

    all_entity_ids = set(staged_map.keys()) | set(current_results.keys())
    entities = ContributorEntity.objects.filter(id__in=all_entity_ids)
    entities_map = {e.id: e for e in entities}

    for ent_id in all_entity_ids:
        st_res = staged_map.get(ent_id)
        cu_res = current_results.get(ent_id)
        ent = entities_map.get(ent_id)

        st_status = st_res.result_status if st_res else 'NO_MATCH'
        cu_status = cu_res.result_status if cu_res else 'NO_MATCH'

        name = "[REDACTED - VIEW SENSITIVE ROSTER REQUIRED]"
        if can_view_identity:
            name = ent.display_name if ent else 'Unknown'

        # Check inclusions
        st_inc = st_status in ['INCLUDED_BY_RULE', 'MANUALLY_INCLUDED', 'PROVISIONAL_GEOGRAPHIC_MATCH']
        cu_inc = cu_status in ['INCLUDED_BY_RULE', 'MANUALLY_INCLUDED', 'PROVISIONAL_GEOGRAPHIC_MATCH']

        if st_inc and not cu_inc:
            new_inclusions.append({'entity_id': ent_id, 'name': name, 'status': st_status})
        elif cu_inc and not st_inc:
            removed_inclusions.append({'entity_id': ent_id, 'name': name, 'status': cu_status})

        # Check exclusions
        st_exc = st_status in ['EXCLUDED_BY_RULE', 'MANUALLY_EXCLUDED']
        cu_exc = cu_status in ['EXCLUDED_BY_RULE', 'MANUALLY_EXCLUDED']

        if st_exc and not cu_exc:
            new_exclusions.append({'entity_id': ent_id, 'name': name, 'status': st_status})
        elif cu_exc and not st_exc:
            removed_exclusions.append({'entity_id': ent_id, 'name': name, 'status': cu_status})

        # Check ambiguity
        st_amb = st_status in ['AMBIGUOUS_GEOGRAPHY', 'AMBIGUOUS_LOCATION']
        cu_amb = cu_status in ['AMBIGUOUS_GEOGRAPHY', 'AMBIGUOUS_LOCATION']

        if st_amb and not cu_amb:
            new_ambiguity.append({'entity_id': ent_id, 'name': name, 'status': st_status})
        elif cu_amb and not st_amb:
            resolved_ambiguity.append({'entity_id': ent_id, 'name': name, 'status': cu_status})

        # Check unresolved
        st_unr = st_status in ['NO_CURRENT_LOCATION', 'NO_CURRENT_RESOLVED_LOCATION', 'NO_RULE_MATCH']
        cu_unr = cu_status in ['NO_CURRENT_LOCATION', 'NO_CURRENT_RESOLVED_LOCATION', 'NO_RULE_MATCH']

        if st_unr and not cu_unr:
            new_unresolved.append({'entity_id': ent_id, 'name': name, 'status': st_status})
        elif cu_unr and not st_unr:
            resolved_unresolved.append({'entity_id': ent_id, 'name': name, 'status': cu_status})

        # Check decisive rule changes
        if st_res and cu_res:
            st_rule = st_res.rule_matches.first()
            cu_rule = cu_res.rule_matches.first()
            st_rule_id = st_rule.rule_id if st_rule else None
            cu_rule_id = cu_rule.rule_id if cu_rule else None
            if st_rule_id != cu_rule_id:
                decisive_rule_changes.append({
                    'entity_id': ent_id, 'name': name,
                    'old_rule': f"Rule {cu_rule_id}" if cu_rule_id else "None",
                    'new_rule': f"Rule {st_rule_id}" if st_rule_id else "None"
                })

    context = {
        'run': run,
        'chapter': chapter,
        'can_view_identity': can_view_identity,
        'new_inclusions': new_inclusions,
        'removed_inclusions': removed_inclusions,
        'new_exclusions': new_exclusions,
        'removed_exclusions': removed_exclusions,
        'new_ambiguity': new_ambiguity,
        'resolved_ambiguity': resolved_ambiguity,
        'new_unresolved': new_unresolved,
        'resolved_unresolved': resolved_unresolved,
        'decisive_rule_changes': decisive_rule_changes
    }

    return render(request, 'chapters/preview_detail.html', context)


@login_required
@permission_required('roster.evaluate_chapter_rules', raise_exception=True)
@require_POST
def apply_execute(request, ruleset_id):
    """
    HTTP POST handler to execute a pending run and atomically promote it.
    """
    ruleset = get_object_or_404(ChapterRuleSet, id=ruleset_id)
    
    # Resolve pending run
    run = ChapterEvaluationRun.objects.filter(
        chapter=ruleset.chapter,
        rule_set=ruleset,
        run_mode='APPLY',
        status='PENDING'
    ).first()

    if not run:
        # Create run if not exists
        active_datasets = GeographyDataset.objects.filter(status='ACTIVE')
        snapshot = {ds.id: ds.version for ds in active_datasets}
        run = ChapterEvaluationRun.objects.create(
            chapter=ruleset.chapter,
            rule_set=ruleset,
            run_mode='APPLY',
            trigger_type='MANUAL_FULL_EVALUATION',
            geography_dataset_snapshot=snapshot,
            resolver_version=RESOLVER_VERSION,
            evaluation_engine_version=ENGINE_VERSION,
            membership_snapshot_date=timezone.now().date(),
            scope='all',
            actor=request.user.username,
            status='PENDING'
        )

    try:
        run_chapter_evaluation(run.id)
        messages.success(request, f"Evaluation run completed successfully. Cache generation promoted.")
    except Exception as e:
        messages.error(request, f"Evaluation execution failed: {str(e)}")

    return redirect('chapter_detail', chapter_id=ruleset.chapter_id)


@login_required
@permission_required('roster.view_chapter_definitions', raise_exception=True)
def run_detail(request, run_id):
    """
    Renders evaluation run execution stats.
    """
    run = get_object_or_404(ChapterEvaluationRun, id=run_id)
    return render(request, 'chapters/run_detail.html', {'run': run})


@login_required
@permission_required('roster.view_chapter_definitions', raise_exception=True)
def aggregate_overlaps(request):
    """
    Aggregates overlap matrix data across active chapters.
    """
    chapters = list(Chapter.objects.filter(status='ACTIVE'))
    active_runs = [c.current_evaluation_run_id for c in chapters if c.current_evaluation_run_id]

    if not active_runs or len(active_runs) < 2:
        return render(request, 'chapters/overlaps.html', {'overlap_data': [], 'total_overlaps': 0})

    # Count overlaps
    overlap_qs = ChapterAssignment.objects.filter(
        evaluation_run_id__in=active_runs,
        assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']
    ).values('contributor_entity_id').annotate(cnt=models.Count('id')).filter(cnt__gt=1)
    
    total_overlaps = overlap_qs.count()

    # Pairwise overlap calculation
    overlap_matrix = {}
    for i in range(len(chapters)):
        for j in range(i+1, len(chapters)):
            c1 = chapters[i]
            c2 = chapters[j]
            if c1.current_evaluation_run_id and c2.current_evaluation_run_id:
                # Find entities included in both
                e1 = set(ChapterAssignment.objects.filter(evaluation_run=c1.current_evaluation_run, assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']).values_list('contributor_entity_id', flat=True))
                e2 = set(ChapterAssignment.objects.filter(evaluation_run=c2.current_evaluation_run, assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']).values_list('contributor_entity_id', flat=True))
                common = e1 & e2
                if common:
                    overlap_matrix[(c1.name, c2.name)] = len(common)

    return render(request, 'chapters/overlaps.html', {
        'overlap_data': [{'chapter_1': k[0], 'chapter_2': k[1], 'count': v} for k, v in overlap_matrix.items()],
        'total_overlaps': total_overlaps
    })


@login_required
@permission_required('roster.view_chapter_assignments', raise_exception=True)
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def named_overlaps(request):
    """
    Renders detail list of contributor entities overlapping multiple chapters.
    Requires sensitive roster permissions.
    """
    chapters = list(Chapter.objects.filter(status='ACTIVE'))
    active_runs = [c.current_evaluation_run_id for c in chapters if c.current_evaluation_run_id]

    if not active_runs or len(active_runs) < 2:
        return render(request, 'chapters/named_overlaps.html', {'overlaps': []})

    overlap_qs = ChapterAssignment.objects.filter(
        evaluation_run_id__in=active_runs,
        assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']
    ).values('contributor_entity_id').annotate(cnt=models.Count('id')).filter(cnt__gt=1)

    overlapping_entity_ids = [item['contributor_entity_id'] for item in overlap_qs]
    entities = ContributorEntity.objects.filter(id__in=overlapping_entity_ids)

    payload = []
    for ent in entities:
        # Find which chapters it is in
        matched_chapters = ChapterAssignment.objects.filter(
            evaluation_run_id__in=active_runs,
            contributor_entity=ent,
            assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']
        ).select_related('chapter')
        
        payload.append({
            'entity': ent,
            'chapters': [mc.chapter.name for mc in matched_chapters]
        })

    return render(request, 'chapters/named_overlaps.html', {'overlaps': payload})


@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
@permission_required('roster.manage_chapter_overrides', raise_exception=True)
def override_search(request):
    """
    Renders overrides and entity search/assignment controls.
    """
    query = request.GET.get('query', '')
    entities = []
    if query:
        entities = ContributorEntity.objects.filter(
            models.Q(display_name__icontains=query) | models.Q(id__icontains=query)
        )[:15]

    overrides = ChapterEntityOverride.objects.all().select_related('chapter', 'contributor_entity').order_by('-created_at')
    chapters = Chapter.objects.all().order_by('name')

    return render(request, 'chapters/overrides.html', {
        'entities': entities,
        'overrides': overrides,
        'chapters': chapters,
        'query': query
    })


@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
@permission_required('roster.manage_chapter_overrides', raise_exception=True)
@require_POST
def override_create(request):
    """
    POST handler to create manual entity override.
    """
    chapter_id = request.POST.get('chapter_id')
    entity_id = request.POST.get('entity_id')
    override_type = request.POST.get('override_type')
    reason = request.POST.get('reason')
    effective_date = request.POST.get('effective_date')
    expiration_date = request.POST.get('expiration_date') or None

    try:
        create_override(
            chapter_id=chapter_id,
            entity_id=entity_id,
            override_type=override_type,
            reason=reason,
            effective_date=effective_date,
            expiration_date=expiration_date,
            actor=request.user.username
        )
        messages.success(request, "Successfully created manual override.")
    except Exception as e:
        messages.error(request, f"Failed to create override: {str(e)}")

    return redirect('override_search')


@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
@permission_required('roster.manage_chapter_overrides', raise_exception=True)
@require_POST
def override_expiration(request, override_id):
    """
    Forces override expiration.
    """
    try:
        expire_override(override_id, request.user.username)
        messages.success(request, "Successfully expired manual override.")
    except Exception as e:
        messages.error(request, f"Failed to expire override: {str(e)}")

    return redirect('override_search')


@login_required
@permission_required('roster.view_sensitive_roster', raise_exception=True)
@permission_required('roster.manage_chapter_overrides', raise_exception=True)
@require_POST
def override_revocation(request, override_id):
    """
    Revokes manual override.
    """
    try:
        revoke_override(override_id, request.user.username)
        messages.success(request, "Successfully revoked manual override.")
    except Exception as e:
        messages.error(request, f"Failed to revoke override: {str(e)}")

    return redirect('override_search')


@login_required
@permission_required('roster.view_chapter_assignments', raise_exception=True)
@permission_required('roster.view_sensitive_roster', raise_exception=True)
def assignment_detail(request, assignment_id):
    """
    Renders detailed rule matches for a specific assignment.
    """
    assignment = get_object_or_404(ChapterAssignment, id=assignment_id)
    res = assignment.evaluation_result
    matches = res.rule_matches.all().select_related('rule')

    return render(request, 'chapters/assignment_detail.html', {
        'assignment': assignment,
        'result': res,
        'matches': matches
    })
