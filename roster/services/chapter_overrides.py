from django.db import transaction
from django.utils import timezone
from roster.models import Chapter, ContributorEntity, ChapterEntityOverride, AuditEvent

def create_override(chapter_id, entity_id, override_type, reason, effective_date, expiration_date, actor):
    """
    Creates a manual chapter override for a contributor entity, superseding any existing active override.
    """
    if not reason or not reason.strip():
        raise ValueError("A nonblank reason is required to create a manual override.")

    if expiration_date and expiration_date < effective_date:
        raise ValueError("Expiration date cannot be before the effective date.")

    chapter = Chapter.objects.get(id=chapter_id)
    entity = ContributorEntity.objects.get(id=entity_id)

    with transaction.atomic():
        # Look for existing active override
        existing = ChapterEntityOverride.objects.select_for_update().filter(
            chapter=chapter,
            contributor_entity=entity,
            status='ACTIVE'
        ).first()

        new_override = ChapterEntityOverride(
            chapter=chapter,
            contributor_entity=entity,
            override_type=override_type,
            reason=reason.strip(),
            effective_date=effective_date,
            expiration_date=expiration_date,
            status='ACTIVE',
            created_by=actor
        )

        if existing:
            # Change status first to release active unique constraint
            existing.status = 'SUPERSEDED'
            existing.save()
            
            # Save new override to get ID
            new_override.save()
            
            # Link them
            existing.superseded_by = new_override
            existing.save()
            
            AuditEvent.objects.create(
                event_type='OVERRIDE_SUPERSEDED',
                description=f"Superseded override {existing.id} with new override {new_override.id} for Entity {entity.id} on Chapter {chapter.name}.",
                actor=actor
            )
        else:
            new_override.save()

        AuditEvent.objects.create(
            event_type='OVERRIDE_CREATION',
            description=f"Created active override {new_override.id} ({override_type}) for Entity {entity.id} on Chapter {chapter.name}.",
            actor=actor
        )
        return new_override


def revoke_override(override_id, actor):
    """
    Revokes an active override.
    """
    with transaction.atomic():
        override = ChapterEntityOverride.objects.select_for_update().get(id=override_id)
        if override.status != 'ACTIVE':
            raise ValueError(f"Override is in status '{override.status}' and cannot be revoked.")

        override.status = 'REVOKED'
        override.save()

        AuditEvent.objects.create(
            event_type='OVERRIDE_REVOCATION',
            description=f"Revoked override {override.id} for Entity {override.contributor_entity_id} on Chapter {override.chapter.name}.",
            actor=actor
        )
        return override


def expire_override(override_id, actor):
    """
    Forces expiration of an active override.
    """
    with transaction.atomic():
        override = ChapterEntityOverride.objects.select_for_update().get(id=override_id)
        if override.status != 'ACTIVE':
            raise ValueError(f"Override is in status '{override.status}' and cannot be expired.")

        override.status = 'EXPIRED'
        override.save()

        AuditEvent.objects.create(
            event_type='OVERRIDE_EXPIRATION',
            description=f"Expired override {override.id} for Entity {override.contributor_entity_id} on Chapter {override.chapter.name}.",
            actor=actor
        )
        return override
