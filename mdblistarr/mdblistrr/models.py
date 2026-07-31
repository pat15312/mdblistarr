# models.py
from django.db import models
from .crypto import decrypt, encrypt, SECRET_PREF_NAMES
from django.dispatch import receiver
from django.db.models.signals import pre_save, post_save, pre_delete

class EncryptedCharField(models.CharField):
    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        return decrypt(value)

    def get_prep_value(self, value):
        return encrypt(value)

class Preferences(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, unique=True)
    value = models.CharField(max_length=2048, null=True)

    @classmethod
    def secret_names(cls):
        return SECRET_PREF_NAMES

    @classmethod
    def get_value(cls, name, default=None):
        pref = cls.objects.filter(name=name).first()
        return pref.value if pref is not None else default

    @classmethod
    def set_value(cls, name, value):
        pref, _ = cls.objects.update_or_create(name=name, defaults={"value": value})
        return pref

    @classmethod
    def get_secret(cls, name, default=None):
        if name not in SECRET_PREF_NAMES:
            raise ValueError(f"{name} is not configured as a secret preference")
        pref = cls.objects.filter(name=name).first()
        if pref is None or pref.value in (None, ""):
            return default
        return decrypt(pref.value)

    @classmethod
    def set_secret(cls, name, value):
        if name not in SECRET_PREF_NAMES:
            raise ValueError(f"{name} is not configured as a secret preference")
        encrypted = encrypt(value)
        pref, _ = cls.objects.update_or_create(name=name, defaults={"value": encrypted})
        pref.value = encrypted
        return pref

    @classmethod
    def clear_secret(cls, name):
        return cls.set_secret(name, "")

    def save(self, *args, **kwargs):
        if self.name in SECRET_PREF_NAMES:
            self.value = encrypt(self.value)
        super().save(*args, **kwargs)
    
    class Meta:
        verbose_name_plural = "preferences"
        
    def __str__(self):
        return self.name

class RadarrInstance(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=255)
    apikey = EncryptedCharField(max_length=2048)
    quality_profile = models.CharField(max_length=255)
    root_folder = models.CharField(max_length=255)
    enable_queue_import = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.enable_queue_import and (not self.quality_profile or self.quality_profile == '0' or not self.root_folder or self.root_folder == '0'):
            raise ValidationError('Queue-import Radarr instances require a valid quality profile and root folder.')

class SonarrInstance(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=255)
    apikey = EncryptedCharField(max_length=2048)
    quality_profile = models.CharField(max_length=255, null=True, blank=True)
    root_folder = models.CharField(max_length=255, null=True, blank=True)
    is_library_source = models.BooleanField(default=True)
    is_ondemand_target = models.BooleanField(default=False)
    enable_queue_import = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.enable_queue_import and (not self.quality_profile or self.quality_profile == '0' or not self.root_folder or self.root_folder == '0'):
            raise ValidationError('Queue-import Sonarr instances require a valid quality profile and root folder.')
    
    def __str__(self):
        return self.name

class InstanceChangeLog(models.Model):
    INSTANCE_TYPES = [
        ('radarr', 'Radarr'),
        ('sonarr', 'Sonarr'),
    ]
    EVENT_TYPES = [
        ('added', 'Added'),
        ('deleted', 'Deleted'),
        ('name_changed', 'Name Changed'),
    ]
    
    instance_type = models.CharField(max_length=10, choices=INSTANCE_TYPES)
    instance_id = models.IntegerField()
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    old_value = models.CharField(max_length=100, null=True, blank=True)
    new_value = models.CharField(max_length=100, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)

@receiver(pre_save, sender=RadarrInstance)
def radarr_instance_about_to_save(sender, instance, **kwargs):
    if instance.pk:  # Only for existing instances, not new ones
        try:
            # Get the current state from DB before save happens
            instance._old_instance = RadarrInstance.objects.get(pk=instance.pk)
        except RadarrInstance.DoesNotExist:
            pass
        
# Signal handlers to track changes
@receiver(post_save, sender=RadarrInstance)
def radarr_instance_saved(sender, instance, created, **kwargs):
    print('radarr_instance_saved')
    if created:
        InstanceChangeLog.objects.create(
            instance_type='radarr',
            instance_id=instance.id,
            event_type='added',
            new_value=instance.name
        )
    else:
        # Check for name change using the cached old instance
        if hasattr(instance, '_old_instance') and instance._old_instance.name != instance.name:
            InstanceChangeLog.objects.create(
                instance_type='radarr',
                instance_id=instance.id,
                event_type='name_changed',
                old_value=instance._old_instance.name,
                new_value=instance.name
            )

@receiver(pre_delete, sender=RadarrInstance)
def radarr_instance_deleted(sender, instance, **kwargs):
    InstanceChangeLog.objects.create(
        instance_type='radarr',
        instance_id=instance.id,
        event_type='deleted',
        old_value=instance.name
    )

@receiver(pre_save, sender=SonarrInstance)
def rsonarr_instance_about_to_save(sender, instance, **kwargs):
    if instance.pk:  # Only for existing instances, not new ones
        try:
            # Get the current state from DB before save happens
            instance._old_instance = SonarrInstance.objects.get(pk=instance.pk)
        except SonarrInstance.DoesNotExist:
            pass

# Sonarr signal handlers
@receiver(post_save, sender=SonarrInstance)
def sonarr_instance_saved(sender, instance, created, **kwargs):
    if created:
        InstanceChangeLog.objects.create(
            instance_type='sonarr',
            instance_id=instance.id,
            event_type='added',
            new_value=instance.name
        )
    else:
        # Check for name change using the cached old instance
        if hasattr(instance, '_old_instance') and instance._old_instance.name != instance.name:
            InstanceChangeLog.objects.create(
                instance_type='sonarr',
                instance_id=instance.id,
                event_type='name_changed',
                old_value=instance._old_instance.name,
                new_value=instance.name
            )

