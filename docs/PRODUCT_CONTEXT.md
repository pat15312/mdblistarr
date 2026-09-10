# Product context

## Purpose of this document

This document records the durable product intent behind this MDBListarr fork.

The source code and tests are the authority for what the current build actually does. This document explains why the fork exists, which behaviours are deliberate, what is in scope, and which design principles should survive future refactoring.

If current implementation and stated product intent appear to disagree, treat that as a discrepancy to investigate rather than silently redefining the product around the code. Current qualifications are recorded briefly in [Arr behaviour](ARR_BEHAVIOUR.md#current-implementation-qualifications) and [Security](SECURITY.md#logging-and-observability); they do not change the principles here.

## Project origin

MDBListarr connects MDBList with Sonarr and Radarr. The upstream project provides the useful foundation of reporting library state to MDBList, synchronising MDBList collections, and optionally processing the MDBList add queue.

This repository is a fork of [`linaspurinis/mdblistarr`](https://github.com/linaspurinis/mdblistarr).

The fork initially diverged to improve security and deployment safety, including authenticated administration, encrypted stored credentials, persistent runtime secrets, safer first-run setup, and automated validation.

It subsequently evolved to support a more deliberate media-lifecycle model built around two different Arr roles:

- a **Permanent/library source**, representing media intended to be retained;
- an **On-Demand target**, representing media that may be acquired temporarily.

That distinction became the central product concept of the fork.

## The problem the fork is solving

Normal Sonarr and Radarr workflows do not inherently model the difference between content that is permanently retained and content that should exist only when it is not already satisfied by the permanent library.

That becomes especially important for partially retained television series.

A series can exist in the permanent library without being complete. Some relevant episodes may be retained while others are intentionally absent or still needed. A whole-series existence check, or a simplistic "has any file" interpretation, can therefore incorrectly make the entire series appear satisfied.

MDBListarr's On-Demand workflow solves this by comparing item-level evidence from a Permanent/library source with corresponding records in an On-Demand target, then applying only the permitted target-side changes.

## Core model

The relationship is intentionally asymmetric:

```text
Permanent/library source
        |
        | read evidence
        v
     MDBListarr
        |
        | controlled writes
        v
   On-Demand target
```

The Permanent/library source is evidence.

The On-Demand target is the place where reconciliation may act.

Reconciliation must not achieve its goal by altering the Permanent/library source.

## Product goals

MDBListarr should:

- preserve useful upstream MDBList/Sonarr/Radarr integration;
- accurately represent partial as well as complete permanent media ownership;
- keep Permanent/library-source Arr instances read-only from reconciliation;
- control On-Demand monitoring decisions using validated source/target evidence;
- optionally initiate precise searches for newly eligible missing media;
- persist search state so uncertain external side effects can be recovered safely;
- remove confirmed redundant duplicate files from the On-Demand target when cleanup is explicitly enabled;
- expose useful, bounded operational health without probing external services from the health page;
- fail closed when required evidence is malformed, conflicting, unavailable, or uncertain;
- remain easy to deploy without requiring unnecessary manual secret generation;
- remain broadly useful outside any one operator's surrounding infrastructure.

## Product non-goals

MDBListarr is not intended to:

- support Arr products beyond Sonarr and Radarr;
- replace Sonarr or Radarr as media managers;
- become a general download client;
- become a filesystem manager;
- directly manage a download client's storage;
- become a general retention, pruning, or housekeeping engine;
- delete media for reasons other than resolving confirmed On-Demand duplication unless a future explicit product decision changes that boundary;
- depend on a particular downstream media server;
- depend on NzbDAV, SABnzbd, or any other particular download-side component;
- invent or maintain a speculative feature roadmap.

The application is currently expected to do its defined job well. New work should be driven by concrete needs, defects, useful upstream improvements, or clearly justified enhancements.

## Relationship with download-side components

NzbDAV should be treated broadly like SABnzbd or another external download-side component from MDBListarr's point of view.

MDBListarr may coexist with these components, but should not:

- require one specific implementation;
- manipulate their filesystems directly;
- rely on private storage paths;
- use them as the authority for Arr monitoring, search, or cleanup decisions when the relevant Arr API provides the intended boundary.

This keeps MDBListarr portable across different operator stacks.

## Traditional MDBList workflow and On-Demand workflow

The original MDBList workflow and the On-Demand reconciliation workflow are related but independent.

Traditional functionality includes:

- uploading library state to MDBList;
- synchronising matching MDBList collection/list state when enabled;
- optionally processing MDBList queue items into Sonarr or Radarr.

On-Demand reconciliation should not depend on MDBList queue processing being enabled.

Likewise, assigning an Arr instance an On-Demand role must not implicitly authorise MDBList queue imports.

Actual add operations therefore require explicit queue-processing permission and destination-instance permission. Settings such as quality profile and root folder are required when an operation will add media, but should not be mandatory merely to use an Arr instance as read-only library evidence or for reconciliation.

## Core product principles

### Permanent means read-only from reconciliation

The Permanent/library source exists to answer questions about retained media.

Reconciliation must not write monitoring changes, searches, cleanup, or other state back to that source.

This boundary is fundamental.

### Partial ownership must be represented accurately

For television, the presence of a series or one episode file is not enough to establish that a series is complete.

Relevant episode state is the basis for completeness.

This is what allows a partially retained series to remain eligible for On-Demand handling where appropriate.

### Search is a lifecycle, not a fire-and-forget request

Submitting an Arr search command is not proof of acquisition.

Search intent, submission state, observed command state, retry lineage, and independent evidence are distinct concepts.

Persistent lifecycle state exists to prevent duplicate submissions and to survive crashes, restarts, ambiguous responses, and disappearing command history.

### Persist intent before uncertain external side effects

Where an external action could succeed but the local process might fail to observe or persist the response, record enough local intent first to make later reconciliation safe.

This principle is especially important for search submission but may apply to future stateful external operations.

### Fail closed on uncertainty

When trustworthy evidence is required for a consequential action, missing or ambiguous evidence is a reason to stop, defer, or report, not a reason to guess.

Typical fail-closed situations include:

- malformed Arr resources;
- invalid identity values;
- uncertain command state;
- malformed air dates;
- incomplete file associations;
- conflicting edition evidence;
- changed cleanup evidence;
- unavailable external APIs.

### Destructive operations deserve stronger proof

Cleanup is more conservative than ordinary reconciliation.

Eligibility must persist through a grace period, exact file identity must remain stable, and evidence must be revalidated immediately before deletion.

If destructive verification becomes uncertain, stopping later destructive work in that run is preferable to optimistic continuation.

### Observability must not control the system

Arr Health is an observer.

Rendering the health view should rely on persisted local state rather than making new MDBList/Sonarr/Radarr calls, and health-recording failures should not alter the result of core reconciliation.

### Parity should not erase product semantics

Sonarr and Radarr should share terminology, operator experience, lifecycle concepts, and implementation primitives where that reduces unnecessary drift.

They should not be forced through abstractions that erase real differences:

- Sonarr has series, seasons, episodes, air dates, and multi-episode files;
- Radarr has movie availability, movie-file identity, and edition semantics.

Parity means equivalent intent and safety, not identical algorithms.

### Operational output should be bounded and safe

Large backlogs must not produce unbounded UI or log output.

Operational details should favour:

- accurate aggregate counts;
- deterministic ordering;
- bounded detail lists;
- sanitised titles and labels;
- useful stable identifiers;
- omission of secrets and unnecessary raw errors.

### Scheduling should represent due work

A legitimate reconciliation interval should not disappear merely because a scheduler starts slightly late.

At the same time, one due interval should not be serviced repeatedly.

Scheduling should therefore reason about due slots rather than naïvely relying on the exact current minute.

## Scope: Sonarr

Sonarr is in scope for:

- Permanent/library-source evidence;
- On-Demand monitoring reconciliation;
- season and top-level series monitoring derived from episode intent;
- explicit eligible-episode search lifecycle;
- confirmed duplicate-file cleanup;
- operational health.

Episode-level reasoning is essential to the product model.

## Scope: Radarr

Radarr is in scope for:

- Permanent/library-source evidence;
- On-Demand monitoring reconciliation;
- explicit eligible-movie search lifecycle;
- confirmed duplicate-file cleanup;
- operational health.

Radarr should provide operational parity with Sonarr while preserving Radarr-specific movie and edition semantics.

## Security as a product concern

Security hardening is part of the fork's design, not incidental housekeeping.

The expected baseline includes:

- authenticated administration;
- encrypted storage of external-service credentials;
- persistent cryptographic secrets;
- safe first-run administrator creation;
- cautious reverse-proxy trust;
- sanitised logs and diagnostics;
- public-repository hygiene.

Application-level encryption complements, rather than replaces, HTTPS, host security, access controls, limited external API permissions, and protected backups.

## Ease of deployment

Security should not require unnecessary setup ceremony.

A fresh container should be able to generate and persist its required runtime secrets in application data, then allow the first administrator to be claimed safely.

Operators who need explicit injected secrets should still be able to provide them.

## Upstream relationship

This repository remains a fork and should continue to benefit from useful general improvements made in `linaspurinis/mdblistarr`.

Upstream changes should be reviewed periodically and incorporated when useful and compatible. [Releases](RELEASES.md#upstream-maintenance) owns the practical integration process and release policy.

Upstream compatibility does not override deliberate fork behaviour. In particular, syncing upstream must preserve established:

- authentication and secret handling;
- Permanent/On-Demand separation;
- source read-only guarantees;
- fail-closed lifecycle semantics;
- destructive-operation safeguards.

## No standing roadmap

There is no long-term feature roadmap.

That is deliberate.

Future work may arise from:

- defects discovered in real operation;
- new edge cases;
- useful general improvements from upstream;
- maintenance or dependency changes;
- justified operator improvements.

Do not convert speculative possibilities into commitments or create architecture solely to prepare for hypothetical future products.

## Canonical terminology

| Term | Meaning |
| --- | --- |
| **Permanent / library source** | Arr instance providing evidence of permanently retained media. Reconciliation reads from it and does not write to it. |
| **On-Demand target** | Arr instance where MDBListarr may apply controlled monitoring, search, and validated duplicate cleanup. |
| **Reconciliation** | Comparing validated source/target state and bringing permitted target state towards the desired condition. |
| **Eligible / wanted** | An item that current validated evidence says should be monitored/acquired on the target. |
| **Search candidate** | Persisted representation of an eligible search need and its lifecycle. |
| **Search command** | Persisted representation of an Arr search command and its observed state. |
| **Cleanup candidate** | Persisted exact target-file candidate being evaluated through grace and revalidation. |
| **Fail closed** | Refuse a consequential operation when required evidence is missing, malformed, conflicting, or uncertain. |
| **Permanent duplicate** | Target media file for which required evidence proves an appropriate permanent copy exists. |
| **Arr Health** | Read-only operational view derived from persisted reconciliation and lifecycle information. |
