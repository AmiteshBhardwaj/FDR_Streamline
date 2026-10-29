from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from .models import AuditEvent

@receiver(user_logged_in)
def record_login(sender,request,user,**kwargs):
    AuditEvent.objects.create(actor=user,action='authentication.signed_in',details={})

@receiver(user_logged_out)
def record_logout(sender,request,user,**kwargs):
    if user and user.pk:
        AuditEvent.objects.create(actor=user,action='authentication.signed_out',details={})
