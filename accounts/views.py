from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import SignupForm, LoginForm


def signup_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Account created successfully!')
            return redirect('home')
    else:
        form = SignupForm()

    return render(request, 'accounts/signup.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                messages.success(request, f'Welcome back, {user.username}!')

                # Superuser -> full Admin Dashboard
                if user.is_superuser:
                    return redirect('dashboard:admin_home')

                # Staff (non-superuser) -> role decides where they land
                if user.is_staff:
                    profile = getattr(user, 'profile', None)
                    if profile and profile.role == 'server':
                        return redirect('dashboard:server_dashboard')
                    return redirect('dashboard:kitchen_dashboard')

                # Normal users -> next param irundha adhukku, illana home
                next_url = request.GET.get('next', 'home')
                return redirect(next_url)
            else:
                messages.error(request, 'Invalid username or password.')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form})


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