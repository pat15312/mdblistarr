#!/bin/bash
set -euo pipefail
python /usr/src/app/mdblistrr/runtime_secrets.py
if [ "${RESET_DB:-}" = "1" ]; then rm -f /usr/src/db/db.sqlite3; fi
python /usr/src/app/manage.py migrate --noinput
python /usr/src/app/manage.py encrypt_secrets
python /usr/src/app/manage.py secure_startup
python /usr/src/app/manage.py run_task_scheduler &
python /usr/src/app/manage.py runserver 0.0.0.0:${PORT:-5353}
exec "$@"
