from django import forms
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Log


class LogFilterForm(forms.Form):
    from_date = forms.DateTimeField(label='From', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control', 'step': '1'}, format='%Y-%m-%dT%H:%M:%S'))
    to_date = forms.DateTimeField(label='To', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control', 'step': '1'}, format='%Y-%m-%dT%H:%M:%S'))
    provider = forms.ChoiceField(label='Event source', required=False,
        choices=[('', 'All'), ('2', 'Sonarr'), ('1', 'Radarr')],
        widget=forms.Select(attrs={'class': 'form-select'}))
    page_size = forms.TypedChoiceField(label='Rows per page', coerce=int,
        choices=[(10, '10'), (25, '25'), (50, '50')],
        widget=forms.Select(attrs={'class': 'form-select'}))

    def clean(self):
        data = super().clean()
        start, end = data.get('from_date'), data.get('to_date')
        if start and end and start > end:
            self.add_error('to_date', 'To must be on or after From.')
        return data


@require_GET
def log_view(request):
    params = request.GET.copy()
    params.setdefault('page_size', '25')
    form = LogFilterForm(params)
    logs = Log.objects.order_by('-date', '-id')
    if form.is_valid():
        data = form.cleaned_data
        if data['from_date']:
            logs = logs.filter(date__gte=data['from_date'])
        if data['to_date']:
            logs = logs.filter(date__lte=data['to_date'])
        if data['provider']:
            logs = logs.filter(provider=data['provider'])
    else:
        # Invalid filters must never silently broaden the result set.
        logs = logs.none()
    size = form.cleaned_data.get('page_size', 25)
    page = Paginator(logs, size).get_page(request.GET.get('page'))
    return render(request, 'log.html', {
        'logs': page, 'page_obj': page, 'filter_form': form,
        'log_timezone': timezone.get_current_timezone_name(),
    })
