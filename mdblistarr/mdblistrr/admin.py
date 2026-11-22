from django.contrib import admin
from .models import Preferences, Log
from django.utils.decorators import method_decorator
from .cleanup_lock import configuration_write_lock

class PreferencesAdmin(admin.ModelAdmin):
    list_display = ("name", "value", )
    search_fields = ('name', 'value', )
    # Hold through Django admin's enclosing transaction, including its commit.
    @method_decorator(configuration_write_lock)
    def changeform_view(self, request, *args, **kwargs):
        return super().changeform_view(request, *args, **kwargs)

    @method_decorator(configuration_write_lock)
    def delete_view(self, request, *args, **kwargs):
        return super().delete_view(request, *args, **kwargs)

    @method_decorator(configuration_write_lock)
    def changelist_view(self, request, *args, **kwargs):
        return super().changelist_view(request, *args, **kwargs)
admin.site.register(Preferences, PreferencesAdmin)

class LogAdmin(admin.ModelAdmin):
    list_display = ("date", "provider", "status", "text",  )
    search_fields = ('date', 'provider', 'status', )
admin.site.register(Log, LogAdmin)
