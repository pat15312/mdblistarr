# Security

Security hardening is part of this fork's product intent. This document owns the trust model, credential handling and public-hygiene policy. Deployment settings and recovery steps are in [Operations](OPERATIONS.md); behavioural safeguards are in [Arr behaviour](ARR_BEHAVIOUR.md).

## Administrative authentication

Application administration uses Django authentication. The access middleware normally requires an active authenticated staff user or superuser. The health view and manual operational views additionally require staff status; an unusual account with only the superuser flag should not be assumed to have access to every view.

First-run setup remains available whenever no active user with **both staff and superuser flags and a usable password** exists. Staff-only users and accounts with unusable passwords do not satisfy that condition. Browser setup validates passwords, serializes the final claim with a file lock and creates the administrator transactionally. An unclaimed setup endpoint must only be exposed to trusted users.

Startup can bootstrap explicit administrator credentials, but does not overwrite an existing usable administrator. It first disables the legacy insecure `admin/admin` account. Administrator passwords are not automatically generated. See [administrator state](../mdblistarr/mdblistrr/admin_state.py), [setup view](../mdblistarr/mdblistrr/views.py) and [secure startup](../mdblistarr/mdblistrr/management/commands/secure_startup.py).

Login, setup and `/healthz` are public endpoints; static assets are also exempt from the administration gate. JSON requests to protected routes distinguish setup required (503), authentication required (401) and insufficient privileges (403). State-changing actions use POST and Django CSRF protection.

## Secret storage and resolution

Arr API keys and the designated MDBList API/access/refresh-token preferences are encrypted at rest using authenticated Fernet encryption. Copying the SQLite database alone therefore does not reveal those integration credentials. URLs, instance names, general preferences and logs are not all encrypted. Administrator passwords use Django's password handling rather than the integration-secret field scheme.

The two runtime cryptographic secrets have distinct jobs:

- The Django signing secret protects signed/session data.
- The Fernet encryption key protects stored integration credentials.

