import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Profile

User = get_user_model()

PHONE_RE = re.compile(r'^[6-9]\d{9}$')


def normalize_phone(raw):
    """Return a clean 10-digit Indian mobile number, or None if it is not one."""
    digits = re.sub(r'\D', '', raw or '')
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    return digits if PHONE_RE.match(digits) else None


class SignupForm(forms.Form):
    """Customer sign up: name, phone number and password only."""

    full_name = forms.CharField(
        max_length=60,
        label='Your name',
        widget=forms.TextInput(attrs={
            'placeholder': 'Your name',
            'autocomplete': 'name',
        }),
    )
    phone = forms.CharField(
        max_length=20,
        label='Phone number',
        widget=forms.TextInput(attrs={
            'placeholder': '10-digit mobile number',
            'type': 'tel',
            'inputmode': 'numeric',
            'autocomplete': 'tel',
        }),
    )
    password = forms.CharField(
        min_length=6,
        label='Password',
        widget=forms.PasswordInput(attrs={
            'placeholder': 'At least 6 characters',
            'autocomplete': 'new-password',
        }),
    )

    def clean_full_name(self):
        return self.cleaned_data['full_name'].strip()

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data['phone'])
        if phone is None:
            raise forms.ValidationError('Enter a valid 10-digit mobile number.')
        if User.objects.filter(username=phone).exists():
            raise forms.ValidationError(
                'This number is already registered. Please login.')
        return phone

    def save(self):
        return User.objects.create_user(
            username=self.cleaned_data['phone'],
            first_name=self.cleaned_data['full_name'],
            password=self.cleaned_data['password'],
        )


class LoginForm(forms.Form):
    """Customers login with phone number; staff can still use their username."""

    username = forms.CharField(
        max_length=150,
        label='Phone number or username',
        widget=forms.TextInput(attrs={
            'placeholder': 'Phone number',
            'autocomplete': 'username',
            'autofocus': True,
        }),
    )
    password = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Password',
            'autocomplete': 'current-password',
        }),
    )

    def clean_username(self):
        raw = self.cleaned_data['username'].strip()
        return normalize_phone(raw) or raw


class StaffCreationForm(UserCreationForm):
    """Admin creates a staff login (kitchen / server / store) with a role."""

    email = forms.EmailField(required=False, label='Email')
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        label='Role',
        widget=forms.RadioSelect,
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get('email', '')
        user.is_staff = True
        user.is_superuser = False
        if commit:
            user.save()
            Profile.objects.update_or_create(
                user=user,
                defaults={'role': self.cleaned_data['role']},
            )
        return user