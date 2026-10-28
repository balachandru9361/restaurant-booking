from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

STORE_GROUP = "Store Manager"
STORE_ROLE = "store"


def _role(user):
    """Role saved in the user's profile ('server', 'store', ...) or None."""
    profile = getattr(user, "profile", None)
    return getattr(profile, "role", None) if profile else None


def is_store_staff(user):
    """A real store person: in the 'Store Manager' group OR has profile role 'store'.
    Superusers are NOT counted here (admin is view-only on store pages)."""
    return bool(
        user.is_authenticated
        and user.is_active
        and not user.is_superuser
        and (user.groups.filter(name=STORE_GROUP).exists() or _role(user) == STORE_ROLE)
    )


def is_store_manager(user):
    """Who may open Store pages: store staff, or a superuser (view-only)."""
    return bool(
        user.is_authenticated
        and user.is_active
        and (user.is_superuser or is_store_staff(user))
    )


def _url(name, fallback="/"):
    try:
        return reverse(name)
    except NoReverseMatch:
        return fallback


def home_url_for(user):
    """The one place that decides which dashboard each role lands on after login."""
    if not user.is_authenticated:
        return _url("home")
    if user.is_superuser:
        return _url("dashboard:admin_home")
    if is_store_staff(user):
        return _url("store:dashboard")
    if user.is_staff:
        if _role(user) == "server":
            return _url("dashboard:server_dashboard")
        return _url("dashboard:kitchen_dashboard")
    return _url("home")


def store_manager_required(view):
    """Only store staff (or a superuser, view-only) can open the view."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_store_manager(request.user):
            return redirect(f"{reverse('store:login')}?next={request.path}")
        # Admin (superuser) can only look, not change.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not is_store_staff(request.user):
            messages.error(request, "Admin has view-only access to Store pages.")
            return redirect("store:dashboard")
        return view(request, *args, **kwargs)

    return wrapped