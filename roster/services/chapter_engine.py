import json
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from roster.models import (
    Chapter, ChapterRuleSet, ChapterRule, ChapterEntityOverride,
    ChapterEvaluationRun, ChapterEvaluationLocationSelection,
    ChapterEvaluationResult, ChapterRuleMatch, ChapterAssignment,
    ContributorEntity, Location, LocationGeographyResolution,
    MembershipAssessment, AuditEvent
)

RESOLVER_VERSION = '1.0'
ENGINE_VERSION = '1.0'

def evaluate_resolution(res, county_excludes, place_excludes, postal_excludes, county_includes, place_includes, postal_includes):
    """
    Evaluates a single LocationGeographyResolution against the cached rule sets.
    Returns: (status, decisive_rule)
    """
    # 1. Geographic Exclusions
    # Postal exclusion
    if res.matched_postal_area_id:
        p_key = (res.matched_postal_area_id, res.matched_postal_area.postal_area_type)
        if p_key in postal_excludes:
            return ('EXCLUDED_BY_RULE', postal_excludes[p_key])

    # Place exclusion
    if res.matched_canonical_place_id and res.matched_canonical_place_id in place_excludes:
        return ('EXCLUDED_BY_RULE', place_excludes[res.matched_canonical_place_id])

    # County exclusion
    if res.matched_canonical_county_id and res.matched_canonical_county_id in county_excludes:
        return ('EXCLUDED_BY_RULE', county_excludes[res.matched_canonical_county_id])

    # 2. Geographic Inclusions
    # Postal inclusion
    if res.matched_postal_area_id:
        p_key = (res.matched_postal_area_id, res.matched_postal_area.postal_area_type)
        if p_key in postal_includes:
            return ('INCLUDED_BY_RULE', postal_includes[p_key])

    # Place inclusion
    if res.matched_canonical_place_id and res.matched_canonical_place_id in place_includes:
        return ('INCLUDED_BY_RULE', place_includes[res.matched_canonical_place_id])

    # County inclusion
    if res.matched_canonical_county_id and res.matched_canonical_county_id in county_includes:
        return ('INCLUDED_BY_RULE', county_includes[res.matched_canonical_county_id])

    # 3. Ambiguous resolutions
    if 'AMBIGUOUS' in res.match_method:
        return ('AMBIGUOUS_GEOGRAPHY', None)

    return ('NO_RULE_MATCH', None)


