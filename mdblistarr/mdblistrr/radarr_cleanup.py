"""Safety-first Radarr On Demand duplicate MovieFile lifecycle."""
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from django.utils import timezone
from .connect import sanitize_text
from .models import RadarrCleanupCandidate, RadarrMovieSearchCandidate, RadarrMovieSearchCommand

REASON_PERMANENT_DUPLICATE = 'permanent_duplicate'

@dataclass(frozen=True)
class CleanupEvidence:
    tmdb_id: int; source_movie_id: int; source_movie_file_id: int
    target_movie_id: int; movie_file_id: int; source_edition: str; target_edition: str
    reason: str = REASON_PERMANENT_DUPLICATE

@dataclass
class RadarrCleanupCounters:
    cleanup_candidates_new: int = 0; cleanup_candidates_pending: int = 0
    cleanup_candidates_ready: int = 0; cleanup_candidates_cancelled: int = 0
    cleanup_would_delete: int = 0; cleanup_files_deleted: int = 0
    cleanup_files_already_absent: int = 0; cleanup_deferred_by_limit: int = 0
    cleanup_edition_conflicts: int = 0; cleanup_safety_deferred: int = 0
    cleanup_failures: int = 0; delete_attempts_consumed: int = 0
    stop_deletes_for_run: bool = False; events: list = field(default_factory=list)
    def json_counters(self):
        data = asdict(self); data.pop('events'); return data

def positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

def normalize_edition(value):
    if value is None: return ''
    if not isinstance(value, str): return None
    return ' '.join(value.split()).casefold()

def editions_compatible(source, target):
    left, right = normalize_edition(source), normalize_edition(target)
    if left is None or right is None: return 'malformed', left, right
    return ('compatible' if left == right else 'conflict'), left, right

def _bad_api(resource):
    if not isinstance(resource, dict): return True
    if any(resource.get(k) for k in ('error','errorMessage','result')): return True
    status = resource.get('status_code')
    return status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300)

def validate_movie_for_cleanup(movie, *, target=False, expected_movie_id=None, expected_tmdb_id=None, require_nested=False):
    if _bad_api(movie): return False, 'invalid_resource'
    movie_id, tmdb = positive_int(movie.get('id')), positive_int(movie.get('tmdbId'))
    if movie_id is None or tmdb is None: return False, 'invalid_identity'
    if expected_movie_id is not None and movie_id != expected_movie_id: return False, 'movie_id_mismatch'
    if expected_tmdb_id is not None and tmdb != expected_tmdb_id: return False, 'tmdb_id_mismatch'
    if not isinstance(movie.get('hasFile'), bool): return False, 'invalid_has_file'
    if target and not isinstance(movie.get('monitored'), bool): return False, 'invalid_monitored'
    if movie['hasFile']:
        file_id = positive_int(movie.get('movieFileId'))
        if file_id is None: return False, 'invalid_movie_file_id'
        nested = movie.get('movieFile')
        if require_nested or nested is not None:
            ok, reason = validate_movie_file(nested, expected_file_id=file_id, expected_movie_id=movie_id)
            if not ok: return False, f'nested_{reason}'
    return True, ''

def validate_movie_file(resource, *, expected_file_id=None, expected_movie_id=None):
    if _bad_api(resource): return False, 'invalid_resource'
    file_id, movie_id = positive_int(resource.get('id')), positive_int(resource.get('movieId'))
    if file_id is None or movie_id is None: return False, 'invalid_identity'
    if expected_file_id is not None and file_id != expected_file_id: return False, 'file_id_mismatch'
    if expected_movie_id is not None and movie_id != expected_movie_id: return False, 'movie_id_mismatch'
    if normalize_edition(resource.get('edition')) is None: return False, 'malformed_edition'
    return True, ''

def derive_cleanup_evidence(source, target):
    # Non-duplicates are ordinary monitoring evidence, not malformed destructive
    # evidence; only inspect nested MovieFileResource data when both have files.
    if not isinstance(source, dict) or not isinstance(target, dict):
        return 'unsafe', None, 'invalid_resource'
    if source.get('tmdbId') != target.get('tmdbId') or source.get('hasFile') is not True or target.get('hasFile') is not True:
        return 'ineligible', None, 'not_duplicate'
    ok, reason = validate_movie_for_cleanup(source, require_nested=True)
    if not ok: return 'unsafe', None, reason
    ok, reason = validate_movie_for_cleanup(target, target=True, require_nested=True)
    if not ok: return 'unsafe', None, reason
    state, se, te = editions_compatible(source['movieFile'].get('edition'), target['movieFile'].get('edition'))
    if state != 'compatible': return state, None, 'edition_' + state
    return 'eligible', CleanupEvidence(source_movie_id=source['id'], source_movie_file_id=source['movieFileId'],
        target_movie_id=target['id'], movie_file_id=target['movieFileId'], tmdb_id=target['tmdbId'],
        source_edition=se, target_edition=te), ''

