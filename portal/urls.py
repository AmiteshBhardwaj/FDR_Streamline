from django.urls import path
from . import views

app_name='portal'
urlpatterns=[
    path('',views.landing,name='home'),
    path('dashboard/',views.dashboard,name='dashboard'),
    path('instruments/',views.instrument_list,name='instruments'),
    path('instruments/new/',views.instrument_new,name='instrument_new'),
    path('instruments/<uuid:pk>/',views.instrument_detail,name='instrument_detail'),
    path('instruments/<uuid:pk>/edit/',views.instrument_edit,name='instrument_edit'),
    path('instruments/<uuid:pk>/submit/',views.instrument_submit,name='instrument_submit'),
    path('instruments/<uuid:pk>/action/',views.instrument_action,name='instrument_action'),
    path('instruments/<uuid:pk>/documents/',views.document_upload,name='document_upload'),
    path('documents/<uuid:pk>/',views.document_download,name='document_download'),
    path('instruments/<uuid:pk>/verification/',views.verification,name='verification'),
    path('instruments/<uuid:pk>/renew/',views.instrument_new,name='renew'),
    path('instruments/<uuid:pk>/release/',views.release_new,name='release_new'),
    path('releases/<uuid:pk>/',views.release_detail,name='release_detail'),
    path('releases/<uuid:pk>/action/',views.release_action,name='release_action'),
    path('notifications/',views.notifications,name='notifications'),
    path('notifications/<int:pk>/read/',views.notification_read,name='notification_read'),
    path('reports/',views.reports,name='reports'),path('reports/export/',views.export,name='export'),
    path('import/',views.import_records,name='import_records'),
    path('administration/',views.administration,name='administration'),path('audit/',views.audit_log,name='audit'),
]
