# Sonarr and Radarr behaviour

## Purpose

This document records the intended behavioural contract for Permanent/On-Demand reconciliation, search, and duplicate cleanup, alongside current lifecycle mechanics. [Product context](PRODUCT_CONTEXT.md) preserves the historical rationale. The [current implementation qualifications](#current-implementation-qualifications) identify limits without weakening that contract; [Operations](OPERATIONS.md#operational-defaults) owns the complete defaults and configuration reference.

These rules exist because apparently simpler behaviour can create incorrect monitoring, uncontrolled backlog searches, duplicate commands, or unsafe deletion.

Changes in this area should be treated as product behaviour changes, not merely internal refactoring.

## Shared Arr role model

Each product can have instances assigned distinct roles.

### Permanent / library source

A Permanent/library source:

- contributes library and comparison evidence;
- may participate in traditional MDBList library-state sync;
- is read-only from On-Demand reconciliation;
- must not receive reconciliation monitoring changes;
- must not receive reconciliation searches;
- must not receive duplicate cleanup.

### On-Demand target

An On-Demand target may receive:

- monitoring changes;
- product-specific parent monitoring changes;
- explicitly enabled search commands;
- explicitly enabled duplicate-file cleanup.

The source and target must be distinct valid instances of the same Arr product.

## Queue-import independence

MDBList queue processing is separate from reconciliation.

A reconciliation source or target role does not implicitly permit queue imports.

Queue imports require:

- the global queue-processing option;
- the destination instance's queue-import role;
- a usable quality profile;
- a usable root folder.

Reconciliation and its search lifecycle do not require queue import to be enabled.

## Sonarr reconciliation

### Identity

Sonarr series are matched across source and target by TVDB ID.

Within a series, episode decisions use season/episode identity.

Malformed identity data must not be guessed.

### Relevant episodes

An episode is normally relevant to completeness and desired monitoring when:

- its season and episode numbers are valid;
- it is not an excluded season 0 special;
- it has a valid air date;
- it has aired.

The current default excludes season 0 specials.

### Future episodes

Future episodes are not yet wanted and should be unmonitored by reconciliation.

They do not make the Permanent series incomplete for current On-Demand purposes.

### Unscheduled episodes

A legitimate Sonarr placeholder can have no `airDateUtc` and no `airDate`.

That is **unscheduled**, not malformed.

Unscheduled episodes:

- are ignored for current completeness;
- are not searched;
- are desired unmonitored on the target;
- may become eligible later when Sonarr supplies a valid aired date.

### Malformed date or episode data

A non-empty invalid date is different from an absent date.

Malformed episode data creates uncertainty and should fail closed for the affected decision/reconciliation scope rather than being treated as future or unscheduled.

### Permanent-series completeness

A Permanent series is complete only when:

- there is at least one relevant episode;
- every relevant episode has a file;
- relevant episode data is well formed.

Ignored specials, future episodes, and unscheduled episodes do not make the series incomplete.

The presence of any single file is not enough to make the entire series complete.

### Desired target episode monitoring

For each valid target episode:

- if it is not currently relevant, desired monitoring is false;
- if the Permanent source has the corresponding relevant episode file, desired monitoring is false;
- otherwise the relevant target episode is wanted and desired monitoring is true.

A target-only series can therefore have relevant missing episodes monitored even if no corresponding Permanent series exists.

A file already present on the Sonarr target does not by itself make desired monitoring false when the Permanent copy is missing. Search eligibility additionally requires the target episode to be missing. This differs deliberately from Radarr's target-file monitoring rule.

### Season monitoring

Season monitoring is derived from desired episode monitoring.

A season is wanted when at least one episode in that season is wanted.

Episode updates are applied before dependent season updates. Failures in prerequisite episode writes should prevent later dependent writes for that series.

### Top-level series monitoring

Top-level series monitoring is derived from whether any season/episode remains wanted.

A series with wanted eligible content should be monitored.

A series with no wanted eligible content should be unmonitored.

This prevents an imported target series from remaining top-level unmonitored while its episodes are wanted.

### Native Sonarr import lists

A native Sonarr import list can be used to populate the On-Demand target, but it should not independently control the backlog that MDBListarr is intended to reconcile.

Recommended behaviour:

| Sonarr import-list setting | Value |
| --- | --- |
| Automatic Add | Enabled |
| Search for Missing Episodes | Off |
| Monitor | None |
| Monitor New Seasons | No New Seasons |
| Root folder | Intended On-Demand target root |
| Quality profile | Intended On-Demand target profile |

Tags may be used where required by the operator.

The important principle is that record population and search/monitoring authority remain separate.

## Radarr reconciliation

### Identity

Radarr movies are matched by TMDB ID.

Identity conflicts or malformed IDs should fail closed.

### Availability and file evidence

The target's `isAvailable` value is authoritative for current target availability.

The desired monitoring rules are:

- if the Permanent source has the movie file, target monitoring is false;
- if the On-Demand target itself already has the movie file, target monitoring is false;
- if the movie is available and missing from both, target monitoring is true;
- if the movie is unavailable, target monitoring is false.

A target-only movie follows the same target availability/file rules without requiring a matching Permanent record.

### Native Radarr import lists

A native Radarr import list may populate the target, but should not bypass MDBListarr's monitoring/search lifecycle.

Recommended behaviour:

| Radarr import-list setting | Value |
| --- | --- |
| Monitor | None |
| Search on Add | Disabled |

Root folder, quality profile, and other normal Radarr settings remain operator choices for record population.

## Search lifecycle

### Search is opt-in

Automatic searches for newly eligible media default off.

While reconciliation remains enabled, disabling new search submission still allows candidate discovery, maintenance and reconciliation of previously recorded commands. It stops new reconciliation search POSTs, including automatic retries. Disabling reconciliation itself stops this maintenance too.

### Candidate creation

When an item is eligible missing media after required monitoring confirmation, MDBListarr creates or maintains a persistent candidate independently of the search-submission switch. Enabling submission later can search the accumulated pending backlog.

Candidates exist so that search intent survives restarts and is not inferred solely from transient in-memory work. Existing valid `lastSearchTime` evidence can suppress candidate creation or resolve pending work; newly monitored eligibility and retry lineage also affect whether a candidate is retained or reset. Do not equate “newly eligible” with “discovered only after the switch was enabled.”

### Persist before POST

Before sending a reconciliation search command, MDBListarr transactionally persists a submitting command, its exact candidate relationships and the candidates’ current-command links.

This protects against a process or network failure after Arr accepts the command but before MDBListarr receives or persists the response.

### One-shot command submission

Reconciliation search command POSTs are intentionally not transparently retried by the HTTP transport. The legacy queue-search path is qualified [below](#current-implementation-qualifications).

A network retry at that layer could submit the same search more than once.

Recovery and retry belong in the persistent lifecycle where command identity and evidence can be reasoned about explicitly.

### Command states

Candidate and command states are separate persisted concepts. Both products have these candidate states:

| Candidate state | Meaning |
| --- | --- |
| `pending` | A search need awaiting permitted submission or retry timing |
| `submitted` | Intent associated with submission, accepted/executed work or resolving evidence; not proof of acquisition |
| `cancelled` | Retired current need, with lifecycle history retained |
| `failed` | Current unresolved need that exhausted automatic retries |

Commands use the following states:

| Command state | Meaning |
| --- | --- |
| `submitting` | Intent saved before the external POST; acceptance may not yet be known |
| `queued` | Arr has queued the command |
| `started` | Arr reports execution in progress |
| `completed` | Execution completed or valid independent evidence resolved a missing command |
| `failed`, `aborted`, `cancelled`, `orphaned` | Terminal outcomes subject to evidence reconciliation and retry limits |
| `ambiguous` | Acceptance, identity or outcome cannot safely be established |
| `unavailable` | Tracked command state is unavailable; elapsed grace alone does not permit retry |
| `superseded` | An attempt no longer controlling the candidate, including a definite submission rejection |

Queued or running commands must not be resubmitted. Current command associations and explicit command/candidate links preserve retry lineage; candidates have no `acquired` or `completed` state. See [models](../mdblistarr/mdblistrr/models.py).

Submissions are batched at up to 100 items per command; this is not a total per-run search cap. Recovery may adopt an exact, uniquely matching unclaimed command whose queued time is within ten minutes of persisted intent. Per-command fallback polling is bounded to ten lookups per pass; deferred lookups do not establish a terminal outcome.

### Completion is not acquisition

An Arr command reaching a completed state means the Arr command finished.

It does not prove:

- that a suitable release existed;
- that a release was grabbed;
- that a file was imported.

Valid command completion leaves candidates `submitted` and suppresses automatic repetition even when files remain missing. Resulting file state or a valid `lastSearchTime` at or after the relevant attempt/eligibility timestamp can also resolve uncertainty. A search timestamp is evidence of search activity, not file acquisition.

### Completed searches should not loop

A completed search must not be repeatedly resubmitted merely because the media remains missing.

This avoids uncontrolled repeated searches for content that simply was not available.

### Retryable failure

Genuine failed, aborted, cancelled, or otherwise validated retryable terminal outcomes may be retried according to configured delay and retry limits.

Retries count after the initial accepted attempt. Definite HTTP rejection returns candidates to pending without counting an accepted attempt; the retry limit is not a universal limit on every rejected HTTP POST. See [current defaults and ranges](OPERATIONS.md#operational-defaults).

### Missing command history

An accepted command can disappear from Arr command history.

When no independent file/search evidence resolves it:

- it remains uncertain/unavailable;
- a grace period is used to distinguish recent disappearance from longer-lived uncertainty;
- passing the grace period does not automatically convert uncertainty into a retryable terminal failure;
- unresolved uncertainty continues to block unsafe assumptions and destructive cleanup.

The missing-command grace clock starts when unavailability is first observed. [Operations](OPERATIONS.md#operational-defaults) lists its default and allowed range.

### Retry-exhausted candidates

Retry exhaustion represents an unresolved current need, not a permanent historical badge.

If later validated reconciliation evidence proves the item is no longer eligible, the failed candidate should be retired/cancelled while preserving command history and attempt lineage.

Uncertain evidence must not retire the failure prematurely.

## Sonarr duplicate-file cleanup

### Purpose

Sonarr cleanup exists solely to remove an On-Demand episode file that is confirmed redundant because the relevant media is safely represented in the Permanent source.

It does not delete:

- Permanent files;
- Sonarr series records;
- arbitrary paths;
- download-client filesystems directly.

### File identity

Cleanup is file-oriented.

A Sonarr episode file may represent multiple linked episodes. Deletion is safe only when every linked episode represented by that target file is confirmed to satisfy the duplicate condition.

Every linked target episode must have a file, be confirmed unmonitored and have a matching Permanent file, with the reconciliation decision explicitly identifying a Permanent duplicate. Unmonitoring for a future date, an unscheduled episode or an excluded special does not establish duplication.

The exact target episode-file identity must remain stable through the cleanup lifecycle.

### Grace period

Eligible files become persistent cleanup candidates.

The intended safeguard is stable eligibility through the configured grace period. The implementation records observations and elapsed time from `first_eligible_at`; it does not continuously observe Arr between runs. Changed linked episode sets or reappearing terminal candidates reset the Sonarr grace clock, and live deletion must still pass fresh validation.

Candidate discovery and grace tracking occur while cleanup deletion is disabled, provided reconciliation reaches cleanup maintenance. Enabling live deletion does not start a new grace period for every existing candidate. With zero-hour grace, a newly created Sonarr candidate is evaluated for readiness on a subsequent pass.

### Dry-run

Cleanup defaults to disabled.

Dry-run defaults on and exposes candidate activity without deleting files. It does not promise that a later live revalidation will pass. Review accumulated Ready candidates before enabling live deletion.

### Pre-delete revalidation

Immediately before live deletion, MDBListarr re-fetches and revalidates the relevant evidence.

A deletion is deferred or cancelled when evidence has changed, is incomplete, malformed, ambiguous, or unavailable.

### Deletion budget

Live cleanup is bounded per reconciliation.

The cap counts deletion attempts rather than only successful deletions, across the product’s reconciliation run. Dry-run does not consume a live deletion budget. See [defaults](OPERATIONS.md#operational-defaults).

### Post-delete verification

After requesting deletion through Sonarr, MDBListarr verifies that the exact target file is absent.

Uncertain destructive verification should stop later live deletes for that run.

## Radarr duplicate-file cleanup

### Purpose

Radarr cleanup exists to remove an exact On-Demand movie file that is confirmed redundant against the Permanent source.

It does not delete movie records, Permanent files, or arbitrary filesystem paths.

### Movie identity

Source and target evidence must represent the same TMDB movie.

Both must have validated file evidence, and the target movie must be confirmed unmonitored. File identity, monitoring confirmation and search-lifecycle safety are checked in addition to edition compatibility.

### Edition safety

Two files for the same TMDB movie are not automatically interchangeable.

Edition metadata is therefore part of the cleanup-safety decision.

Current comparison treats missing/`None` and blank editions as empty, collapses whitespace and compares case-insensitively. Two empty editions match; empty versus a named edition conflicts. Non-string, non-null values are malformed. This uses Arr metadata rather than inspecting or hashing media files.

A meaningful edition conflict blocks deletion.

### Candidate identity and lifecycle

The target movie-file identity is persisted as the cleanup candidate identity.

Eligibility must remain stable through the grace period and survive exact pre-delete revalidation.

The same default dry-run, grace, deletion-budget and fail-closed principles used by Sonarr apply. Radarr tracks source file and normalised editions as evidence: changes reset grace. A replacement target file gets its own candidate identity. Unlike Sonarr’s first-pass deferral, Radarr can mature a new candidate during the same pass when grace is zero. Candidate maintenance also runs while deletion is disabled.

## Cleanup and search interaction

Destructive cleanup must not race ahead of unresolved search uncertainty.

Where search state is ambiguous, missing, unreconciled, or otherwise required for safety, cleanup should remain blocked rather than assuming a safe duplicate condition.

## Cleanup candidate states

Both products persist `pending`, `ready`, `deleted`, `cancelled` and `already_absent` states. `ready` means grace has matured in recorded state; it is not unconditional permission to delete. `already_absent` distinguishes validated disappearance from a confirmed successful deletion. Changed or uncertain evidence can cancel, reset or defer work without deleting anything.

Sonarr verifies absence through validated episode/file associations for the exact candidate. Radarr verifies the exact movie-file resource, requiring absence evidence rather than treating a missing movie record as sufficient. State and title/year display metadata are described in [Architecture](ARCHITECTURE.md#persistence-and-models).

## Arr Health behaviour

Arr Health is an observer: rendering must make no external calls, and health-recording failure must not affect reconciliation. Display current actionable work separately from historical submissions, with bounded details, deterministic ordering and sanitised labels. [Operations](OPERATIONS.md#operational-health) owns classifications, limits and operator interpretation.

## Current implementation qualifications

These qualifications describe the reviewed implementation, not exceptions to the intended product principles:

- **Role enforcement:** checks require source/target role flags and different database IDs, but permit dual-role instances and do not detect endpoint aliases. Configure genuinely separate services; a library-source flag alone is not an enforced universal write prohibition.
- **Sonarr evidence validation:** source episode keys/file flags are less strictly validated than the intended fail-closed contract. Conflicting keys can overwrite earlier evidence, non-boolean source file flags can be treated as missing, and the date parser can accept malformed strings with a valid date prefix. These gaps must not be treated as intended permissive semantics. Target episode relevance drives monitoring; source dates are not equivalently validated in that decision helper.
- **Traditional queue searches:** queue adds request Arr-native searches, and the already-present Radarr movie fallback uses an explicit search through ordinary retrying transport. Those searches do not use the reconciliation command/candidate lifecycle. The scope discrepancy against the persistent-search principle remains separate from preserving useful upstream integration.

Sources: [role forms](../mdblistarr/mdblistrr/forms.py), [Sonarr decisions](../mdblistarr/mdblistrr/sonarr_reconcile.py), [queue orchestration](../mdblistarr/mdblistrr/cron.py) and [Arr search wrappers](../mdblistarr/mdblistrr/arr.py). Logging qualifications are recorded once in [Security](SECURITY.md#logging-and-observability).

## Behaviour changes require deliberate review

Changes to any of the following should be treated as behavioural/safety changes and receive focused regression coverage:

- Permanent-source read-only guarantees;
- episode relevance/completeness;
- target monitoring rules;
- parent season/series monitoring;
- Radarr availability logic;
- candidate eligibility;
- search submission or retry rules;
- command uncertainty;
- cleanup eligibility;
- grace periods;
- pre-delete revalidation;
- post-delete verification;
- edition handling;
- health classification or health's zero-network contract.
