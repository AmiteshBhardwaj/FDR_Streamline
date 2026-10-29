"""Deny-by-default role and institution checks shared by views and domain services."""
from django.core.exceptions import PermissionDenied

from .models import Institution, Instrument, Profile, Role


def profile_for(user):
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied("An active portal account is required.")
    try:
        return user.profile
    except Profile.DoesNotExist as exc:
        raise PermissionDenied("This account has no portal role.") from exc


def scoped_institutions(user):
    profile = profile_for(user)
    if profile.role == Role.ADMIN:
        return Institution.objects.none()
    return profile.institutions.all()


def scoped_instruments(user):
    return Instrument.objects.filter(institution__in=scoped_institutions(user))


def require_role(user, *roles):
    profile = profile_for(user)
    if profile.role not in roles:
        raise PermissionDenied("Your role cannot perform this action.")
    return profile


def can_access(user, instrument):
    try:
        return scoped_institutions(user).filter(pk=instrument.institution_id).exists()
    except PermissionDenied:
        return False
