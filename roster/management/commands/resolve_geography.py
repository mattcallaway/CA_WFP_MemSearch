from django.core.management.base import BaseCommand
from roster.services.geo_resolver import resolve_geographic_locations

class Command(BaseCommand):
    help = 'Triggers a synchronous geographic resolution run for current locations'

    def add_arguments(self, parser):
        parser.add_argument('--actor', type=str, default='SYSTEM_CLI', help='The actor executing the resolution run')
        parser.add_argument('--location-ids', nargs='+', type=int, help='Optional specific location IDs to resolve')

    def handle(self, *args, **options):
        actor = options['actor']
        location_ids = options['location_ids']

        self.stdout.write(f"Starting geographic resolution run. Actor: {actor}...")
        try:
            run = resolve_geographic_locations(actor=actor, trigger_type='MANUAL_BULK_RESOLUTION', location_ids=location_ids)
            self.stdout.write(self.style.SUCCESS(
                f"Successfully completed resolution run {run.id}.\n"
                f"Considered: {run.locations_considered}\n"
                f"Resolved: {run.resolved_count}\n"
                f"Ambiguous: {run.ambiguous_count}\n"
                f"Conflicting: {run.conflict_count}\n"
                f"Unmatched: {run.unmatched_count}"
            ))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Resolution run failed: {str(e)}"))
