from django.conf import settings

def portal_context(request):
    if not request.user.is_authenticated:
        return {'demo_mode':settings.DEMO_MODE}
    from .models import Profile, Notification
    from .access import scoped_instruments
    from django.db.models import Q
    profile = Profile.objects.filter(user=request.user).first()
    unread = Notification.objects.filter(user=request.user,read_at__isnull=True)
    unread = unread.filter(Q(instrument__isnull=True)|Q(instrument__in=scoped_instruments(request.user))) if profile else unread.none()
    return {'user_role':profile.role if profile else '', 'unread_count':unread.count(),'demo_mode':settings.DEMO_MODE}
