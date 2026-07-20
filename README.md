# mdblistarr

Companion app for [mdblist.com](https://mdblist.com) for better Radarr and Sonarr integration.

## Docker Hub image

[linaspurinis/mdblistarr](https://hub.docker.com/r/linaspurinis/mdblistarr)

## Basics

- Connects MDBList with Radarr and Sonarr.
- Uploads your current library state back to MDBList on schedule.
- Pulls MDBList queue items and sends add requests to Radarr/Sonarr.
- Supports multiple Radarr/Sonarr instances.
- Runs as a simple Docker container with persistent DB volume.

### Basic workflow

1. Connect your MDBList account via OAuth (or enter an API key manually).
2. Add your Radarr and Sonarr instances.
3. Set quality profile and root folder mappings per instance.
4. Let scheduled sync keep MDBList and your ARR apps in sync.

## New in v2.3.0

- MDBList OAuth authentication: connect your account via the new "Connect with MDBList" button instead of copying an API key. Uses the OAuth 2.0 device authorization flow.
- API key auth still works until you connect via OAuth — once OAuth is connected, the API key is cleared and OAuth takes over.

## New in v2.2.4

- Added support for syncing library status across all configured servers

## New in v2.2.3

- Optional MDBList collection sync: enable "Sync Library Status" in the MDBList config tab to keep your MDBList collection up to date based on what is downloaded in Radarr/Sonarr.
- Configurable sync hour: choose which UTC hour of the day Radarr and Sonarr sync runs. A random hour is assigned automatically on first run to spread load across all users.
- Home page now shows last sync time and next sync estimate so you always know when to expect the next run.
- Fixed UI bug where the Radarr/Sonarr server form would reset after saving — the selected server now stays active across page reloads.

## New in v2.2.2

- Full sync now reports monitored and unmonitored items more reliably:
  - Radarr uses `hasFile` to mark downloaded vs missing.
  - Sonarr uses episode file statistics where available.
- Import list exclusions from Radarr/Sonarr are included in sync payloads.
- If a movie is already in Radarr, MDBListarr now triggers a Radarr `MoviesSearch` command instead of only logging a duplicate error.
- HTTP/JSON handling is more defensive for empty/invalid/compressed responses.

## App Configuration Screen

![image](https://github.com/user-attachments/assets/cdd58b1a-4b55-464d-84dd-55246ba6a096)

## MDBListarr

```sh
git clone --branch latest git@github.com:linaspurinis/mdblistarr.git
docker build -t mdblistarr .
docker run -e PORT=5353 -p 5353:5353 mdblistarr
```

```
services:
  mdblistarr:
    container_name: mdblistarr
    image: linaspurinis/mdblistarr:latest
    environment:
      - PORT=5353
    volumes:
      - db:/usr/src/db/
    ports:
      - '5353:5353'
volumes:
  db:
```

## Security hardening in this fork

MDBListarr now requires Django authentication for the configuration UI, logs, OAuth device-flow actions, connection tests, and state-changing endpoints. Only active staff or superuser accounts may use the application. `/healthz` and static assets remain unauthenticated.

### First-run setup and runtime secrets

The recommended home-server deployment does not require credentials or cryptographic keys in Compose. On first startup MDBListarr creates persistent runtime secret files in the existing application-data volume and then shows `/setup/` so you can claim the first administrator account in the browser. Complete this setup promptly on a trusted network because the first administrator has not yet been claimed.

Generated files live under:

- `/usr/src/db/secrets/django_secret_key`
- `/usr/src/db/secrets/mdblistarr_encryption_key`

Back up the entire `/usr/src/db` volume, not only `db.sqlite3`. Losing the generated encryption key makes encrypted application credentials unrecoverable. Deleting only `db.sqlite3` keeps the generated keys and makes the one-time setup page available again because the user table is empty.

Advanced/headless deployments may still provide explicit secrets. For each runtime secret, `_FILE` takes precedence over the direct environment variable, which takes precedence over the generated persistent file. `MDBLISTARR_ADMIN_USERNAME` plus `MDBLISTARR_ADMIN_PASSWORD` or `MDBLISTARR_ADMIN_PASSWORD_FILE` can still create the initial administrator during startup; otherwise web setup is used.

| Purpose | Variable | File alternative | Default persistent file |
| --- | --- | --- | --- |
| Initial administrator name | `MDBLISTARR_ADMIN_USERNAME` | n/a | web setup |
| Initial administrator password | `MDBLISTARR_ADMIN_PASSWORD` | `MDBLISTARR_ADMIN_PASSWORD_FILE` | web setup |
| Django signing secret | `DJANGO_SECRET_KEY` | `DJANGO_SECRET_KEY_FILE` | `/usr/src/db/secrets/django_secret_key` |
| Database secret encryption key | `MDBLISTARR_ENCRYPTION_KEY` | `MDBLISTARR_ENCRYPTION_KEY_FILE` | `/usr/src/db/secrets/mdblistarr_encryption_key` |
| Allowed hosts | `DJANGO_ALLOWED_HOSTS` | n/a | built-in LAN defaults |

A legacy `admin` account is disabled only when its stored password still verifies as the literal password `admin`; an `admin` account with a changed password is preserved.

### Encrypted credentials and migration

Sonarr API keys, Radarr API keys, the MDBList API key, and MDBList OAuth access and refresh tokens are encrypted at rest with Fernet authenticated encryption and the `mdblistarr:v1:fernet:` prefix. Existing plaintext values are migrated by `python manage.py encrypt_secrets` during container startup after migrations. The migration is idempotent and skips already encrypted values. If the encryption key is missing or wrong, startup fails rather than replacing secrets with blanks.

Back up `/usr/src/db` before upgrading. Older upstream images do not understand encrypted credentials, so rollback to upstream requires restoring the pre-migration database backup. Host-root, Docker-daemon, and running-container compromise remain outside this protection boundary.

Verify that plaintext credentials are absent without printing secrets by checking for the encrypted prefix and lengths, for example:

```sh
sqlite3 /usr/src/db/db.sqlite3 "select 'radarr', count(*) from mdblistrr_radarrinstance where apikey like 'mdblistarr:v1:fernet:%' union all select 'sonarr', count(*) from mdblistrr_sonarrinstance where apikey like 'mdblistarr:v1:fernet:%' union all select name, length(value) from mdblistrr_preferences where name in ('mdblist_apikey','mdblist_access_token','mdblist_refresh_token');"
```

### Generic Docker Compose example

```yaml
services:
  mdblistarr:
    image: ghcr.io/pat15312/mdblistarr:latest
    container_name: mdblistarr
    environment:
      PORT: "5353"
      DJANGO_ALLOWED_HOSTS: "mdblistarr,localhost,127.0.0.1,10.0.0.11,mdblistarr.lan"
    volumes:
      - ./db:/usr/src/db
    ports:
      - "5353:5353"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5353/healthz', timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
```

### Administration

Change an administrator password with `python manage.py changepassword USERNAME`. Add another staff administrator with `python manage.py createsuperuser`. Disable an administrator with `python manage.py shell -c "from django.contrib.auth import get_user_model; u=get_user_model().objects.get(username='NAME'); u.is_active=False; u.save()"`.

### Reverse proxies and HTTPS

Authentication protects the web interface; encryption protects copied database backups; HTTPS protects credentials in transit; none of these protect against host-root, Docker-daemon, or running-container compromise. Plain HTTP remains usable for a trusted home LAN, so Secure cookies and HSTS are not forced by default. For an untrusted network, terminate HTTPS at a trusted reverse proxy, set `SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1`, and configure `DJANGO_SECURE_PROXY_SSL_HEADER` only for proxy headers you actually trust.
