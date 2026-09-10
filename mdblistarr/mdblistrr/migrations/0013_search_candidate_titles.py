from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('mdblistrr', '0012_radarrcleanupcandidate_target_title_and_more')]

    operations = [
        migrations.AddField(
            model_name='sonarrepisodesearchcandidate', name='target_title',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='radarrmoviesearchcandidate', name='target_title',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