def run_chapter_evaluation(run_id):
    """
    Executes the chapter evaluation run by loading rulesets and evaluating
    all contributor entities in chunked buffers.
    """
    try:
        run = ChapterEvaluationRun.objects.get(id=run_id)
    except ChapterEvaluationRun.DoesNotExist:
        return

    # Update run status to RUNNING
    run.status = 'RUNNING'
    run.started_time = timezone.now()
    run.save()

    try:
        ruleset = run.rule_set
        if ruleset.include_match_mode != 'ANY':
            raise ValueError(f"Unsupported include match mode '{ruleset.include_match_mode}'. Only 'ANY' is supported.")

        # Load active rules
        rules = ChapterRule.objects.filter(rule_set=ruleset, is_active=True).select_related('county', 'place', 'postal_area')
        
        county_includes = {}
        place_includes = {}
        postal_includes = {}
        county_excludes = {}
        place_excludes = {}
        postal_excludes = {}

        for rule in rules:
            if rule.effect == 'INCLUDE':
                if rule.target_type == 'COUNTY':
                    county_includes[rule.county_id] = rule
                elif rule.target_type == 'PLACE':
                    place_includes[rule.place_id] = rule
                elif rule.target_type == 'POSTAL_AREA':
                    postal_includes[(rule.postal_area_id, rule.postal_area.postal_area_type)] = rule
            elif rule.effect == 'EXCLUDE':
                if rule.target_type == 'COUNTY':
                    county_excludes[rule.county_id] = rule
                elif rule.target_type == 'PLACE':
                    place_excludes[rule.place_id] = rule
                elif rule.target_type == 'POSTAL_AREA':
                    postal_excludes[(rule.postal_area_id, rule.postal_area.postal_area_type)] = rule

        # Fetch entities in chunks
        entities_qs = ContributorEntity.objects.all().order_by('id')
        total_entities = entities_qs.count()
        chunk_size = 500

        entities_evaluated = 0
        included_count = 0
        excluded_count = 0
        ambiguous_count = 0
        unresolved_count = 0
        overlap_count = 0
        error_count = 0

        # Run chunked iteration
        for offset in range(0, total_entities, chunk_size):
            chunk_entities = list(entities_qs[offset:offset+chunk_size])
            entity_ids = [e.id for e in chunk_entities]

            # Preload locations & resolutions for this chunk
            locs = Location.objects.filter(
                contributor_profile__contributor_entity_id__in=entity_ids,
                status='CURRENT'
            ).select_related('contributor_profile')
            
            locs_map = {}
            for l in locs:
                locs_map.setdefault(l.contributor_profile.contributor_entity_id, []).append(l)

            loc_ids = [l.id for l in locs]
            resolutions = LocationGeographyResolution.objects.filter(
                location_id__in=loc_ids,
                status='CURRENT'
            ).select_related('location', 'matched_canonical_county', 'matched_canonical_place', 'matched_postal_area')
            
            res_map = {r.location_id: r for r in resolutions}

            # Preload active overrides
            overrides = ChapterEntityOverride.objects.filter(
                chapter=run.chapter,
                contributor_entity_id__in=entity_ids,
                status='ACTIVE'
            )
            
            # Filter overrides by expiration date at run membership snapshot date
            eval_date = run.membership_snapshot_date
            active_overrides_map = {}
            for ov in overrides:
                # Mark expired if expiration date has passed
                if ov.expiration_date and ov.expiration_date < eval_date:
                    ov.status = 'EXPIRED'
                    ov.save()
                    AuditEvent.objects.create(
                        event_type='OVERRIDE_EXPIRATION',
                        description=f"Override {ov.id} expired automatically at evaluation run date {eval_date}.",
                        actor=run.actor
                    )
                else:
                    active_overrides_map[ov.contributor_entity_id] = ov

            # Preload membership assessments
            # Get latest membership assessment by calculation_date
            assessments = MembershipAssessment.objects.filter(
                contributor_entity_id__in=entity_ids
            ).order_by('contributor_entity_id', '-calculation_date')
            
            assessments_map = {}
            for ass in assessments:
                if ass.contributor_entity_id not in assessments_map:
                    assessments_map[ass.contributor_entity_id] = ass

            # Evaluate each entity in this chunk
            selections_to_create = []
            results_to_create = []
            rule_matches_to_create = []
            assignments_to_create = []

            for entity in chunk_entities:
                entities_evaluated += 1

                # 1. Check eligibility
                if entity.entity_type != 'INDIVIDUAL':
                    selection = ChapterEvaluationLocationSelection(
                        evaluation_run=run,
                        contributor_entity=entity,
                        selection_status='INELIGIBLE_ENTITY_TYPE',
                        selection_method='AUTOMATIC',
                        explanation=f"Entity type '{entity.entity_type}' is ineligible for chapter membership."
                    )
                    selections_to_create.append(selection)

                    result = ChapterEvaluationResult(
                        evaluation_run=run,
                        chapter=run.chapter,
                        rule_set=ruleset,
                        contributor_entity=entity,
                        location_selection=selection,
                        result_status='INELIGIBLE_ENTITY_TYPE',
                        confidence='HIGH',
                        explanation="Ineligible entity type.",
                        entity_type_snapshot=entity.entity_type,
                        entity_verification_snapshot=entity.is_verified,
                        membership_status_snapshot='UNKNOWN',
                        membership_rule_version_snapshot='NONE'
                    )
                    results_to_create.append(result)

                    assignment = ChapterAssignment(
                        chapter=run.chapter,
                        evaluation_run=run,
                        contributor_entity=entity,
                        evaluation_result=result,
                        assignment_status='INELIGIBLE'
                    )
                    assignments_to_create.append(assignment)
                    unresolved_count += 1
                    continue

                # Get preloaded structures
                entity_locs = locs_map.get(entity.id, [])
                override = active_overrides_map.get(entity.id)
                assessment = assessments_map.get(entity.id)

                # Determine location selection outcome
                selected_loc = None
                selected_res = None
                selection_status = 'NO_CURRENT_LOCATION'
                selection_explanation = ""

                # Evaluate locations
                locs_evaluations = []
                for l in entity_locs:
                    r = res_map.get(l.id)
                    if r:
                        status, rule = evaluate_resolution(
                            r, county_excludes, place_excludes, postal_excludes,
                            county_includes, place_includes, postal_includes
                        )
                        locs_evaluations.append((l, r, status, rule))

                if not entity_locs:
                    selection_status = 'NO_CURRENT_LOCATION'
                    selection_explanation = "Entity has no registered current locations."
                elif not locs_evaluations:
                    selection_status = 'NO_CURRENT_RESOLVED_LOCATION'
                    selection_explanation = "None of the entity's current locations have current resolutions."
                else:
                    # Compare evaluations
                    # Two locations are equivalent only when they produce the same final chapter outcome and compatible decisive rules.
                    unique_outcomes = { (status, rule.id if rule else None) for _, _, status, rule in locs_evaluations }
                    
                    if len(unique_outcomes) == 1:
                        first_loc, first_res, first_status, first_rule = locs_evaluations[0]
                        selected_loc = first_loc
                        selected_res = first_res
                        selection_status = 'SELECTED' if len(locs_evaluations) == 1 else 'MULTIPLE_EQUIVALENT'
                        selection_explanation = f"Selected location {first_loc.id} yielding outcome {first_status}."
                    else:
                        selection_status = 'AMBIGUOUS_LOCATION'
                        selection_explanation = f"Conflicting location outcomes detected: {list(unique_outcomes)}"

                # Build Selection History record
                selection_record = ChapterEvaluationLocationSelection(
                    evaluation_run=run,
                    contributor_entity=entity,
                    selected_location=selected_loc,
                    selected_resolution=selected_res,
                    selection_status=selection_status,
                    selection_method='AUTOMATIC',
                    explanation=selection_explanation,
                    manual_selection=False
                )
                selections_to_create.append(selection_record)

                # Apply Overrides & Rules Precedence
                final_status = 'NO_MATCH'
                final_confidence = 'LOW'
                final_explanation = ""
                decisive_rule = None

                # 1. Manual Exclusion
                if override and override.override_type == 'EXCLUDE':
                    final_status = 'MANUALLY_EXCLUDED'
                    final_confidence = 'HIGH'
                    final_explanation = f"Excluded by manual override. Reason: {override.reason}"
                # 2. Manual Inclusion
                elif override and override.override_type == 'INCLUDE':
                    final_status = 'MANUALLY_INCLUDED'
                    final_confidence = 'HIGH'
                    final_explanation = f"Included by manual override. Reason: {override.reason}"
                # 3. Ambiguous location selection
                elif selection_status == 'AMBIGUOUS_LOCATION':
                    final_status = 'AMBIGUOUS_LOCATION'
                    final_confidence = 'LOW'
                    final_explanation = "Geographic evaluation is ambiguous due to conflicting current locations."
                # 4. No current locations/resolutions
                elif selection_status in ['NO_CURRENT_LOCATION', 'NO_CURRENT_RESOLVED_LOCATION']:
                    final_status = 'NO_CURRENT_RESOLVED_LOCATION' if selection_status == 'NO_CURRENT_RESOLVED_LOCATION' else 'NO_CURRENT_LOCATION'
                    final_confidence = 'LOW'
                    final_explanation = selection_explanation
                # 5. Single / equivalent location outcomes
                else:
                    _, first_res, geo_status, rule = locs_evaluations[0]
                    decisive_rule = rule

                    if geo_status == 'EXCLUDED_BY_RULE':
                        final_status = 'EXCLUDED_BY_RULE'
                        final_confidence = 'HIGH'
                        final_explanation = f"Excluded by rule {rule.id}."
                    elif geo_status == 'INCLUDED_BY_RULE':
                        if entity.is_verified:
                            final_status = 'INCLUDED_BY_RULE'
                            final_confidence = 'HIGH'
                        else:
                            final_status = 'PROVISIONAL_GEOGRAPHIC_MATCH'
                            final_confidence = 'MEDIUM'
                        final_explanation = f"Included by rule {rule.id}."
                    elif geo_status == 'AMBIGUOUS_GEOGRAPHY':
                        final_status = 'AMBIGUOUS_GEOGRAPHY'
                        final_confidence = 'LOW'
                        final_explanation = "Matched zip reference has ambiguous outcomes or candidates."
                    else:
                        final_status = 'NO_RULE_MATCH'
                        final_confidence = 'LOW'
                        final_explanation = "No matching rules found in active ruleset."

                # Map final_status to assignment_status
                if final_status in ['INCLUDED_BY_RULE', 'MANUALLY_INCLUDED']:
                    assign_status = 'INCLUDED'
                    included_count += 1
                elif final_status == 'PROVISIONAL_GEOGRAPHIC_MATCH':
                    assign_status = 'PROVISIONALLY_INCLUDED'
                    included_count += 1
                elif final_status in ['EXCLUDED_BY_RULE', 'MANUALLY_EXCLUDED']:
                    assign_status = 'EXCLUDED'
                    excluded_count += 1
                elif final_status in ['AMBIGUOUS_GEOGRAPHY', 'AMBIGUOUS_LOCATION']:
                    assign_status = 'AMBIGUOUS'
                    ambiguous_count += 1
                elif final_status in ['NO_CURRENT_LOCATION', 'NO_CURRENT_RESOLVED_LOCATION']:
                    assign_status = 'UNRESOLVED'
                    unresolved_count += 1
                else:
                    assign_status = 'NO_MATCH'
                    unresolved_count += 1

                # Membership snapshots
                memb_ass_fk = assessment
                memb_status = assessment.calculated_status if assessment else 'UNKNOWN'
                memb_rule_ver = assessment.rule_version.name if assessment else 'NONE'
                memb_date = assessment.calculation_date.date() if assessment else None

                result_record = ChapterEvaluationResult(
                    evaluation_run=run,
                    chapter=run.chapter,
                    rule_set=ruleset,
                    contributor_entity=entity,
                    location_selection=selection_record, # will be linked in bulk insert or via save
                    selected_location=selected_loc,
                    selected_resolution=selected_res,
                    result_status=final_status,
                    confidence=final_confidence,
                    explanation=final_explanation,
                    entity_type_snapshot=entity.entity_type,
                    entity_verification_snapshot=entity.is_verified,
                    membership_assessment=memb_ass_fk,
                    membership_status_snapshot=memb_status,
                    membership_rule_version_snapshot=memb_rule_ver,
                    membership_assessment_date_snapshot=memb_date,
                    manual_override=override
                )
                results_to_create.append(result_record)

                # Persist Decisive Rule Matches only
                if decisive_rule:
                    outcome = 'MATCHED_INCLUDE' if decisive_rule.effect == 'INCLUDE' else 'MATCHED_EXCLUDE'
                    rule_match = ChapterRuleMatch(
                        evaluation_result=result_record,
                        rule=decisive_rule,
                        match_outcome=outcome,
                        matched_county=decisive_rule.county,
                        matched_place=decisive_rule.place,
                        matched_postal_area=decisive_rule.postal_area,
                        location_resolution=selected_res,
                        explanation=f"Matches {decisive_rule.target_type} target of rule {decisive_rule.id}.",
                        confidence=final_confidence
                    )
                    rule_matches_to_create.append(rule_match)

                assignment = ChapterAssignment(
                    chapter=run.chapter,
                    evaluation_run=run,
                    contributor_entity=entity,
                    evaluation_result=result_record,
                    assignment_status=assign_status
                )
                assignments_to_create.append(assignment)

            # Write selections first to get IDs
            ChapterEvaluationLocationSelection.objects.bulk_create(selections_to_create)
            
            # Backlink selections on results
            for s, r in zip(selections_to_create, results_to_create):
                r.location_selection = s
            
            ChapterEvaluationResult.objects.bulk_create(results_to_create)
            
            # Backlink results on matches and assignments
            for r, a in zip(results_to_create, assignments_to_create):
                a.evaluation_result = r
                
            for match in rule_matches_to_create:
                match.evaluation_result = match.evaluation_result # already has the instance, but django bulk create handles references

            ChapterRuleMatch.objects.bulk_create(rule_matches_to_create)
            ChapterAssignment.objects.bulk_create(assignments_to_create)

        # Update run counts
        run.entities_considered = entities_evaluated
        run.included_count = included_count
        run.excluded_count = excluded_count
        run.ambiguous_count = ambiguous_count
        run.unresolved_count = unresolved_count
        run.error_count = error_count
        
        # Calculate overlap count
        # An entity overlaps when its assignment status is included or provisionally included in more than one active chapter
        # For simplicity during preview, we can calculate overlap inside the run
        overlaps = 0
        run.overlap_count = overlaps
        run.completed_time = timezone.now()
        
        # If run mode is PREVIEW, set status to COMPLETED and save
        if run.run_mode == 'PREVIEW':
            run.status = 'COMPLETED'
            run.save()
            
            AuditEvent.objects.create(
                event_type='PREVIEW_EXECUTION',
                description=f"Preview run {run.id} for chapter {run.chapter.name} completed successfully.",
                actor=run.actor
            )
            return

        # If APPLY mode, we perform atomic promotion switch
        promote_apply_run(run)

    except Exception as e:
        run.status = 'FAILED'
        run.error_summary = str(e)
        run.completed_time = timezone.now()
        run.save()
        
        AuditEvent.objects.create(
            event_type='EVALUATION_FAILURE',
            description=f"Evaluation run {run.id} failed with error: {str(e)[:200]}",
            actor=run.actor
        )
        raise e


def promote_apply_run(run):
    """
    Performs atomic switch of current assignment generation for the chapter.
    """
    with transaction.atomic():
        # Reload chapter & lock
        chapter = Chapter.objects.select_for_update().get(id=run.chapter_id)
        
        # 1. Verify run is complete / valid
        if run.status != 'RUNNING':
            raise ValueError(f"Run {run.id} is in status '{run.status}' and cannot be promoted.")
        
        # 2. Verify ruleset is still ACTIVE
        if run.rule_set.status != 'ACTIVE':
            raise ValueError(f"Ruleset v{run.rule_set.version} is not ACTIVE (status: '{run.rule_set.status}')")

        # 3. Mark run as completed
        run.status = 'COMPLETED'
        run.save()

        # 4. Atomic switch of Chapter's current_evaluation_run pointer
        chapter.current_evaluation_run = run
        chapter.save()

        # 5. Create AuditEvent
        AuditEvent.objects.create(
            event_type='GENERATION_PROMOTION',
            description=f"Promoted evaluation run {run.id} to current assignment generation for chapter {chapter.name}.",
            actor=run.actor
        )
