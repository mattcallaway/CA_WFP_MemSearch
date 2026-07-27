from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from roster.models import (
    ImportBatch, RawContribution, Contribution, ContributorEntity,
    Person, Organization, ContributionCluster, ContributionClusterAssignment,
    Location, MembershipAssessment, ProfilePatternAssessment
)

class Command(BaseCommand):
    help = 'Sets up the three core user roles (Read-only, Data manager, Administrator) with correct permissions'

    def handle(self, *args, **options):
        # 1. Define groups
        groups = {
            'Read-only': {
                'models': [
                    ImportBatch, RawContribution, Contribution, ContributorEntity,
                    Person, Organization, ContributionCluster, ContributionClusterAssignment,
                    Location, MembershipAssessment, ProfilePatternAssessment
                ],
                'actions': ['view']
            },
            'Data manager': {
                'models': [
                    ImportBatch, RawContribution, Contribution, ContributorEntity,
                    Person, Organization, ContributionCluster, ContributionClusterAssignment,
                    Location, MembershipAssessment, ProfilePatternAssessment
                ],
                # Data managers can view everything, and add/change operational data (but NOT delete)
                'actions': ['view', 'add', 'change']
            },
            'Administrator': {
                'models': [
                    ImportBatch, RawContribution, Contribution, ContributorEntity,
                    Person, Organization, ContributionCluster, ContributionClusterAssignment,
                    Location, MembershipAssessment, ProfilePatternAssessment
                ],
                # Administrators have full view, add, change, delete privileges
                'actions': ['view', 'add', 'change', 'delete']
            }
        }

        for group_name, spec in groups.items():
            group, created = Group.objects.get_or_create(name=group_name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created group '{group_name}'"))
            else:
                self.stdout.write(f"Group '{group_name}' already exists. Syncing permissions...")

            permissions_to_add = []
            for model in spec['models']:
                content_type = ContentType.objects.get_for_model(model)
                for action in spec['actions']:
                    codename = f"{action}_{model._meta.model_name}"
                    try:
                        perm = Permission.objects.get(content_type=content_type, codename=codename)
                        permissions_to_add.append(perm)
                    except Permission.DoesNotExist:
                        self.stdout.write(self.style.WARNING(f"Permission '{codename}' not found for content type '{content_type}'"))

            # Also assign the custom permission on ContributorEntity for managing clusters to Data manager and Administrator
            if group_name in ['Data manager', 'Administrator']:
                entity_ct = ContentType.objects.get_for_model(ContributorEntity)
                try:
                    manage_clusters_perm = Permission.objects.get(content_type=entity_ct, codename='can_manage_clusters')
                    permissions_to_add.append(manage_clusters_perm)
                except Permission.DoesNotExist:
                    pass

            group.permissions.set(permissions_to_add)
            self.stdout.write(self.style.SUCCESS(f"Successfully configured permissions for group '{group_name}' ({len(permissions_to_add)} permissions assigned)."))