BLOCKING_COMMAND_STATUSES = {'submitting','queued','started','ambiguous','unavailable'}
def search_lifecycle_unsafe(target_instance, movie_id):
    candidate = RadarrMovieSearchCandidate.objects.filter(target_instance=target_instance, target_movie_id=movie_id).select_related('current_command').first()
    if not candidate or not candidate.current_command: return False
    command = candidate.current_command
    if command.status in BLOCKING_COMMAND_STATUSES: return True
    return command.status in {'failed','aborted','cancelled','orphaned'} and command.outcome_reconciled_at is None

def _absent(response): return isinstance(response, dict) and response.get('status_code') == 404

def _event(kind, cand, title='', reason=''):
    title = ' '.join(sanitize_text(title or 'Unknown movie').split()).replace('"', "'")[:200]
    text = f'Radarr cleanup {kind} title="{title}" tmdb={cand.tmdb_id} target_movie={cand.target_movie_id} movieFileId={cand.movie_file_id}'
    return text + (f' reason={sanitize_text(reason)}' if reason else '')

def _terminal(cand, status, now):
    cand.status=status; cand.ready_at=None
    cand.cancelled_at=now if status == cand.STATUS_CANCELLED else None
    cand.deleted_at=now if status == cand.STATUS_DELETED else None
    cand.last_error=''; cand.save()

def _destructive_uncertainty(counters, cand, reason):
    cand.last_error = sanitize_text(reason)
    cand.save(update_fields=['last_error', 'updated_at'])
    counters.cleanup_failures += 1
    counters.cleanup_safety_deferred += 1
    counters.stop_deletes_for_run = True
    counters.events.append(_event('failure', cand, reason=reason))

def _retire_inactive_exact_file(cand, target_api, counters, now, title=''):
    """Resolve an old immutable file identity after the movie references another file."""
    exact = target_api.get_movie_file(cand.movie_file_id)
    if _absent(exact):
        _terminal(cand, cand.STATUS_ALREADY_ABSENT, now)
        counters.cleanup_files_already_absent += 1
        counters.events.append(_event('already_absent', cand, title))
        return 'absent'
    ok, _ = validate_movie_file(
        exact, expected_file_id=cand.movie_file_id, expected_movie_id=cand.target_movie_id)
    if ok:
        # The old file still exists, but is no longer the movie's active file.  It
        # must never be treated as the replacement or remain destructively ready.
        _terminal(cand, cand.STATUS_CANCELLED, now)
        counters.cleanup_candidates_cancelled += 1
        counters.events.append(_event('cancelled', cand, title, 'target_file_no_longer_active'))
        return 'cancelled'
    cand.last_error = sanitize_text('cleanup lifecycle uncertain: exact old target file state invalid')
    cand.save(update_fields=['last_error', 'updated_at'])
    counters.cleanup_failures += 1
    counters.cleanup_safety_deferred += 1
    return 'uncertain'

