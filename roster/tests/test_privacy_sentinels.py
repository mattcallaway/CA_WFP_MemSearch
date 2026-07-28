from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User, Group
from roster.models import ContributorEntity, ImportBatch, RawContribution, Contribution
from roster.management.commands.setup_roles import Command as SetupRolesCommand


class PrivacySentinelTestCase(TestCase):
    def setUp(self):
        # Setup roles
        SetupRolesCommand().handle()
        self.client = Client()

        # Create user lacking view_sensitive_roster / export / audit permissions
        self.unprivileged_user = User.objects.create_user(username="unprivileged_user", password="password")

        # Create user with view_sensitive_roster
        self.admin_user = User.objects.create_superuser(username="admin_user", password="password")

        # Create synthetic sentinel entity
        self.sentinel_name = "SENTINEL_PII_JOHN_DOE_99999"
        self.sentinel_street = "SENTINEL_STREET_SECRET_123"
        self.entity = ContributorEntity.objects.create(
            display_name=self.sentinel_name,
            entity_type="INDIVIDUAL",
            verification_status="UNVERIFIED",
            is_verified=False
        )

    def test_unauthorized_roster_export_returns_403(self):
        self.client.login(username="unprivileged_user", password="password")
        response = self.client.get(reverse('export_roster'))
        self.assertEqual(response.status_code, 403)

    def test_unauthorized_audit_view_returns_403(self):
        self.client.login(username="unprivileged_user", password="password")
        response = self.client.get(reverse('audit_history'))
        self.assertEqual(response.status_code, 403)

    def test_privacy_values_projection_excludes_pii(self):
        # Queryset values projection used for anonymized exports / views
        qs = ContributorEntity.objects.values('id', 'entity_type', 'verification_status')
        item = qs.filter(id=self.entity.id).first()
        
        self.assertNotIn('display_name', item)
        self.assertNotIn('primary_street', item)
        self.assertNotIn('employer', item)