@receiver(pre_delete, sender=SonarrInstance)
def sonarr_instance_deleted(sender, instance, **kwargs):
    InstanceChangeLog.objects.create(
        instance_type='sonarr',
        instance_id=instance.id,
        event_type='deleted',
        old_value=instance.name
    )

class Log(models.Model):
    id = models.BigAutoField(primary_key=True)
    date = models.DateTimeField()
    status = models.IntegerField()
    provider = models.IntegerField()
    text = models.TextField()
    
    class Meta:
        verbose_name_plural = "log"
        
    def __str__(self):
        return self.text
class SonarrCleanupCandidate(models.Model):
    REASON_PERMANENT_DUPLICATE = 'permanent_duplicate'
    STATUS_PENDING = 'pending'
    STATUS_READY = 'ready'
    STATUS_DELETED = 'deleted'
    STATUS_CANCELLED = 'cancelled'
    STATUS_ALREADY_ABSENT = 'already_absent'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_READY, 'Ready'),
        (STATUS_DELETED, 'Deleted'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_ALREADY_ABSENT, 'Already absent'),
    ]

    target_instance = models.ForeignKey(SonarrInstance, on_delete=models.CASCADE, related_name='cleanup_candidates')
    tvdb_id = models.IntegerField()
    target_series_id = models.IntegerField()
    episode_file_id = models.IntegerField()
    linked_episode_keys = models.JSONField(default=list)
    reason = models.CharField(max_length=64, default=REASON_PERMANENT_DUPLICATE)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    first_eligible_at = models.DateTimeField()
    last_confirmed_at = models.DateTimeField()
    ready_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['target_instance', 'episode_file_id'], name='uniq_sonarr_cleanup_target_file')
        ]

    def __str__(self):
        return f'{self.target_instance_id}:{self.episode_file_id}:{self.status}'


class SonarrEpisodeSearchCandidate(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SUBMITTED = 'submitted'
    STATUS_CANCELLED = 'cancelled'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_FAILED, 'Failed'),
    ]

    target_instance = models.ForeignKey(SonarrInstance, on_delete=models.CASCADE, related_name='episode_search_candidates')
    target_series_id = models.PositiveIntegerField()
    target_episode_id = models.PositiveIntegerField()
    tvdb_id = models.PositiveIntegerField()
    season_number = models.PositiveIntegerField()
    episode_number = models.PositiveIntegerField()
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    first_eligible_at = models.DateTimeField()
    last_confirmed_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    current_command = models.ForeignKey('SonarrEpisodeSearchCommand', null=True, blank=True, on_delete=models.SET_NULL, related_name='current_candidates')
    attempt_count = models.PositiveSmallIntegerField(default=0, help_text='Total accepted submission attempts, including the initial attempt.')
    retry_not_before = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['target_instance', 'target_episode_id'], name='uniq_sonarr_search_target_episode')
        ]

    def __str__(self):
        return f'{self.target_instance_id}:{self.target_episode_id}:{self.status}'


class SonarrEpisodeSearchCommand(models.Model):
    STATUS_SUBMITTING = 'submitting'
    STATUS_QUEUED = 'queued'
    STATUS_STARTED = 'started'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_ABORTED = 'aborted'
    STATUS_CANCELLED = 'cancelled'
    STATUS_ORPHANED = 'orphaned'
    STATUS_AMBIGUOUS = 'ambiguous'
    STATUS_UNAVAILABLE = 'unavailable'
    STATUS_SUPERSEDED = 'superseded'
    STATUS_CHOICES = [(value, value.title()) for value in (
        STATUS_SUBMITTING, STATUS_QUEUED, STATUS_STARTED, STATUS_COMPLETED,
        STATUS_FAILED, STATUS_ABORTED, STATUS_CANCELLED, STATUS_ORPHANED,
        STATUS_AMBIGUOUS, STATUS_UNAVAILABLE, STATUS_SUPERSEDED,
    )]

    target_instance = models.ForeignKey(SonarrInstance, on_delete=models.CASCADE, related_name='episode_search_commands')
    target_series_id = models.PositiveIntegerField()
    sonarr_command_id = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_SUBMITTING)
    sonarr_status = models.CharField(max_length=32, blank=True, default='')
    sonarr_result = models.CharField(max_length=32, blank=True, default='')
    submission_attempted_at = models.DateTimeField()
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    outcome_reconciled_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_fallback_checked_at = models.DateTimeField(null=True, blank=True)
    unavailable_since = models.DateTimeField(null=True, blank=True)
    retry_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='retries')
    attempt_number = models.PositiveSmallIntegerField(default=1)
    failure_reason = models.CharField(max_length=255, blank=True, default='')
    candidates = models.ManyToManyField(SonarrEpisodeSearchCandidate, through='SonarrEpisodeSearchCommandCandidate', related_name='search_commands')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['target_instance', 'sonarr_command_id'], condition=models.Q(sonarr_command_id__isnull=False), name='uniq_sonarr_search_command_per_target')]


class SonarrEpisodeSearchCommandCandidate(models.Model):
    command = models.ForeignKey(SonarrEpisodeSearchCommand, on_delete=models.CASCADE, related_name='candidate_links')
    candidate = models.ForeignKey(SonarrEpisodeSearchCandidate, on_delete=models.CASCADE, related_name='command_links')
    target_episode_id = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['command', 'candidate'], name='uniq_sonarr_search_command_candidate')]
