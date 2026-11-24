# MDBList blacklist cleanup

Blacklist sources exclude already-imported On-Demand titles from reconciliation monitoring and searches. Optional cleanup removes the associated managed media files and then the entire movie or television-series catalogue entry. Permanent sources remain read-only. MDBList discovery lists and download clients are not modified.

## Configuration

Open the fourth **Blacklist** tab. Add a numeric MDBList list ID, a `https://mdblist.com/lists/user/list/` URL, or an Unlisted `/lists/share/…/` URL. The existing MDBList API key or OAuth connection supplies authentication. Access to owned private and Unlisted lists depends on that account and MDBList's API access policy. Share references are encrypted at rest and are not echoed in the saved-source table.

Up to 20 sources can be configured. The table shows the resolved name/ID, unique movie and series counts, last successful refresh and any error. **Refresh lists now** validates all sources. Removing a source takes effect during the next reconciliation; titles still present in another source remain blacklisted.

Removal is **disabled** and **dry run is on** by default. These settings are independent of permanent-duplicate cleanup. The blacklist grace period defaults to 24 hours and supports 0–168 hours. Each product's existing deletion-attempt limit (default 25) is shared by both cleanup reasons. A title with two files needs three attempts: two native file requests and one catalogue request. Existing duplicate cleanup consumes its budget first.

Blacklist suppression and candidate maintenance run whenever the product's reconciliation is enabled, including while blacklist removal is disabled. Dry run does not make ordinary reconciliation read-only: it still applies the normal monitoring decisions, including unmonitoring blacklisted titles. Disabling reconciliation stops these evaluations too.

## Membership evidence

