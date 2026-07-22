from django.contrib.auth import get_user_model
from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Preferences, SonarrInstance

class InitialAdminSetupForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({'class': 'form-control', 'autocomplete': 'username'})
        self.fields['password1'].widget.attrs.update({'class': 'form-control', 'autocomplete': 'new-password'})
        self.fields['password2'].widget.attrs.update({'class': 'form-control', 'autocomplete': 'new-password'})


class SonarrReconciliationForm(forms.Form):
    enabled = forms.BooleanField(label='Enable On Demand reconciliation', required=False)
    source = forms.ModelChoiceField(label='Permanent Sonarr source', queryset=SonarrInstance.objects.none(), required=False)
    target = forms.ModelChoiceField(label='Sonarr On Demand target', queryset=SonarrInstance.objects.none(), required=False)
    include_specials = forms.BooleanField(label='Include specials in completeness checks', required=False)
    search_newly_eligible = forms.BooleanField(label='Search newly eligible On Demand episodes', required=False)
    interval_minutes = forms.ChoiceField(label='Reconciliation interval', choices=[('5','Every 5 minutes'),('15','Every 15 minutes'),('30','Every 30 minutes')])
    cleanup_enabled = forms.BooleanField(label='Enable automatic duplicate-file cleanup', required=False)
    cleanup_dry_run = forms.BooleanField(label='Dry-run cleanup', required=False)
    cleanup_grace_hours = forms.ChoiceField(label='Cleanup grace period', choices=[('0','Immediately'),('1','1 hour'),('6','6 hours'),('12','12 hours'),('24','24 hours'),('48','48 hours'),('168','7 days')])
    cleanup_max_deletions_per_run = forms.IntegerField(label='Maximum file deletions per reconciliation run', min_value=1, max_value=500)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = SonarrInstance.objects.order_by('name')
        self.fields['source'].queryset = qs.filter(is_library_source=True)
        self.fields['target'].queryset = qs.filter(is_ondemand_target=True)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs.update({'class': 'form-select'})
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.update({'class': 'form-check-input'})

    def clean(self):
        data = super().clean()
        if data.get('enabled'):
            if not data.get('source') or not data.get('target'):
                raise forms.ValidationError('Select both a permanent Sonarr source and an On Demand target.')
            if data.get('source') == data.get('target'):
                raise forms.ValidationError('Permanent source and On Demand target must be different Sonarr instances.')
        return data

    def save_preferences(self):
        data = self.cleaned_data
        Preferences.set_value('sonarr_reconciliation_enabled', '1' if data.get('enabled') else '0')
        Preferences.set_value('sonarr_reconciliation_source_id', str(data['source'].id) if data.get('source') else '')
        Preferences.set_value('sonarr_reconciliation_target_id', str(data['target'].id) if data.get('target') else '')
        Preferences.set_value('sonarr_include_specials', '1' if data.get('include_specials') else '0')
        Preferences.set_value('sonarr_search_newly_eligible', '1' if data.get('search_newly_eligible') else '0')
        Preferences.set_value('sonarr_reconciliation_interval_minutes', data.get('interval_minutes') or '15')
        Preferences.set_value('sonarr_cleanup_enabled', '1' if data.get('cleanup_enabled') else '0')
        Preferences.set_value('sonarr_cleanup_dry_run', '1' if data.get('cleanup_dry_run') else '0')
        Preferences.set_value('sonarr_cleanup_grace_hours', data.get('cleanup_grace_hours') or '24')
        Preferences.set_value('sonarr_cleanup_max_deletions_per_run', str(data.get('cleanup_max_deletions_per_run') or 25))
