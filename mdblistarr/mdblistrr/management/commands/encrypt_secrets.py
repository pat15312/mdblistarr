from django.core.management.base import BaseCommand
from django.db import transaction, connection
from mdblistrr.crypto import encrypt, is_encrypted
from mdblistrr.models import Preferences, RadarrInstance, SonarrInstance

class Command(BaseCommand):
    help = "Encrypt plaintext application secrets in-place. Idempotent."
    def handle(self, *args, **options):
        changed=0
        with transaction.atomic():
            for model, field in [(RadarrInstance,'apikey'), (SonarrInstance,'apikey')]:
                table=model._meta.db_table
                with connection.cursor() as c:
                    c.execute(f"SELECT id, {field} FROM {table}")
                    for pk, value in c.fetchall():
                        if value and not is_encrypted(value):
                            c.execute(f"UPDATE {table} SET {field}=%s WHERE id=%s", [encrypt(value), pk]); changed+=1
            with connection.cursor() as c:
                c.execute("SELECT id, name, value FROM mdblistrr_preferences")
                for pk, name, value in c.fetchall():
                    if name in Preferences.secret_names() and value and not is_encrypted(value):
                        c.execute("UPDATE mdblistrr_preferences SET value=%s WHERE id=%s", [encrypt(value), pk]); changed+=1
        self.stdout.write(f"Encrypted or verified application secrets ({changed} updated).")
