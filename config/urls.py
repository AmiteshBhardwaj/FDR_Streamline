from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy
from portal.auth import PortalLoginView
from portal.views import health

urlpatterns = [
    path('login/', PortalLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password/', auth_views.PasswordChangeView.as_view(template_name='portal/generic_form.html', success_url=reverse_lazy('portal:dashboard'), extra_context={'title':'Change password','submit_label':'Update password'}), name='password_change'),
    path('health/', health, name='health'),
    path('admin/', admin.site.urls),
    path('', include('portal.urls')),
]
