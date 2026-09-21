from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm


class KitchenStaffForm(UserCreationForm):
    """Used by admins (from the dashboard) to create a Kitchen/Chef login.
    The created user gets is_staff=True so login_view sends them to the
    kitchen dashboard, but is_superuser stays False so they can't access
    the full admin panel."""

    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get('email', '')
        user.is_staff = True
        user.is_superuser = False
        if commit:
            user.save()
        return user
