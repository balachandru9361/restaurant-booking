from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from .models import Profile


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
        return user


class StaffCreationForm(UserCreationForm):
    """Used by the admin to create a Kitchen or Server login."""
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        widget=forms.RadioSelect,
        initial='kitchen',
    )

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2', 'role']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.is_staff = True  # staff login, not full admin
        if commit:
            user.save()
            Profile.objects.update_or_create(
                user=user, defaults={'role': self.cleaned_data['role']}
            )
        return user


class LoginForm(forms.Form):
    username = forms.CharField(
        widget=forms.TextInput(attrs={'placeholder': 'Username'})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Password'})
    )