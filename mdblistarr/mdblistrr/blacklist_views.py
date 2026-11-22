from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .blacklist import add_source, refresh_blacklists, BlacklistError
from .forms import BlacklistSettingsForm
from .models import BlacklistSource
from .cleanup_lock import configuration_write_lock


@staff_member_required
@require_POST
@configuration_write_lock
def configure_blacklist(request):
    request.session['active_tab'] = 'blacklist'
    action = request.POST.get('action')
    try:
        if action == 'settings':
            form = BlacklistSettingsForm(request.POST)
            if not form.is_valid():
                raise BlacklistError('Select valid blacklist cleanup settings.')
            form.save()
            messages.success(request, 'Blacklist settings saved. Reconciliation continues to suppress blacklisted titles independently of removal settings.')
        elif action == 'add':
            add_source(request.POST.get('list_reference', ''))
            messages.success(request, 'Blacklist source validated and saved.')
        elif action == 'remove':
            source_id = request.POST.get('source_id', '')
            if not source_id.isdecimal() or len(source_id) > 10:
                raise BlacklistError('Select a configured blacklist source.')
            BlacklistSource.objects.filter(pk=int(source_id)).delete()
            messages.success(request, 'Blacklist source removed. Pending removals will be cancelled on the next reconciliation unless another list still includes the title.')
        elif action == 'refresh':
            refresh_blacklists()
            messages.success(request, 'Blacklist memberships refreshed successfully.')
        else:
            raise BlacklistError('Unknown blacklist configuration action.')
    except BlacklistError as exc:
        messages.error(request, str(exc))
    return redirect('home_view')
