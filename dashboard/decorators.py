from django.contrib.auth.decorators import user_passes_test


def _has_role(user, role):
    """True if user is that staff role, or is a superuser (admin can see everything)."""
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    return bool(profile and profile.role == role)


def admin_required(view_func):
    """Full admin only — superusers. Chefs/Servers (is_staff but not superuser) are blocked."""
    return user_passes_test(lambda u: u.is_active and u.is_superuser, login_url='accounts:login')(view_func)


def kitchen_access_required(view_func):
    """Admin or chef — anyone with is_staff (superuser included) can access.
    Used for shared actions like updating order status."""
    return user_passes_test(lambda u: u.is_active and u.is_staff, login_url='accounts:login')(view_func)


def kitchen_required(view_func):
    """Kitchen dashboard: admins and kitchen-role staff only (servers are blocked)."""
    return user_passes_test(lambda u: u.is_active and _has_role(u, 'kitchen'), login_url='accounts:login')(view_func)


def server_required(view_func):
    """Server dashboard: admins and server-role staff only (kitchen staff are blocked)."""
    return user_passes_test(lambda u: u.is_active and _has_role(u, 'server'), login_url='accounts:login')(view_func)