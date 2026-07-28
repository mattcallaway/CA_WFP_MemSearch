from django.urls import path
from roster import views

urlpatterns = [
    path('', views.dashboard_redirect, name='index'),
    path('dashboard/', views.dashboard, name='dashboard'),
    
    # Imports
    path('imports/', views.imports_list, name='imports_list'),
    path('imports/upload/', views.imports_upload, name='imports_upload'),
    path('imports/preview/<int:batch_id>/', views.imports_preview, name='imports_preview'),
    path('imports/process/<int:batch_id>/', views.imports_process, name='imports_process'),
    path('imports/rollback/<int:batch_id>/', views.imports_rollback, name='imports_rollback'),
    path('imports/restore/<int:batch_id>/', views.imports_restore, name='imports_restore'),
    path('imports/failures/<int:batch_id>/', views.imports_failures, name='imports_failures'),
    
    # People
    path('people/', views.people_list, name='people_list'),
    path('people/profile/<int:entity_id>/', views.person_profile, name='person_profile'),
    path('people/override/<int:entity_id>/', views.membership_override, name='membership_override'),
    path('people/merge/', views.merge_profiles, name='merge_profiles'),
    path('people/split/<int:merge_decision_id>/', views.split_profiles, name='split_profiles'),
    path('people/export/', views.export_roster, name='export_roster'),
    path('people/correct-type/<int:entity_id>/', views.correct_entity_type, name='correct_entity_type'),
    
    # Audit
    path('audit/', views.audit_history, name='audit_history'),
    
    # Geography Stage 2A
    path('geography/datasets/', views.geography_datasets_list, name='geography_datasets_list'),
    path('geography/datasets/<int:dataset_id>/', views.geography_dataset_detail, name='geography_dataset_detail'),
    path('geography/datasets/<int:dataset_id>/activate/', views.geography_dataset_activate, name='geography_dataset_activate'),
    path('geography/import/', views.geography_import_upload, name='geography_import_upload'),
    path('geography/import/execute/', views.geography_import_execute, name='geography_import_execute'),
    path('geography/batch/rollback/<int:batch_id>/', views.geography_batch_rollback, name='geography_batch_rollback'),
    path('geography/batch/restore/<int:batch_id>/', views.geography_batch_restore, name='geography_batch_restore'),
    
    # Directories
    path('geography/counties/', views.county_directory, name='county_directory'),
    path('geography/places/', views.place_directory, name='place_directory'),
    path('geography/postal-areas/', views.postal_area_directory, name='postal_area_directory'),
    path('geography/aliases/', views.geography_alias_directory, name='geography_alias_directory'),
    
    # Resolution Ambiguity & Queue
    path('geography/ambiguity-queue/', views.geography_ambiguity_queue, name='geography_ambiguity_queue'),
    path('geography/resolve/<int:res_id>/', views.geography_manual_resolve, name='geography_manual_resolve'),
    path('geography/run/<int:run_id>/', views.geography_resolution_run_detail, name='geography_resolution_run_detail'),
    path('geography/run/execute/<int:run_id>/', views.geography_run_execute, name='geography_run_execute'),

    # Chapters & Rule Engine (Stage 2B)
    path('chapters/', views.chapter_list, name='chapter_list'),
    path('chapters/<int:chapter_id>/', views.chapter_detail, name='chapter_detail'),
    path('chapters/<int:chapter_id>/create-draft/', views.ruleset_create_draft, name='ruleset_create_draft'),
    path('rulesets/<int:ruleset_id>/editor/', views.ruleset_editor, name='ruleset_editor'),
    path('rulesets/<int:ruleset_id>/add-rule/', views.rule_create, name='rule_create'),
    path('rulesets/<int:ruleset_id>/activate/', views.ruleset_activate, name='ruleset_activate'),
    path('rulesets/<int:ruleset_id>/preview/', views.preview_execute, name='preview_execute'),
    path('rulesets/<int:ruleset_id>/apply/', views.apply_execute, name='apply_execute'),
    path('rules/deactivate/<int:rule_id>/', views.rule_deactivate, name='rule_deactivate'),
    path('runs/<int:run_id>/', views.run_detail, name='run_detail'),
    path('runs/<int:run_id>/preview/', views.preview_detail, name='preview_detail'),
    
    # Overlaps & Overrides
    path('chapters/overlaps/', views.aggregate_overlaps, name='aggregate_overlaps'),
    path('chapters/overlaps/named/', views.named_overlaps, name='named_overlaps'),
    path('chapters/overrides/', views.override_search, name='override_search'),
    path('chapters/overrides/create/', views.override_create, name='override_create'),
    path('chapters/overrides/<int:override_id>/expire/', views.override_expiration, name='override_expiration'),
    path('chapters/overrides/<int:override_id>/revoke/', views.override_revocation, name='override_revocation'),
    path('assignments/<int:assignment_id>/', views.assignment_detail, name='assignment_detail'),
]
