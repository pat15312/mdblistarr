from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import UsernameField

class InitialAdminSetupForm(forms.Form):
    username = UsernameField(widget=forms.TextInput(attrs={'class':'form-control','autocomplete':'username'}))
    password1 = forms.CharField(label='Password', strip=False, widget=forms.PasswordInput(attrs={'class':'form-control','autocomplete':'new-password'}))
    password2 = forms.CharField(label='Password confirmation', strip=False, widget=forms.PasswordInput(attrs={'class':'form-control','autocomplete':'new-password'}))

    def clean_username(self):
        username = self.cleaned_data['username']
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError('A user with that username already exists.')
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', 'The two password fields did not match.')
        if p1:
            password_validation.validate_password(p1)
        return cleaned
