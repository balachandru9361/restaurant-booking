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
            next_url, allowed_hosts={request.get_host()}):
        return next_url
    return None


def signup_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(
                request, f'Welcome, {user.first_name}! Your account is ready.')
            # New customer always lands on the home page (View Menu is one tap from there)
            return redirect('home')
    else:
        form = SignupForm()

    return render(request, 'accounts/signup.html', {
        'form': form,
        'next': request.GET.get('next', ''),
    })


def login_view(request):
    if request.user.is_authenticated:
        return redirect(home_url_for(request.user))

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

                # Every role goes to its OWN dashboard (next param is ignored):
                #   superuser    -> dashboard:admin_home
                #   store staff  -> store dashboard
                #   server role  -> dashboard:server_dashboard
                #   other staff  -> dashboard:kitchen_dashboard
                if user.is_superuser or user.is_staff or is_store_staff(user):
                    return redirect(home_url_for(user))

                # Normal customers always land on the home page (the ?next= link is ignored,
                # so a customer is never thrown straight into the menu or another page)
                return redirect('home')
            else:
                messages.error(
                    request, 'Invalid phone number or password.')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {
        'form': form,
        'next': request.GET.get('next', ''),
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