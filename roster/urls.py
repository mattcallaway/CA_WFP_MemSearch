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
]
