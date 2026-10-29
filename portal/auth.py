import hashlib
from datetime import timedelta
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import LoginAttempt

class PortalLoginView(LoginView):
    template_name = 'registration/login.html'
    redirect_authenticated_user = True

    def keys(self):
        # Deliberately ignore untrusted forwarding headers.
        values = [('ip:' + self.request.META.get('REMOTE_ADDR',''), 25), ('user:' + self.request.POST.get('username','').strip().casefold(), 5)]
        return [(hashlib.sha256(value.encode()).hexdigest(), limit) for value, limit in values]

    def post(self, request, *args, **kwargs):
        now = timezone.now()
        if LoginAttempt.objects.filter(key__in=[key for key,_ in self.keys()],blocked_until__gt=now).exists():
            form = self.get_form_class()(request=request,initial={'username':request.POST.get('username','')})
            form.cleaned_data = {}
            form.add_error(None,'Too many sign-in attempts. Please wait 15 minutes and try again.')
            return self.render_to_response(self.get_context_data(form=form),status=429)
        return super().post(request,*args,**kwargs)

    def form_invalid(self, form):
        now = timezone.now()
        with transaction.atomic():
            for key,limit in self.keys():
                attempt,_ = LoginAttempt.objects.get_or_create(key=key,defaults={'count':0,'window_start':now})
                if attempt.window_start < now - timedelta(minutes=15):
                    LoginAttempt.objects.filter(pk=attempt.pk).update(count=0,window_start=now,blocked_until=None)
                LoginAttempt.objects.filter(pk=attempt.pk).update(count=F('count')+1)
                LoginAttempt.objects.filter(pk=attempt.pk,count__gte=limit).update(blocked_until=now+timedelta(minutes=15))
        return super().form_invalid(form)

    def form_valid(self, form):
        # Clear account failures only; retain the per-address spray limit.
        LoginAttempt.objects.filter(key=self.keys()[1][0]).delete()
        return super().form_valid(form)
