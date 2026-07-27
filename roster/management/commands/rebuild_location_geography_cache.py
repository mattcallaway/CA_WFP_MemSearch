from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.db import transaction
from roster.models import Location, LocationGeographyResolution, AuditEvent

class Command(BaseCommand):
    help = 'Rebuilds Location geography cache fields from current resolutions'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Preview changes without committing')
        parser.add_argument('--actor', type=str, required=True, help='The actor username executing the rebuild')
        parser.add_argument('--location-ids', nargs='+', type=int, help='Limit rebuild to specific location IDs')
        parser.add_argument('--run-id', type=int, help='Limit rebuild to locations resolved in a specific run')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        actor_username = options['actor']
        location_ids = options['location_ids']
        run_id = options['run_id']

        # Validate actor exists, is active, and is authorized
        try:
            actor_user = User.objects.get(username=actor_username)
        except User.DoesNotExist:
            raise CommandError(f"Actor username '{actor_username}' does not exist.")

        if not actor_user.is_active:
            raise CommandError(f"Actor username '{actor_username}' is inactive.")

        if not (actor_user.is_superuser or actor_user.has_perm('roster.manage_geography_reference')):
            raise CommandError(f"Actor username '{actor_username}' does not have 'roster.manage_geography_reference' permission.")

        self.stdout.write(f"Starting cache rebuild. Actor: {actor_username}, Dry Run: {dry_run}")

        # Filter target locations
        locations_qs = Location.objects.all()
        if location_ids:
            locations_qs = locations_qs.filter(id__in=location_ids)
        if run_id:
            resolved_loc_ids = LocationGeographyResolution.objects.filter(resolution_run_id=run_id).values_list('location_id', flat=True)
            locations_qs = locations_qs.filter(id__in=resolved_loc_ids)

        locations = list(locations_qs)
        total_locations = len(locations)
        self.stdout.write(f"Locations matching criteria: {total_locations}")

        before_updated = 0
        cleared_count = 0
        success_count = 0
        corrupt_count = 0

        chunk_size = 500
        for i in range(0, total_locations, chunk_size):
            chunk = locations[i:i + chunk_size]
            
            with transaction.atomic():
                locations_to_update = []
                
                # Fetch resolutions for this chunk
                resolutions = LocationGeographyResolution.objects.filter(
                    location__in=chunk, status='CURRENT'
                ).select_related('matched_canonical_county', 'matched_canonical_place', 'matched_postal_area')
                
                # Group by location_id
                res_map = {}
                for res in resolutions:
                    res_map.setdefault(res.location_id, []).append(res)

                for loc in chunk:
                    loc_res_list = res_map.get(loc.id, [])
                    
                    if len(loc_res_list) > 1:
                        # Corruption detected! Multiple current resolutions!
                        corrupt_count += 1
                        self.stderr.write(
                            f"CORRUPTION: Location {loc.id} ({loc.city or 'No City'}) has {len(loc_res_list)} current resolutions. Skipping cache rebuild for this location."
                        )
                        continue
                    
                    if len(loc_res_list) == 1:
                        res = loc_res_list[0]
                        # Track if cache values actually change
                        changed = (
                            loc.matched_place != res.matched_canonical_place or
                            loc.matched_postal_area != res.matched_postal_area or
                            loc.matched_county != res.matched_canonical_county or
                            loc.match_method != res.match_method or
                            loc.geo_confidence != res.confidence or
                            loc.geo_explanation != res.explanation
                        )
                        
                        if changed:
                            loc.matched_place = res.matched_canonical_place
                            loc.matched_postal_area = res.matched_postal_area
                            loc.matched_county = res.matched_canonical_county
                            loc.match_method = res.match_method
                            loc.geo_confidence = res.confidence
                            loc.geo_ambiguity_status = 'RESOLVED' if res.match_method in ['EXACT_PLACE_ZIP_MATCH', 'EXACT_ALIAS_ZIP_MATCH', 'UNIQUE_ZIP_INFERENCE', 'MANUALLY_RESOLVED'] else 'UNRESOLVED'
                            loc.geo_explanation = res.explanation
                            
                            locations_to_update.append(loc)
                            success_count += 1
                    else:
                        # No current resolution exists: clear cache fields
                        changed = (
                            loc.matched_place is not None or
                            loc.matched_postal_area is not None or
                            loc.matched_county is not None or
                            loc.match_method != 'UNRESOLVED'
                        )
                        if changed:
                            loc.matched_place = None
                            loc.matched_postal_area = None
                            loc.matched_county = None
                            loc.match_method = 'UNRESOLVED'
                            loc.geo_confidence = 'LOW'
                            loc.geo_ambiguity_status = 'UNRESOLVED'
                            loc.geo_explanation = "Cache cleared: no current resolution found."
                            
                            locations_to_update.append(loc)
                            cleared_count += 1

                if not dry_run and locations_to_update:
                    Location.objects.bulk_update(locations_to_update, fields=[
                        'matched_place', 'matched_postal_area', 'matched_county',
                        'match_method', 'geo_confidence', 'geo_ambiguity_status', 'geo_explanation'
                    ])
                elif dry_run:
                    pass

        self.stdout.write(
            f"Rebuild completed. Locations Updated: {success_count}, Cleared: {cleared_count}, Corrupted skipped: {corrupt_count}."
        )

        if not dry_run:
            AuditEvent.objects.create(
                event_type='GEOGRAPHY_CACHE_REBUILD',
                description=f"Rebuilt location geography cache. Updated: {success_count}, Cleared: {cleared_count}, Corrupted: {corrupt_count}.",
                actor=actor_username
            )
