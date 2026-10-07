from functools import wraps

from django.shortcuts import redirect
from django.urls import reverse

STORE_GROUP = "Store Manager"


def is_store_manager(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and (user.is_superuser or user.groups.filter(name=STORE_GROUP).exists())
    )


def store_manager_required(view):
    """Only users in the 'Store Manager' group (or superusers) can open the view."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_store_manager(request.user):
            return redirect(f"{reverse('store:login')}?next={request.path}")
        return view(request, *args, **kwargs)

    return wrapped
