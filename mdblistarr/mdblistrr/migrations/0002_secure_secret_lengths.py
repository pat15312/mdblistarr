# Generated manually for encrypted secret storage.
from django.db import migrations, models
import mdblistrr.models

class Migration(migrations.Migration):
    dependencies = [('mdblistrr', '0001_initial')]
    operations = [
        migrations.AlterField(model_name='preferences', name='value', field=models.CharField(max_length=2048, null=True)),
        migrations.AlterField(model_name='radarrinstance', name='apikey', field=mdblistrr.models.EncryptedCharField(max_length=2048)),
        migrations.AlterField(model_name='sonarrinstance', name='apikey', field=mdblistrr.models.EncryptedCharField(max_length=2048)),
    ]
