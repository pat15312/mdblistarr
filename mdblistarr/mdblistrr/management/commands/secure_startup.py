from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from mdblistrr.crypto import read_secret

class Command(BaseCommand):
    help = "Bootstrap secure admin credentials and disable legacy admin/admin."

    def handle(self, *args, **options):
        User = get_user_model()
        username = read_secret("MDBLISTARR_ADMIN_USERNAME") or "admin"
        password = read_secret("MDBLISTARR_ADMIN_PASSWORD")
        with transaction.atomic():
            legacy = User.objects.filter(username="admin", is_active=True).first()
            if legacy and legacy.check_password("admin"):
                legacy.is_active = False
                legacy.set_unusable_password()
                legacy.save(update_fields=["is_active", "password"])
                self.stdout.write("Disabled legacy insecure admin account.")
            usable_admin_exists = User.objects.filter(is_active=True, is_staff=True, is_superuser=True).exists()
            if not usable_admin_exists:
                if not username or not password:
                    raise CommandError("No active staff administrator exists. Set MDBLISTARR_ADMIN_USERNAME and MDBLISTARR_ADMIN_PASSWORD or MDBLISTARR_ADMIN_PASSWORD_FILE for first startup.")
                user = User.objects.filter(username=username).first()
                if user:
                    user.is_active = True; user.is_staff = True; user.is_superuser = True
                    user.set_password(password)
                    user.save()
                    self.stdout.write(f"Secured existing administrator '{username}'.")
                else:
                    User.objects.create_superuser(username=username, email="", password=password)
                    self.stdout.write(f"Created initial administrator '{username}'.")
            else:
                self.stdout.write("Active staff administrator already exists; not changing passwords.")