The integration uses the [authenticated MDBList API](https://api.mdblist.com/docs/), including its supported list/share metadata and items endpoints. It does not scrape HTML. Numeric IDs are bound to returned list identity. Username/slug references may resolve more than one typed list; these become separate configured sources.

Movies match by TMDb ID. Series match by TVDb ID; missing TVDb identities are translated using the authenticated TMDb-show metadata endpoint and accepted only when both provider identities and media type agree. Conflicting identities within or across lists block evaluation. Mixed lists and duplicate memberships are supported; season/episode-only memberships and identities without verified mappings are rejected.

Membership retrieval validates totals and pagination continuation, supports cursor and legacy offset pagination, and is bounded to 100 pages of 1,000 entries per source. Because the API does not promise an atomic revision token, each complete membership read is repeated and compared, with list metadata rechecked between reads. Same-count changes detected between reads fail validation. Requests have connection/read timeouts and no redirects or automatic retries. A shared 120-second client work budget prevents starting further requests after its deadline; an already-started bounded request may finish later.

Reconciliation uses one validated union for the run; successful snapshots can be reused across products for 60 seconds. Local source configuration changes invalidate ongoing write decisions. A long reconciliation does not continually re-fetch the same sources for each title. External list edits after that run's snapshot take effect at a subsequent refresh. Every destructive stage requires a new complete membership verification, followed by fresh native evidence and a final freshness check.

Failed, partial, malformed or conflicting responses never become empty lists. The last successful snapshot remains visible, but a failed refresh prevents new reconciliation writes from that run and cannot authorise deletion. Rate-limit errors are reported without discarding the snapshot. Large lists or frequent live removal may require an MDBList account quota suitable for this request volume.

## Native removal and storage safety

Whole-title cleanup has a related persistent candidate/event model because permanent-source file identities do not apply. Its reason is `blacklisted`. It retains the target product, instance fingerprint, native and external title IDs, title/path/creation identity, exact file identities, grace timestamps and original request history. No permanent evidence is fabricated.

An exact matching pending/ready duplicate-file candidate is cancelled when blacklist cleanup takes responsibility for the title. Its original reason, identities and error evidence remain intact, with a supersession event on the title candidate. A previous uncertain file DELETE blocks blacklist removal until that exact file is independently verified absent. Removing blacklist membership lets normal duplicate reconciliation qualify the file again with a new grace period.

Before live work, validate the target's real storage topology. Native file removal must operate on the intended On-Demand library files or file symlinks. Directory symlinks, mounts that turn unlink into deletion of retained remote objects, or unknown storage semantics require deferral and operator investigation. MDBListarr does not inspect or manage external NzbDAV/InfiniDysk/rclone objects. Generic Arr API evidence cannot certify the underlying filesystem.

The application deliberately does **not** issue recursive title deletion with `deleteFiles=true`. Sonarr/Radarr can remove the catalogue record before asynchronous folder deletion completes; folder traversal may have wider effects on directory links. Instead it:

1. Confirms the configured target is On Demand, is not a Permanent source and does not alias a source endpoint or credential.
2. Verifies the exact unmonitored title and complete associated file inventory. Sonarr also requires all linked episodes to be unmonitored and the episode/file inventories to agree.
3. Defers for unresolved application searches, active or uncertain native commands, target downloads/imports, or incomplete queue evidence.
4. Requires native `deleteEmptyFolders=false` and an empty recycle-bin setting before deleting files. It never changes these settings automatically.
5. Persists intent, submits one exact native episode-file/movie-file DELETE, and verifies the original file resource is absent and the remaining inventory matches.
6. Once all associated managed files are verified absent, persists intent and removes the catalogue entry with `deleteFiles=false` and `addImportExclusion=false`. It verifies both the exact resource and canonical identity are absent from the catalogue.

The native per-file operation can also remove sidecars that Arr manages with that file. Empty folders and unrelated sidecars remain after the native stage. The optional Jellyfin continuation below may remove verified-empty local directories; it never removes files or download-client records. API absence is verification of native catalogue/file state, not independent proof of remote-object or playback state. Native filesystem semantics were inspected in [Sonarr's deletion service](https://github.com/Sonarr/Sonarr/blob/51a2412af235197ce15d7d4a17c053a8e2c26609/src/NzbDrone.Core/MediaFiles/MediaFileDeletionService.cs) and [Radarr's deletion service](https://github.com/Radarr/Radarr/blob/25a2124d7d5bb10aae6b89726577303f4bd60f75/src/NzbDrone.Core/MediaFiles/MediaFileDeletionService.cs); qualify the installed versions before activation.

## Recovery and cancellation

A timeout, malformed response or failed post-delete verification retains original intent and stops further live blacklist removals for that run. Restarts perform read-only reconciliation of that original request. An unverified DELETE is never automatically repeated, even after a definite HTTP failure; a later observed absence can resolve it. This conservative policy can require operator investigation after a rejected request. Do not delete lifecycle rows to force a retry.

Changes to file/title identity before any deletion restart grace. Changes after destructive intent retain the original evidence and defer. Removing membership cancels pending work, including any remaining stages after partial removal. The history reports verified file removals; missing files or deleted titles are not restored. Re-adding a cancelled title before any destructive intent starts a new grace period. Once destructive intent exists, it is not silently reused for a new removal cycle.

Configuration saves are serialised with the final native submission gate, and blacklist settings save atomically. Grace, enablement, dry run, target roles, membership and native evidence are rechecked before intent. A search rejected by a local configuration guard before HTTP is recorded as known-unsent and preserves its pending intent and retry predecessor.

## Arr Health

The existing Cleanup section displays duplicate-file and whole-title totals separately. Historical file quantities retain their original meaning. Combined links count candidate records, not physical files: each title contributes one whole-title record even when its cleanup needs several file requests.

The shared paginated history includes **Reason**, **Action**, external identity, preserved title, timestamp, state and outcome. Completed actions distinguish episode file, movie file, entire movie and entire television series. Pending/cancelled actions use prospective wording; partial progress shows the number of verified associated-file removals. Active candidates, uncertain requests and source-refresh errors are visible without any network request during page rendering.

## Upgrade and initial acceptance

Release 2.5.0 adds migration `0014`; existing cleanup/search history, credentials and schedules are retained. Back up a consistent application database together with its matching encryption/Django secrets and deployment configuration before upgrading. Rehearse migration and the recovery procedure on disposable copies with the exact candidate and previous images. Do not use `RESET_DB` or downgrade/drop new lifecycle tables to hide state.

Initially keep blacklist removal disabled and dry run on. Configure and refresh a list, independently confirm canonical IDs against the target, observe monitoring/search suppression, inspect candidates and Health, and confirm duplicate-cleanup settings are unchanged. This demonstrates matching and non-destructive evaluation, not successful live file removal.

Before later enabling live cleanup, review Ready/uncertain/partial candidates, native activity, installed-version storage semantics, Arr deletion settings, backups and the combined attempt cap. Enabling removal and turning dry run off may immediately act on candidates whose grace has already elapsed. An operator must explicitly approve that activation.


## Optional Jellyfin completion

Jellyfin can retain a television series while its empty series/season directories remain. A library refresh alone does not resolve that condition. Isolated Jellyfin 12.4.0 qualification reproduced this for Series/Season rows; Movie rows disappeared after file removal and refresh even with empty movie directories retained. The integration checks each product independently and does not assume identical catalogue behaviour. Native Arr `deleteEmptyFolders` traverses directories recursively; whole-title `deleteFiles=true` also has asynchronous filesystem effects. Neither provides the persisted, exact, non-recursive directory operation required here. Jellyfin item DELETE can delete filesystem contents and is never used. Existing Arr notifications can refresh libraries but do not prove title disappearance or remove these directories safely.

The optional integration extends the existing product reconciliation. It requires explicitly approved writable **local On-Demand library roots**, the same absolute paths as Arr and Jellyfin, and a file-injected Jellyfin API credential. No additional container, scheduler, Docker socket, download-client access or remote-storage mount is required. Without this configuration, existing native cleanup behaves as before.

Before the first native deletion, the integration captures the approved root, title and season device/inode identities, Jellyfin server/library identity and matching item IDs. It then repeats the existing final native checks. Sonarr season paths must be substantiated by native file paths or matching Jellyfin series/season evidence; unexpected subdirectories defer. Radarr supports media versions within the movie directory and defers subdirectories. Historical native removals are never enrolled automatically.

After native completion, the continuation verifies the title remains absent from Arr. It opens every path component without following symlinks, revalidates the captured root and directory identities, rejects current mount crossings, checks emptiness including hidden entries, and persists intent before each single `rmdir`. It never unlinks files, follows their symlink targets, traverses nested season directories or removes sidecars to make a directory empty. Each directory attempt consumes the existing shared deletion budget. Fresh membership, current target/configuration, grace, enabled and dry-run gates apply again. Uncertain removal is reconciled read-only and never automatically repeated.

Root/title/season identities survive container recreation; namespace-local mount IDs are checked afresh, not persisted. Changed roots, permission failures, replacement directories, sidecars and concurrent imports defer. Linux does not provide inode-conditional `rmdir`: a same-name empty replacement in the final check-to-syscall interval is a residual race. `rmdir` atomically refuses nonempty directories; no file contents can be removed by this operation. Deployments that cannot accept that residual empty-directory race must leave the optional integration disabled.

Once the captured title directory is absent, the client verifies the complete scoped Jellyfin catalogue by original item IDs, title path/subtree and series/movie provider identity. Sonarr includes Series, Season and Episode rows; Radarr includes Movie rows, including multiple versions. Pagination is bounded to 200,000 items and repeated before claiming absence. HTTP calls have no redirects or retries, a 60-second client work budget and 5/15-second connect/read timeouts; a request already in progress may finish later. Large or changing catalogues defer rather than proving false absence.

If Jellyfin still retains the title, the continuation records intent and requests one recursive refresh of the configured library through `POST /Items/{libraryId}/Refresh`, without replacing metadata or images. A 204 response means submission only. A later reconciliation verifies disappearance; an idle library alone does not prove refresh completion. Failed/uncertain refreshes are not automatically resubmitted. At most 100 incomplete continuations are checked per product run, ordered by least recent check so blocked records cannot starve later ones.

Health preserves historical native **Deleted** counts and separately reports Jellyfin verified/incomplete outcomes, with bounded reasons. Native file removal, native title removal and Jellyfin disappearance remain distinct evidence. Disabled/dry-run settings stop new directory removals and refreshes; with integration configured, already-submitted work can still resolve through read-only absence checks. Disabling the integration itself leaves its original records pending.

For a blocked stage, inspect the original evidence and correct the cause without clearing intent or inventing a replacement operation. Allow natural scans to establish absence after an uncertain refresh; any deliberate extra refresh is an operator action. Never delete candidate/history rows to force a retry. Live removal activation remains separate from deploying the integration.
