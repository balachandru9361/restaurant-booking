from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils.http import url_has_allowed_host_and_scheme

from store.permissions import home_url_for, is_store_staff
from .forms import SignupForm, LoginForm


def _safe_next(request):
    """Return the ?next= URL if it points to this site, otherwise None."""
    next_url = request.POST.get('next') or request.GET.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        return next_url
    return None


def _is_staff_user(user):
    return user.is_superuser or user.is_staff or is_store_staff(user)


def signup_view(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request) or 'home')

    if request.method == 'POST':
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(
                request, f'Welcome, {user.first_name}! Your account is ready.')
            # Continue where the customer left off (e.g. cart / confirm order)
            return redirect(_safe_next(request) or 'home')
    else:
        form = SignupForm()

    return render(request, 'accounts/signup.html', {
        'form': form,
        'next': _safe_next(request) or '',
    })


def login_view(request):
    next_url = _safe_next(request)

    if request.user.is_authenticated:
        if _is_staff_user(request.user):
            return redirect(home_url_for(request.user))
        return redirect(next_url or 'home')

    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                messages.success(
                    request, f'Welcome back, {user.first_name or user.username}!')

                # Staff / admin / kitchen / server -> their own dashboard
                if _is_staff_user(user):
                    return redirect(home_url_for(user))

                # Customers -> continue to where they were going
                # (cart / confirm order), otherwise home page
                return redirect(next_url or 'home')
            else:
                messages.error(
                    request, 'Invalid phone number or password.')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {
        'form': form,
        'next': next_url or '',
    })


def logout_view(request):
    logout(request)
    messages.success(request, 'Logged out successfully.')
    return redirect('home')


@login_required
def profile_view(request):
    orders_count = request.user.orders.count()
    bookings_count = request.user.table_bookings.count()

    context = {
        'orders_count': orders_count,
        'bookings_count': bookings_count,
    }
    return render(request, 'accounts/profile.html', context)