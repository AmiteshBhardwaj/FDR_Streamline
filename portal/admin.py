from django.contrib import admin
from .models import (Institution, Profile, Policy, Instrument, Document, Submission,
    Verification, ReleaseRequest, AuditEvent, Observation, Notification, WorkerHeartbeat)

class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False
    def has_delete_permission(self,request,obj=None): return False
    def has_view_permission(self,request,obj=None): return request.user.is_superuser

for model in [Institution,Profile,Policy,Instrument,Document,Submission,Verification,ReleaseRequest,AuditEvent,Observation,Notification,WorkerHeartbeat]:
    admin.site.register(model,ReadOnlyAdmin)
admin.site.site_header='FDR Portal · Technical administration'
admin.site.site_title='FDR administration'
admin.site.index_title='Read-only records · use portal workflows for changes'