def process_radarr_cleanup(*, target_instance, source_movies, target_movies, confirmed_unmonitored_ids,
        monitoring_blocked_ids, source_api, target_api, cleanup_enabled=False, dry_run=True,
        grace_hours=24, max_deletions=25, stop_real_deletes=False):
    now=timezone.now(); counters=RadarrCleanupCounters(stop_deletes_for_run=bool(stop_real_deletes))
    source_by_tmdb={m.get('tmdbId'):m for m in source_movies if isinstance(m,dict)}
    target_by_id={m.get('id'):m for m in target_movies if isinstance(m,dict)}
    eligible_ids=set(); uncertain_ids=set(); lifecycle_blocked_candidate_ids=set()
    for target in sorted(target_movies, key=lambda m:m.get('id',0)):
        source=source_by_tmdb.get(target.get('tmdbId'))
        if source is None: continue
        state,evidence,reason=derive_cleanup_evidence(source,target)
        if state == 'conflict':
            counters.cleanup_edition_conflicts += 1
            for cand in RadarrCleanupCandidate.objects.filter(target_instance=target_instance,target_movie_id=target.get('id')).exclude(status__in=('deleted','already_absent','cancelled')):
                _terminal(cand,cand.STATUS_CANCELLED,now); counters.cleanup_candidates_cancelled+=1; counters.events.append(_event('cancelled',cand,target.get('title'), 'edition_conflict'))
            continue
        if state == 'unsafe':
            counters.cleanup_failures+=1; counters.cleanup_safety_deferred+=1; uncertain_ids.add(target.get('id')); continue
        if state != 'eligible': continue
        eligible_ids.add(evidence.target_movie_id)
        if evidence.target_movie_id not in confirmed_unmonitored_ids or evidence.target_movie_id in monitoring_blocked_ids or search_lifecycle_unsafe(target_instance,evidence.target_movie_id):
            counters.cleanup_safety_deferred+=1; continue
        cand,created=RadarrCleanupCandidate.objects.get_or_create(target_instance=target_instance,movie_file_id=evidence.movie_file_id,
            defaults={**evidence.__dict__,'status':'pending','first_eligible_at':now,'last_confirmed_at':now})
        changed=not created and any(getattr(cand,k)!=getattr(evidence,k) for k in ('tmdb_id','source_movie_id','source_movie_file_id','target_movie_id','source_edition','target_edition'))
        if created: counters.cleanup_candidates_new+=1; counters.events.append(_event('candidate created',cand,target.get('title'),REASON_PERMANENT_DUPLICATE))
        if changed or cand.status in ('cancelled','deleted','already_absent'):
            cand.first_eligible_at=now; cand.status='pending'; cand.ready_at=cand.deleted_at=cand.cancelled_at=None
        for k,v in evidence.__dict__.items(): setattr(cand,k,v)
        cand.last_confirmed_at=now; cand.last_error=''
        if now >= cand.first_eligible_at+timedelta(hours=int(grace_hours)):
            if cand.status!='ready': cand.status='ready'; cand.ready_at=now; counters.events.append(_event('candidate ready',cand,target.get('title')))
            counters.cleanup_candidates_ready+=1
        else: counters.cleanup_candidates_pending+=1
        cand.save()
    # Resolve candidates by immutable target file identity.  A replacement gets
    # its own candidate above and can never inherit the old file's grace clock.
    for cand in RadarrCleanupCandidate.objects.filter(target_instance=target_instance).exclude(status__in=('deleted','already_absent','cancelled')):
        current = target_by_id.get(cand.target_movie_id)
        if current is None:
            # A missing movie record is not proof that its immutable file is
            # absent. Resolve the exact file identity before terminalising.
            state = _retire_inactive_exact_file(cand, target_api, counters, now)
            if state == 'uncertain':
                lifecycle_blocked_candidate_ids.add(cand.id)
                if cleanup_enabled and not dry_run:
                    counters.stop_deletes_for_run = True
            continue
        current_file_id = positive_int(current.get('movieFileId')) if current.get('hasFile') is True else None
        if current_file_id != cand.movie_file_id:
            if _retire_inactive_exact_file(cand, target_api, counters, now, current.get('title')) == 'uncertain':
                lifecycle_blocked_candidate_ids.add(cand.id)
                if cleanup_enabled and not dry_run:
                    counters.stop_deletes_for_run = True
            continue
        if cand.target_movie_id not in eligible_ids and cand.target_movie_id not in uncertain_ids:
            _terminal(cand,cand.STATUS_CANCELLED,now); counters.cleanup_candidates_cancelled+=1
    ready=list(RadarrCleanupCandidate.objects.filter(target_instance=target_instance,status='ready').order_by('ready_at','id'))
    if not cleanup_enabled: return counters
    if dry_run:
        for cand in ready:
            if cand.id not in lifecycle_blocked_candidate_ids and cand.target_movie_id in confirmed_unmonitored_ids and not search_lifecycle_unsafe(target_instance,cand.target_movie_id):
                counters.cleanup_would_delete+=1
        return counters
    for index,cand in enumerate(ready):
        if counters.stop_deletes_for_run:
            counters.cleanup_safety_deferred += 1
            continue
        if cand.id in lifecycle_blocked_candidate_ids:
            counters.cleanup_safety_deferred += 1
            continue
        if counters.delete_attempts_consumed >= max_deletions:
            counters.cleanup_deferred_by_limit += 1
            continue
        if cand.target_movie_id not in confirmed_unmonitored_ids or cand.target_movie_id in monitoring_blocked_ids or search_lifecycle_unsafe(target_instance,cand.target_movie_id):
            counters.cleanup_safety_deferred+=1; continue
        sm=source_api.get_movie(cand.source_movie_id)
        tm=target_api.get_movie(cand.target_movie_id)
        if _absent(sm):
            _terminal(cand,cand.STATUS_CANCELLED,timezone.now()); counters.cleanup_candidates_cancelled+=1; continue
        if _absent(tm):
            tf=target_api.get_movie_file(cand.movie_file_id)
            if _absent(tf):
                _terminal(cand,cand.STATUS_ALREADY_ABSENT,timezone.now()); counters.cleanup_files_already_absent+=1
            else:
                _destructive_uncertainty(counters,cand,'revalidation uncertain: target movie absent but exact file absence unproven')
            continue
        ok_s,rs=validate_movie_for_cleanup(sm,expected_movie_id=cand.source_movie_id,expected_tmdb_id=cand.tmdb_id)
        ok_t,rt=validate_movie_for_cleanup(tm,target=True,expected_movie_id=cand.target_movie_id,expected_tmdb_id=cand.tmdb_id)
        if not ok_s or not ok_t:
            _destructive_uncertainty(counters,cand,f'revalidation uncertain: {rs or rt}'); continue
        if not sm.get('hasFile'):
            _terminal(cand,cand.STATUS_CANCELLED,timezone.now()); counters.cleanup_candidates_cancelled+=1; continue
        tf=target_api.get_movie_file(cand.movie_file_id)
        if _absent(tf):
            _terminal(cand,cand.STATUS_ALREADY_ABSENT,timezone.now()); counters.cleanup_files_already_absent+=1; continue
        if not tm.get('hasFile') or tm.get('movieFileId')!=cand.movie_file_id:
            ok_tf,_=validate_movie_file(tf,expected_file_id=cand.movie_file_id,expected_movie_id=cand.target_movie_id)
            if ok_tf:
                _terminal(cand,cand.STATUS_CANCELLED,timezone.now()); counters.cleanup_candidates_cancelled+=1
            else:
                _destructive_uncertainty(counters,cand,'revalidation uncertain: target replacement state invalid')
            continue
        if tm.get('monitored') is not False:
            counters.cleanup_safety_deferred+=1; continue
        new_sf=sm['movieFileId']; sf=source_api.get_movie_file(new_sf)
        ok_sf,_=validate_movie_file(sf,expected_file_id=new_sf,expected_movie_id=cand.source_movie_id)
        ok_tf,_=validate_movie_file(tf,expected_file_id=cand.movie_file_id,expected_movie_id=cand.target_movie_id)
        if not ok_sf or not ok_tf:
            _destructive_uncertainty(counters,cand,'revalidation uncertain: malformed movie file'); continue
        estate,se,te=editions_compatible(sf.get('edition'),tf.get('edition'))
        if estate=='conflict': _terminal(cand,cand.STATUS_CANCELLED,timezone.now()); counters.cleanup_edition_conflicts+=1; counters.cleanup_candidates_cancelled+=1; continue
        if estate=='malformed':
            _destructive_uncertainty(counters,cand,'revalidation uncertain: malformed edition'); continue
        if new_sf!=cand.source_movie_file_id or se!=cand.source_edition or te!=cand.target_edition:
            cand.source_movie_file_id=new_sf; cand.source_edition=se; cand.target_edition=te; cand.first_eligible_at=timezone.now(); cand.status='pending'; cand.ready_at=None; cand.save(); counters.cleanup_candidates_pending+=1; continue
        if timezone.now() < cand.first_eligible_at+timedelta(hours=int(grace_hours)) or cand.reason!=REASON_PERMANENT_DUPLICATE: counters.cleanup_safety_deferred+=1; continue
        counters.delete_attempts_consumed+=1; response=target_api.delete_movie_file(cand.movie_file_id)
        verify=target_api.get_movie_file(cand.movie_file_id); absent=_absent(verify)
        failed=isinstance(response,dict) and bool(response.get('error') or response.get('errorMessage'))
        if absent:
            status=cand.STATUS_ALREADY_ABSENT if failed else cand.STATUS_DELETED; _terminal(cand,status,timezone.now())
            if failed: counters.cleanup_files_already_absent+=1; counters.events.append(_event('already_absent',cand))
            else: counters.cleanup_files_deleted+=1; counters.events.append(_event('deleted',cand))
        else:
            cand.last_error='post-delete verification could not prove exact file absent'; cand.save(); counters.cleanup_failures+=1; counters.stop_deletes_for_run=True
            counters.cleanup_safety_deferred += 1 + len(ready)-index-1; counters.events.append(_event('failure',cand,reason='post_delete_uncertain')); break
    return counters
