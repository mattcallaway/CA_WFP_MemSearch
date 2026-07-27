from django.db import transaction
from roster.models import GeographyDataset, AuditEvent, GeographyResolutionRun

def activate_geography_dataset(dataset_id, actor, priority=100, run_resolution_auto=False):
    """
    Activates a geography dataset, superseding previous datasets of the same type,
    records activation in an audit event, and creates a pending resolution proposal.
    If run_resolution_auto=True, it runs resolution synchronously (not default).
    """
    with transaction.atomic():
        try:
            dataset = GeographyDataset.objects.get(id=dataset_id)
        except GeographyDataset.DoesNotExist:
            raise ValueError(f"Dataset with ID {dataset_id} not found")

        # Find previously active datasets of the same type
        prev_active = list(GeographyDataset.objects.filter(
            dataset_type=dataset.dataset_type, status='ACTIVE'
        ).exclude(id=dataset.id))

        # Update previous ones to SUPERSEDED
        for d in prev_active:
            d.status = 'SUPERSEDED'
            d.save()
            AuditEvent.objects.create(
                event_type='GEOGRAPHY_DATASET_SUPERSEDED',
                description=f"Geography dataset '{d.name}' (v{d.version}) superseded by activation of '{dataset.name}'.",
                actor=actor
            )
            # Supersede any pending runs for this superseded dataset
            GeographyResolutionRun.objects.filter(dataset=d, status='PENDING').update(status='SUPERSEDED')

        # Activate the new dataset
        dataset.status = 'ACTIVE'
        dataset.resolver_priority = priority
        dataset.save()

        # Log activation audit event
        AuditEvent.objects.create(
            event_type='GEOGRAPHY_DATASET_ACTIVATION',
            description=f"Activated geography dataset '{dataset.name}' (v{dataset.version}) with resolver priority {priority}.",
            actor=actor
        )

        # Prevent multiple active pending proposals for the same activation and scope
        scope = f"dataset_{dataset.id}"
        run_proposal, created = GeographyResolutionRun.objects.get_or_create(
            dataset=dataset,
            scope=scope,
            status='PENDING',
            defaults={
                'trigger_type': 'DATASET_ACTIVATION',
                'resolver_version': '1.0',
                'actor': actor
            }
        )

    if run_resolution_auto:
        # Run it synchronously outside activation transaction
        from roster.services.geo_resolver import execute_pending_resolution_run
        run_proposal = execute_pending_resolution_run(run_proposal.id, actor)

    return run_proposal
