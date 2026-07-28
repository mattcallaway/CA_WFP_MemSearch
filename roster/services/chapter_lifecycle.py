from django.db import transaction
from django.utils import timezone
from django.db.models import Max
from roster.models import (
    Chapter, ChapterRuleSet, ChapterRule, ChapterEvaluationRun,
    County, GeographicPlace, PostalArea, GeographyDataset, AuditEvent
)
from roster.services.chapter_engine import RESOLVER_VERSION, ENGINE_VERSION

def create_draft_ruleset(chapter_id, actor):
    """
    Creates a new draft ruleset for a chapter, copying rules from the active ruleset if one exists.
    """
    chapter = Chapter.objects.get(id=chapter_id)
    
    # Calculate next version
    max_ver = ChapterRuleSet.objects.filter(chapter=chapter).aggregate(Max('version'))['version__max']
    next_ver = (max_ver or 0) + 1

    with transaction.atomic():
        draft = ChapterRuleSet.objects.create(
            chapter=chapter,
            version=next_ver,
            status='DRAFT',
            description=f"Draft Ruleset v{next_ver}",
            include_match_mode='ANY',
            created_by=actor
        )

        # Copy rules from currently active ruleset
        active_rs = ChapterRuleSet.objects.filter(chapter=chapter, status='ACTIVE').first()
        if active_rs:
            active_rules = ChapterRule.objects.filter(rule_set=active_rs, is_active=True)
            rules_to_create = []
            for r in active_rules:
                rules_to_create.append(ChapterRule(
                    rule_set=draft,
                    effect=r.effect,
                    target_type=r.target_type,
                    county=r.county,
                    place=r.place,
                    postal_area=r.postal_area,
                    description=r.description,
                    display_order=r.display_order,
                    is_active=True
                ))
            if rules_to_create:
                ChapterRule.objects.bulk_create(rules_to_create)

        AuditEvent.objects.create(
            event_type='DRAFT_CREATION',
            description=f"Created draft ruleset version {next_ver} for chapter {chapter.name}.",
            actor=actor
        )
        return draft


def add_rule_to_ruleset(ruleset_id, effect, target_type, target_id, description, display_order, actor):
    """
    Adds a geographic rule to a draft ruleset.
    """
    ruleset = ChapterRuleSet.objects.get(id=ruleset_id)
    if ruleset.status != 'DRAFT':
        raise ValueError("Rules can only be added to a DRAFT ruleset.")

    county = None
    place = None
    postal_area = None

    if target_type == 'COUNTY':
        county = County.objects.get(id=target_id)
    elif target_type == 'PLACE':
        place = GeographicPlace.objects.get(id=target_id)
    elif target_type == 'POSTAL_AREA':
        postal_area = PostalArea.objects.get(id=target_id)
    else:
        raise ValueError(f"Invalid target type '{target_type}'")

    rule = ChapterRule.objects.create(
        rule_set=ruleset,
        effect=effect,
        target_type=target_type,
        county=county,
        place=place,
        postal_area=postal_area,
        description=description,
        display_order=display_order,
        is_active=True
    )

    AuditEvent.objects.create(
        event_type='RULE_CREATION',
        description=f"Added rule {rule.id} ({effect} {target_type}) to draft ruleset v{ruleset.version} for chapter {ruleset.chapter.name}.",
        actor=actor
    )
    return rule


def deactivate_rule(rule_id, actor):
    """
    Deactivates a rule in a draft ruleset.
    """
    rule = ChapterRule.objects.get(id=rule_id)
    ruleset = rule.rule_set
    if ruleset.status != 'DRAFT':
        raise ValueError("Rules can only be deactivated in a DRAFT ruleset.")

    rule.is_active = False
    rule.save()

    AuditEvent.objects.create(
        event_type='RULE_DEACTIVATION',
        description=f"Deactivated rule {rule.id} in draft ruleset v{ruleset.version} for chapter {ruleset.chapter.name}.",
        actor=actor
    )


def activate_ruleset(ruleset_id, actor):
    """
    Activates a draft ruleset, superseding any currently active ruleset
    and creating a pending APPLY run.
    """
    ruleset = ChapterRuleSet.objects.get(id=ruleset_id)
    if ruleset.status not in ['DRAFT', 'VALIDATING']:
        raise ValueError("Only DRAFT rulesets can be activated.")

    if ruleset.include_match_mode != 'ANY':
        raise ValueError("Only 'ANY' include match mode is supported for activation.")

    with transaction.atomic():
        # Supersede currently active ruleset for chapter
        active_rs = ChapterRuleSet.objects.select_for_update().filter(chapter=ruleset.chapter, status='ACTIVE').first()
        if active_rs:
            active_rs.status = 'SUPERSEDED'
            active_rs.superseded_at = timezone.now()
            active_rs.save()
            
            # Supersede any pending evaluation runs for the old active ruleset
            ChapterEvaluationRun.objects.filter(chapter=ruleset.chapter, rule_set=active_rs, status='PENDING').update(status='SUPERSEDED')

        # Activate ruleset
        ruleset.status = 'ACTIVE'
        ruleset.activated_by = actor
        ruleset.activated_at = timezone.now()
        ruleset.save()

        # Build geography dataset snapshot
        active_datasets = GeographyDataset.objects.filter(status='ACTIVE')
        snapshot = {ds.id: ds.version for ds in active_datasets}

        # Create pending APPLY run
        run = ChapterEvaluationRun.objects.create(
            chapter=ruleset.chapter,
            rule_set=ruleset,
            run_mode='APPLY',
            trigger_type='RULE_SET_ACTIVATION',
            geography_dataset_snapshot=snapshot,
            resolver_version=RESOLVER_VERSION,
            evaluation_engine_version=ENGINE_VERSION,
            membership_snapshot_date=timezone.now().date(),
            scope='all',
            actor=actor,
            status='PENDING'
        )

        AuditEvent.objects.create(
            event_type='RULE_SET_ACTIVATION',
            description=f"Activated ruleset v{ruleset.version} for chapter {ruleset.chapter.name} and staged pending run {run.id}.",
            actor=actor
        )
        return run
