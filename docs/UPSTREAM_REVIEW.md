# Upstream review record

This record tracks the scoped review of upstream changes. It does not indicate a full upstream merge or replace the [upstream maintenance policy](RELEASES.md#upstream-maintenance).

## 2026-09-10: collection batch limit and authentication comparison

Reviewed against fork baseline `14d5d729134752c42e0322dbb428286bd490f244`.

### Collection batch limit: adapted

Source: upstream [a5bb84feabc968e2ebe80a635d964012472c3a44](https://github.com/linaspurinis/mdblistarr/commit/a5bb84feabc968e2ebe80a635d964012472c3a44).

Reduce the collection batch size from 250 to 200 in both traditional library-sync functions in [cron.py](../mdblistarr/mdblistrr/cron.py). This applies to collection additions and removals for both movies and shows. Sonarr batches count shows and preserve their nested season/episode payloads. Library-state uploads and On-Demand reconciliation are unchanged.

[CollectionBatchLimitTests](../mdblistarr/mdblistrr/test_upstream_sync.py) checks empty collections, exactly 200 items, 201 items and 401 items. It verifies batch sizes and complete ordered payloads for both additions and removals, including Sonarr episode metadata.

No schema or configuration changes are required. The upstream version bump is not imported; fork releases remain independent.

### Authentication and credentials: already covered

Source: authentication and credential portions of upstream [0c4426003004793e4923157e383b7f6038f8bb64](https://github.com/linaspurinis/mdblistarr/commit/0c4426003004793e4923157e383b7f6038f8bb64).

No authentication or credential changes are needed from this commit. The comparison covers source, existing security regressions, secret-field migrations, startup, forms and OAuth handling.

| Area | Comparison and disposition |
| --- | --- |
| Staff authentication and usable-administrator detection | `middleware.py` and `admin_state.py` are identical. Preserve the fork implementation. |
| First-run setup | The claim lock and initial-admin form match; the setup view differs only in where its administrator-state helper is imported. The fork stores the form in `forms.py`. |
| Encryption and credential maintenance | `crypto.py`, `encrypt_secrets.py` and `secure_startup.py` are identical. Encrypted model-field and secret-preference implementations also match. |
| Persistent runtime secrets | `runtime_secrets.py` differs only in whitespace. Both use atomic no-clobber publication, explicit secret-source precedence and Fernet-key validation. |
| Secret-field migration | The upstream `0004_secure_secret_lengths` operations are already represented by the fork's `0002_secure_secret_lengths`. Do not import the upstream migration chain. |
| Forms and OAuth | The fork already masks saved keys, preserves blank credential edits, retrieves saved keys for connection tests and encrypts OAuth tokens. It also validates a replacement MDBList key through a fresh client and returns generic OAuth transport-error messages, improvements that should be retained. |
| Host/proxy defaults | The fork uses a bounded default host list and requires opt-in for forwarded-header trust. The reviewed upstream settings allow all hosts and trust proxy headers by default. Preserve fork defaults. |
| Startup migration repair | Upstream adds `reconcile_migrations` for older deployments that regenerated migrations. It marks all-`AddField` migrations applied based on column presence alone. The fork uses committed migrations and normal `migrate`; no demonstrated fork upgrade need justifies importing that repair mechanism. |

Existing [security tests](../mdblistarr/mdblistrr/tests.py) cover authentication status codes, CSRF, login/logout, administrator bootstrap, encrypted preferences, OAuth credential use, blank edits, ciphertext validation, secret generation and concurrent setup. [Environment settings tests](../mdblistarr/mdblistrr/test_upstream_sync.py) cover host/proxy defaults and compatibility aliases.

This is a comparison of the identified upstream change, not a complete security audit. The existing [logging qualifications](SECURITY.md#logging-and-observability) remain; the upstream sanitiser does not resolve them.

### Other changes

Per-instance tags, Sonarr import-monitoring options, season-folder changes, Radarr minimum availability and Plex integration are excluded from this integration at the maintainer's request. These exclusions are not pending feature commitments. Selective adaptation does not make upstream commits ancestors of the fork, so GitHub's behind count may remain unchanged.

### Validation

- All 306 application tests passed in a disposable, network-disabled container. The 10 upstream compatibility tests also passed independently.
- Restoring the old 250-item limit in a disposable container made the new tests fail at 201 and 401 items for both products, confirming they detect the original behaviour.
- Docker build, migration drift check, fresh migration application, pending-migration check and Django system checks passed. The deployment check passed at the configured ERROR threshold, with warnings for unset HSTS and HTTPS redirect settings.
- The CI fresh-volume smoke procedure passed using a separate test port: setup and health endpoints, secret permissions, persistence across container recreation, absence of generated secrets from logs and timezone support.
- Documentation links, fragments and whitespace checks passed.

Validation used the Dockerfile's floating Python base, which resolved to Python 3.14.7 with Django 6.0.8. Python 3.12 CI and live MDBList/Arr requests were not run locally. The first full-suite attempt had one missing-key test failure because the test container had been pre-bootstrapped with a persistent encryption key; a clean container following CI's test setup passed the full suite without code changes.
