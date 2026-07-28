from django.test import TestCase, Client
from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.urls import reverse
from roster.models import ContributorEntity, ImportBatch

class RBACTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # Build ContentType for ContributorEntity
        self.entity_ct = ContentType.objects.get_for_model(ContributorEntity)
        
        # Create standard permissions
        call_command('setup_roles')
        
        # Retreive standard groups
        self.readonly_group = Group.objects.get(name='Read-only')
        self.datamanager_group = Group.objects.get(name='Data manager')
        self.admin_group = Group.objects.get(name='Administrator')
        
        # Create users
        self.readonly_user = User.objects.create_user(username='reviewer', password='password')
        self.readonly_user.groups.add(self.readonly_group)
        
        self.datamanager_user = User.objects.create_user(username='manager', password='password')
        self.datamanager_user.groups.add(self.datamanager_group)
        
        self.admin_user = User.objects.create_user(username='admin', password='password')
        self.admin_user.groups.add(self.admin_group)
        
        self.superuser = User.objects.create_superuser(username='super', password='password')

    def test_setup_roles_idempotency(self):
        # Verify initial assignment counts
        initial_readonly = self.readonly_group.permissions.count()
        initial_manager = self.datamanager_group.permissions.count()
        initial_admin = self.admin_group.permissions.count()
        
        self.assertEqual(initial_readonly, 4)
        self.assertEqual(initial_manager, 19)
        self.assertEqual(initial_admin, 22)
        
        # Add unrelated manually assigned permission to Read-only group
        unrelated_perm = Permission.objects.filter(content_type=ContentType.objects.get_for_model(User)).first()
        self.readonly_group.permissions.add(unrelated_perm)
        
        # Run setup_roles command second time to test idempotency
        call_command('setup_roles')
        
        self.readonly_group.refresh_from_db()
        self.datamanager_group.refresh_from_db()
        self.admin_group.refresh_from_db()
        
        # Verify managed counts remain identical
        managed_readonly = self.readonly_group.permissions.filter(content_type=self.entity_ct).count()
        self.assertEqual(managed_readonly, 4)
        self.assertEqual(self.datamanager_group.permissions.filter(content_type=self.entity_ct).count(), 19)
        self.assertEqual(self.admin_group.permissions.filter(content_type=self.entity_ct).count(), 22)
        
        # Verify unrelated manually added permission was preserved
        self.assertTrue(self.readonly_group.permissions.filter(id=unrelated_perm.id).exists())

    def test_anonymous_redirect_get(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    def test_anonymous_redirect_post(self):
        response = self.client.post(reverse('merge_profiles'), {})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse('login')))

    def test_readonly_denied_import(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('imports_upload'), {'csv_file': ''})
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_rollback(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('imports_rollback', args=[1]))
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_restore(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('imports_restore', args=[1]))
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_merge(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('merge_profiles'), {})
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_split(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('split_profiles', args=[1]))
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_entity_correction(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('correct_entity_type', args=[1]), {})
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_membership_override(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.post(reverse('membership_override', args=[1]), {})
        self.assertEqual(response.status_code, 403)

    def test_readonly_denied_export(self):
        self.client.login(username='reviewer', password='password')
        response = self.client.get(reverse('export_roster'))
        self.assertEqual(response.status_code, 403)

    def test_datamanager_allowed_import_dashboard(self):
        self.client.login(username='manager', password='password')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_datamanager_denied_export_by_default(self):
        self.client.login(username='manager', password='password')
        response = self.client.get(reverse('export_roster'))
        self.assertEqual(response.status_code, 403)

    def test_datamanager_denied_purge(self):
        # Data manager cannot execute purge CLI command
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:
            call_command('purge_batch', batch_id=1, actor="manager", confirm=True, production_confirm=True)
        self.assertIn("not a superuser", str(ctx.exception))

    def test_admin_allowed_export(self):
        self.client.login(username='admin', password='password')
        # Create an individual contributor for export
        ContributorEntity.objects.create(entity_type='INDIVIDUAL', display_name='TEST EXPORT')
        response = self.client.get(reverse('export_roster'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')

    def test_state_changing_get_rejected(self):
        self.client.login(username='manager', password='password')
        # A state changing view like split should return HTTP 405 (method not allowed) when requested via GET
        response = self.client.get(reverse('split_profiles', args=[1]))
        self.assertEqual(response.status_code, 405)
