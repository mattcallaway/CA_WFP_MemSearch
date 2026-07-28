from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from roster.models import ContributorEntity

class Command(BaseCommand):
    help = 'Sets up the three core user roles (Read-only, Data manager, Administrator) with correct permissions'

    def handle(self, *args, **options):
        # 1. Define the 10 custom permissions to manage
        managed_codenames = [
            'view_sensitive_roster',
            'import_contributions',
            'override_duplicate_file',
            'rollback_import',
            'restore_import',
            'manage_identity',
            'override_membership',
            'view_audit',
            'export_sensitive_data',
            'purge_data',
            'view_geography_reference',
            'import_geography_reference',
            'manage_geography_reference',
            'rollback_geography_import',
            'resolve_geography_ambiguity',
            'view_chapter_definitions',
            'manage_chapter_definitions',
            'preview_chapter_rules',
            'activate_chapter_rules',
            'evaluate_chapter_rules',
            'manage_chapter_overrides',
            'view_chapter_assignments'
        ]

        entity_ct = ContentType.objects.get_for_model(ContributorEntity)
        
        # Resolve permission objects
        perm_map = {}
        for codename in managed_codenames:
            try:
                perm = Permission.objects.get(content_type=entity_ct, codename=codename)
                perm_map[codename] = perm
            except Permission.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"Permission '{codename}' not found in DB."))

        # 2. Define group permission assignments
        group_specs = {
            'Read-only': [
                'view_sensitive_roster',
                'view_audit',
                'view_geography_reference',
                'view_chapter_definitions'
            ],
            'Data manager': [
                'view_sensitive_roster',
                'view_audit',
                'import_contributions',
                'override_duplicate_file',
                'rollback_import',
                'restore_import',
                'manage_identity',
                'override_membership',
                'view_geography_reference',
                'import_geography_reference',
                'manage_geography_reference',
                'rollback_geography_import',
                'resolve_geography_ambiguity',
                'view_chapter_definitions',
                'manage_chapter_definitions',
                'preview_chapter_rules',
                'activate_chapter_rules',
                'evaluate_chapter_rules',
                'manage_chapter_overrides'
            ],
            'Administrator': managed_codenames
        }

        for group_name, target_codenames in group_specs.items():
            group, created = Group.objects.get_or_create(name=group_name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created group '{group_name}'"))
            else:
                self.stdout.write(f"Group '{group_name}' already exists. Syncing permissions...")

            # Get current permissions of this group
            current_perms = set(group.permissions.all())

            # Identify permissions to keep (unrelated permissions)
            unrelated_perms = set()
            for perm in current_perms:
                # If it's not one of our managed permissions for ContributorEntity, preserve it
                if perm.content_type == entity_ct and perm.codename in managed_codenames:
                    continue
                unrelated_perms.add(perm)

            # Build target permission set (unrelated + target managed permissions)
            target_perms = set(unrelated_perms)
            for codename in target_codenames:
                if codename in perm_map:
                    target_perms.add(perm_map[codename])

            # Apply permission changes
            group.permissions.set(list(target_perms))
            self.stdout.write(self.style.SUCCESS(
                f"Configured group '{group_name}': assigned {len(target_codenames)} managed permissions, "
                f"preserved {len(unrelated_perms)} unrelated permissions."
            ))