The [Operations reference](OPERATIONS.md#environment-variables) defines secret-source precedence and injection settings. Selected unreadable/invalid secret files fail resolution rather than silently selecting a lower-priority source. Container startup can generate missing cryptographic secrets; this is separate from supplying explicit administrator bootstrap credentials.

Generated secrets default to `/usr/src/db/secrets`. Creation uses an atomic no-clobber publication mechanism; CI verifies directory mode `0700` and file mode `0600` in its Linux container environment. Explicit environment/file values are not copied into that directory. The full variable reference, including relocation and development controls, is in [Operations](OPERATIONS.md#environment-variables).

The credential-maintenance command authenticates existing ciphertext and encrypts legacy plaintext values. A mismatched encryption key fails startup validation. Losing or replacing the key makes matching encrypted values unrecoverable. Signing-key changes can invalidate signed/session data. Preserve the database and effective keys together through [backup and restore](OPERATIONS.md#upgrade-backup-and-restore).

Sources: [runtime resolution](../mdblistarr/mdblistrr/runtime_secrets.py), [encryption](../mdblistarr/mdblistrr/crypto.py), [credential maintenance](../mdblistarr/mdblistrr/management/commands/encrypt_secrets.py).

## Forms and credential exposure

Secret form fields must not echo stored API keys/tokens into the browser. Current password widgets suppress stored values; blank submissions preserve existing secrets where supported. Do not reveal credentials merely to simplify editing. Avoid embedding credentials in URLs, diagnostics or examples.

Encryption complements HTTPS, filesystem permissions, host/container security and external-service access restrictions. Use the least privilege the external service supports for the enabled operations. MDBListarr role flags do not alter Arr API-key permissions.

A complete stolen backup containing both database and encryption key can expose integration credentials. Protect backup access and storage accordingly; external secret injection must also be accounted for during recovery.

## Reconciliation and destructive boundaries

Permanent/library sources must remain read-only from reconciliation. Controlled monitoring, searches and exact duplicate-file deletion belong on On-Demand targets. Cleanup is not a retention, pruning or filesystem-management service. NzbDAV and other download-side components remain outside this boundary.

Destructive actions require stronger proof: stable exact target-file identity, a persisted grace period, immediate revalidation and post-delete verification, with bounded attempts. All linked Sonarr episodes must satisfy duplicate conditions. Radarr edition compatibility is additional evidence beyond same-TMDB identity. Preserve product-specific safeguards described in [Arr behaviour](ARR_BEHAVIOUR.md).

The intended posture is fail closed when required evidence is malformed, conflicting, unavailable or uncertain. Persistent search intent and one-shot submission protect against uncertain external side effects. Missing command history must not be treated as automatic failure or permission to retry/delete.

Current role enforcement and some validation/legacy search paths do not fully enforce these principles. The concise [implementation qualifications](ARR_BEHAVIOUR.md#current-implementation-qualifications) describe those limits; they are not authorisation to weaken the contract.

## Logging and observability

Logs and UI diagnostics should omit API keys, OAuth tokens, generated secrets, passwords, credential-bearing URLs, unnecessary raw external responses and unnecessary private paths. Sanitise and bound external titles, labels and large ID collections. Prefer concise reason codes and stable identifiers.

**Current qualification:** some traditional queue/sync paths still log or print raw external payloads. The text sanitiser is heuristic, and URL redaction preserves the authority component, so arbitrary URL-embedded credentials are not reliably removed. Do not assume all runtime logs are safe to publish; inspect and redact them first. This remains an implementation gap against the policy, not a relaxed logging standard. See [transport sanitisation](../mdblistarr/mdblistrr/connect.py) and [legacy orchestration](../mdblistarr/mdblistrr/cron.py).

Arr Health reads persisted local state and does not call MDBList/Sonarr/Radarr during rendering. It uses bounded details and generic error presentation. Health recording is best-effort and must not alter reconciliation outcomes. [Operations](OPERATIONS.md#operational-health) explains classifications and limits.

## Transport and proxy trust

For Internet-facing access, use HTTPS. A trusted reverse proxy may terminate TLS; configure secure cookies, complete trusted origins and an appropriate host allowlist using [Operations](OPERATIONS.md#reverse-proxy-and-https).

Forwarded headers are untrusted by default. Only enable proxy-header handling when a trusted proxy sets/strips the relevant values and clients cannot bypass that trust boundary. The explicit SSL-header mapping recognises proxied HTTPS; `TRUST_PROXY_HEADERS` additionally enables forwarded-host use. Do not weaken allowed-host validation to work around a proxy configuration issue.

External-service network controls remain useful even with encrypted credentials. The library-source role expresses intended use, not a separate read-only credential type supplied by MDBListarr.

## Public repository hygiene

Documentation, tests, fixtures and examples must be suitable for a public repository. Never commit:

- real credentials, secrets or tokens;
- production databases or identifying production logs;
- private IP addresses/hostnames, VPN/network topology or operator-specific storage paths;
- personal information or private media-library examples;
- local backups, scratch files, editor residue or generated artefacts.

Use generic or synthetic examples. Clearly fake test credentials are acceptable. Public application-contract paths such as `/usr/src/db`, loopback healthcheck addresses, generic example domains, repository/image names and upstream attribution are appropriate.

## Security-sensitive review

Changes to authentication, secrets, transport, proxy handling, searches or cleanup should identify the trust introduced, authoritative evidence, possible duplicate side effects, source-write risk, destructive identity and recovery implications. Test both allowed and blocked cases using the [development workflow](DEVELOPMENT.md).

Keep dependencies current and review useful upstream security fixes. Rebuild images after relevant dependency/base-image changes. Application tests and the current container smoke test do not constitute a dependency vulnerability scan or a complete host-security assessment.
