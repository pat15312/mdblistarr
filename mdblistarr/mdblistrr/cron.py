from django.utils import timezone
from django.tasks import task
from django_scheduled_tasks import cron_task
from .connect import sanitize_text, Connect
import time
import json
import random
import traceback
from .models import Log, InstanceChangeLog, RadarrInstance, SonarrInstance, Preferences
from .services import get_mdblistarr, reset_mdblistarr
from .arr import SonarrAPI
from .arr import RadarrAPI
from .sonarr_reconcile import determine_series_completeness, calculate_episode_monitoring
from .sonarr_cleanup import process_cleanup_for_series
from .sonarr_search import update_search_candidates_for_series, submit_pending_search_candidates
import fcntl, os

def save_log(provider, status, text):
    log = Log()
    log.date = timezone.now()
    log.provider = provider
    log.status = status
    log.text = text
    log.save()

def get_sync_instance_scope():
    pref = Preferences.objects.filter(name='sync_instance_scope').first()
    return pref.value if pref and pref.value == 'all' else 'first'

def get_radarr_sync_instances():
    instances = RadarrInstance.objects.order_by('id')
    if get_sync_instance_scope() == 'all':
        return list(instances)
    first_instance = instances.first()
    return [first_instance] if first_instance else []

def get_sonarr_sync_instances():
    instances = SonarrInstance.objects.filter(is_library_source=True).order_by('id')
    if get_sync_instance_scope() == 'all':
        return list(instances)
    first_instance = instances.first()
    return [first_instance] if first_instance else []

def merge_arr_record(records, key_name, item_id, exists=None, date_added=None, excluded=False):
    rec = records.get(item_id, {key_name: item_id})
    if excluded:
        rec['excluded'] = True
    if exists is True:
        rec['exists'] = True
        if date_added and not rec.get('date_added'):
            rec['date_added'] = date_added
    elif exists is False and rec.get('exists') is not True:
        rec['exists'] = False
    records[item_id] = rec


def arr_api_failed(response):
    if response is None:
        return True
    if isinstance(response, dict):
        status_code = response.get('status_code')
        if status_code is not None:
            try:
                if int(status_code) < 200 or int(status_code) >= 300:
                    return True
            except (TypeError, ValueError):
                return True
        if response.get('error') or response.get('errorMessage'):
            return True
    return False

def post_radarr_payload(force=False):
    provider = 1  # Radarr JSON POST
    try:
        pref = Preferences.objects.filter(name='sync_hour').first()
        if pref is None:
            random_hour = str(random.randint(0, 23))
            pref, _ = Preferences.objects.update_or_create(name='sync_hour', defaults={'value': random_hour})
        sync_hour = int(pref.value)
        if not force and timezone.now().hour != sync_hour:
            return {"response": "Not scheduled hour"}
        if not force:
            time.sleep(random.uniform(0.0, 3600.0))
        reset_mdblistarr()
        mdblistarr = get_mdblistarr()
        if mdblistarr.mdblist is None:
            save_log(provider, 2, "MDBList API key not configured")
            return {"response": "Missing API key"}
        radarr_instances = get_radarr_sync_instances()
        if not radarr_instances:
            save_log(provider, 2, "No Radarr instances configured")
            return {"response": "No Radarr instances configured"}
        records_by_tmdb = {}

        for instance in radarr_instances:
            radarr_api = RadarrAPI(instance_id=instance.id)
            movies = radarr_api.get_movies()
            exclusions = radarr_api.get_exclusions()

            # Avoid sending a partial sync when Radarr is unreachable (can accidentally wipe state server-side).
            if isinstance(movies, dict) and movies.get('error'):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /movie request failed: {movies}")
                return {"response": "RadarrError"}
            if isinstance(movies, list) and len(movies) == 1 and isinstance(movies[0], dict) and movies[0].get('result'):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /movie request failed: {movies[0].get('result')}")
                return {"response": "RadarrError"}
            if not isinstance(movies, list):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /movie unexpected response type={type(movies)} payload={str(movies)[:500]}")
                return {"response": "RadarrError"}

            # Library state (downloaded/missing).
            for movie in movies:
                if not isinstance(movie, dict):
                    continue
                tmdb_id = movie.get('tmdbId')
                if not tmdb_id:
                    continue

                has_file = movie.get('hasFile')
                date_added = None
                if isinstance(movie.get('movieFile'), dict):
                    date_added = movie['movieFile'].get('dateAdded')
                if not date_added:
                    date_added = movie.get('added')
                if has_file is True:
                    merge_arr_record(records_by_tmdb, 'tmdb', tmdb_id, exists=True, date_added=date_added)
                elif has_file is False:
                    merge_arr_record(records_by_tmdb, 'tmdb', tmdb_id, exists=False)
                else:
                    # Fallback: older/odd responses. Treat presence in Radarr as "exists".
                    merge_arr_record(records_by_tmdb, 'tmdb', tmdb_id, exists=True, date_added=date_added)

            # Import List Exclusions -> mark excluded. Include excluded even if not in library.
            if isinstance(exclusions, dict) and exclusions.get('error'):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /exclusions request failed: {exclusions}")
                exclusions = []
            elif isinstance(exclusions, list) and len(exclusions) == 1 and isinstance(exclusions[0], dict) and exclusions[0].get('result'):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /exclusions request failed: {exclusions[0].get('result')}")
                exclusions = []
            elif not isinstance(exclusions, list):
                save_log(provider, 2, f"{radarr_api.name}: Radarr /exclusions unexpected response type={type(exclusions)} payload={str(exclusions)[:500]}")
                exclusions = []

            for ex in exclusions if isinstance(exclusions, list) else []:
                if not isinstance(ex, dict):
                    continue
                tmdb_id = ex.get('tmdbId') or ex.get('tmdbid')
                if not tmdb_id:
                    continue
                merge_arr_record(records_by_tmdb, 'tmdb', tmdb_id, excluded=True)

        records = list(records_by_tmdb.values())
        total_records = len(records)
        excluded_count = sum(1 for rec in records if rec.get('excluded'))
        json_payload = {'radarr': records}

        res = mdblistarr.mdblist.post_arr_payload(json_payload)

        if res.get('response') == 'Ok':
            save_log(provider, 1, f'Radarr: Uploaded {total_records} records from {len(radarr_instances)} instance(s) to MDBList.com (excluded={excluded_count})')
        else:
            save_log(provider, 2, f'Upload records to MDBList.com Failed: {res}')
            return res

        sync_library_pref = Preferences.objects.filter(name='sync_library_status').first()
        if sync_library_pref and sync_library_pref.value == '1':
            collection_add = [
                {k: v for k, v in {'ids': {'tmdb': rec['tmdb']}, 'collected_at': rec.get('date_added')}.items() if v}
                for rec in records if rec.get('exists')
            ]
            collection_remove = [{'ids': {'tmdb': rec['tmdb']}} for rec in records if rec.get('exists') is False]

            chunk_size = 250
            total_added = 0
            for i in range(0, len(collection_add), chunk_size):
                chunk = collection_add[i:i + chunk_size]
                add_res = mdblistarr.mdblist.post_collection({'movies': chunk})
                if isinstance(add_res, dict) and add_res.get('error'):
                    save_log(provider, 2, f'Radarr: Collection add failed: {add_res}')
                    break
                total_added += add_res.get('updated', {}).get('movies', 0) if isinstance(add_res, dict) else 0
            if total_added:
                save_log(provider, 1, f'Radarr: Synced {total_added} movies to MDBList collection')

            total_removed = 0
            for i in range(0, len(collection_remove), chunk_size):
                chunk = collection_remove[i:i + chunk_size]
                rm_res = mdblistarr.mdblist.post_collection_remove({'movies': chunk})
                if isinstance(rm_res, dict) and rm_res.get('error'):
                    save_log(provider, 2, f'Radarr: Collection remove failed: {rm_res}')
                    break
                total_removed += rm_res.get('removed', {}).get('movies', 0) if isinstance(rm_res, dict) else 0
            if total_removed:
                save_log(provider, 1, f'Radarr: Removed {total_removed} movies from MDBList collection')

        return res
    except:
        save_log(provider, 2, sanitize_text(traceback.format_exc()))
        return {'response': 'Exception'}

@cron_task(cron_schedule="0 * * * *")
@task
def post_radarr_payload_task():
    return post_radarr_payload()

def post_sonarr_payload(force=False):
    provider = 2  # Sonarr JSON POST
    try:
        pref = Preferences.objects.filter(name='sync_hour').first()
        if pref is None:
            random_hour = str(random.randint(0, 23))
            pref, _ = Preferences.objects.update_or_create(name='sync_hour', defaults={'value': random_hour})
        sync_hour = int(pref.value)
        if not force and timezone.now().hour != sync_hour:
            return {"response": "Not scheduled hour"}
        if not force:
            time.sleep(random.uniform(0.0, 3600.0))
        reset_mdblistarr()
        mdblistarr = get_mdblistarr()
        if mdblistarr.mdblist is None:
            save_log(provider, 2, "MDBList API key not configured")
            return {"response": "Missing API key"}
        sonarr_instances = get_sonarr_sync_instances()
        if not sonarr_instances:
            save_log(provider, 2, "No Sonarr instances configured")
            return {"response": "No Sonarr instances configured"}
        records_by_tvdb = {}
        series_by_api = []

        for instance in sonarr_instances:
            sonarr_api = SonarrAPI(instance_id=instance.id)
            series = sonarr_api.get_series()
            exclusions = sonarr_api.get_import_list_exclusions()

            # Avoid sending a partial sync when Sonarr is unreachable (can accidentally wipe state server-side).
            if isinstance(series, dict) and series.get('error'):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /series request failed: {series}")
                return {"response": "SonarrError"}
            if isinstance(series, list) and len(series) == 1 and isinstance(series[0], dict) and series[0].get('result'):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /series request failed: {series[0].get('result')}")
                return {"response": "SonarrError"}
            if not isinstance(series, list):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /series unexpected response type={type(series)} payload={str(series)[:500]}")
                return {"response": "SonarrError"}

            series_by_api.append((sonarr_api, series))

            # Library state (downloaded/missing).
            for show in series:
                if not isinstance(show, dict):
                    continue
                tvdb_id = show.get('tvdbId')
                if not tvdb_id:
                    continue

                episodes = sonarr_api.get_episodes(show.get('id'))
                complete = determine_series_completeness(
                    episodes,
                    include_specials=Preferences.get_value('sonarr_include_specials', '0') == '1'
                )
                if complete.get('malformed'):
                    save_log(provider, 2, f"{sonarr_api.name}: Sonarr episodes malformed for tvdb={tvdb_id}")
                    return {"response": "SonarrError"}
                if complete['complete']:
                    merge_arr_record(records_by_tvdb, 'tvdb', tvdb_id, exists=True)
                else:
                    merge_arr_record(records_by_tvdb, 'tvdb', tvdb_id, exists=False)

            # Import List Exclusions -> mark excluded. Include excluded even if not in library.
            if isinstance(exclusions, dict) and exclusions.get('error'):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /importlistexclusion request failed: {exclusions}")
                exclusions = []
            elif isinstance(exclusions, list) and len(exclusions) == 1 and isinstance(exclusions[0], dict) and exclusions[0].get('result'):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /importlistexclusion request failed: {exclusions[0].get('result')}")
                exclusions = []
            elif not isinstance(exclusions, list):
                save_log(provider, 2, f"{sonarr_api.name}: Sonarr /importlistexclusion unexpected response type={type(exclusions)} payload={str(exclusions)[:500]}")
                exclusions = []

            for ex in exclusions if isinstance(exclusions, list) else []:
                if not isinstance(ex, dict):
                    continue
                tvdb_id = ex.get('tvdbId') or ex.get('tvdbid')
                if not tvdb_id:
                    continue
                merge_arr_record(records_by_tvdb, 'tvdb', tvdb_id, excluded=True)

        records = list(records_by_tvdb.values())
        total_records = len(records)
        excluded_count = sum(1 for rec in records if rec.get('excluded'))
        json_payload = {'sonarr': records}

        res = mdblistarr.mdblist.post_arr_payload(json_payload)
        if res.get('response') == 'Ok':
            save_log(provider, 1, f'Sonarr: Uploaded {total_records} records from {len(sonarr_instances)} instance(s) to MDBList.com (excluded={excluded_count})')
        else:
            save_log(provider, 2, f'Upload records to MDBList.com Failed: {res}')
            return res

        sync_library_pref = Preferences.objects.filter(name='sync_library_status').first()
        if sync_library_pref and sync_library_pref.value == '1':
            collection_add_by_tvdb = {}

            for sonarr_api, series in series_by_api:
                for show in series:
                    if not isinstance(show, dict):
                        continue
                    tvdb_id = show.get('tvdbId')
                    if not tvdb_id:
                        continue

                    sonarr_id = show.get('id')
                    if not sonarr_id:
                        continue

                    # Build episodeFileId -> dateAdded map.
                    ef_list = sonarr_api.get_episode_files(sonarr_id)
                    file_date_map = {}
                    if isinstance(ef_list, list):
                        for ef in ef_list:
                            if isinstance(ef, dict) and ef.get('id') and ef.get('dateAdded'):
                                file_date_map[ef['id']] = ef['dateAdded']

                    if not file_date_map:
                        continue

                    episodes = sonarr_api.get_episodes(sonarr_id)
                    if not isinstance(episodes, list):
                        continue

                    seasons_map = collection_add_by_tvdb.setdefault(tvdb_id, {})
                    for ep in episodes:
                        if not isinstance(ep, dict) or not ep.get('hasFile'):
                            continue
                        season_num = ep.get('seasonNumber')
                        ep_num = ep.get('episodeNumber')
                        ef_id = ep.get('episodeFileId')
                        if season_num is None or ep_num is None:
                            continue
                        ep_entry = {'number': ep_num}
                        date = file_date_map.get(ef_id)
                        if date:
                            ep_entry['collected_at'] = date
                        seasons_map.setdefault(season_num, {})[ep_num] = ep_entry

            collection_add = []
            for tvdb_id, seasons_map in collection_add_by_tvdb.items():
                seasons = []
                for season_num, episodes_map in sorted(seasons_map.items()):
                    seasons.append({
                        'number': season_num,
                        'episodes': [episodes_map[ep_num] for ep_num in sorted(episodes_map)]
                    })
                if seasons:
                    collection_add.append({'ids': {'tvdb': tvdb_id}, 'seasons': seasons})

            collection_remove = [
                {'ids': {'tvdb': rec['tvdb']}}
                for rec in records
                if rec.get('exists') is False and rec['tvdb'] not in collection_add_by_tvdb
            ]

            chunk_size = 250
            total_shows, total_seasons = 0, 0
            for i in range(0, len(collection_add), chunk_size):
                chunk = collection_add[i:i + chunk_size]
                add_res = mdblistarr.mdblist.post_collection({'shows': chunk})
                if isinstance(add_res, dict) and add_res.get('error'):
                    save_log(provider, 2, f'Sonarr: Collection add failed: {add_res}')
                    break
                updated = add_res.get('updated', {}) if isinstance(add_res, dict) else {}
                total_shows += updated.get('shows', 0)
                total_seasons += updated.get('seasons', 0)
            if total_shows or total_seasons:
                save_log(provider, 1, f'Sonarr: Synced collection (shows={total_shows} seasons={total_seasons} episodes={sum(len(s["episodes"]) for e in collection_add for s in e.get("seasons", []))})')

            total_removed = 0
            for i in range(0, len(collection_remove), chunk_size):
                chunk = collection_remove[i:i + chunk_size]
                rm_res = mdblistarr.mdblist.post_collection_remove({'shows': chunk})
                if isinstance(rm_res, dict) and rm_res.get('error'):
                    save_log(provider, 2, f'Sonarr: Collection remove failed: {rm_res}')
                    break
                total_removed += rm_res.get('removed', {}).get('shows', 0) if isinstance(rm_res, dict) else 0
            if total_removed:
                save_log(provider, 1, f'Sonarr: Removed {total_removed} shows from MDBList collection')

        return res
    except:
        save_log(provider, 2, sanitize_text(traceback.format_exc()))
        return {'response': 'Exception'}

@cron_task(cron_schedule="0 * * * *")
@task
def post_sonarr_payload_task():
    return post_sonarr_payload()

def get_mdblist_queue_to_arr():
    provider = 0  # Queue Sync
    try:
        time.sleep(random.uniform(0.0, 36.0))
        reset_mdblistarr()
        mdblistarr = get_mdblistarr()
        if Preferences.get_value('enable_mdblist_queue_processing', '0') != '1':
            return {'result': 200, 'message': 'MDBList queue processing disabled'}
        if mdblistarr.mdblist is None:
            save_log(provider, 2, "MDBList API key not configured")
            return {"result": 400, "error": "mdblist_apikey not configured"}

        queue_resp = mdblistarr.mdblist.get_mdblist_queue()

        # The MDBList queue endpoint usually returns a list of items, but in some
        # cases we can get a dict (error wrapper, rate limit, etc) or a JSON string.
        queue_items = None
        if isinstance(queue_resp, list):
            queue_items = queue_resp
        elif isinstance(queue_resp, dict):
            for key in ("queue", "results", "data", "items"):
                if isinstance(queue_resp.get(key), list):
                    queue_items = queue_resp[key]
                    break

            # If this is an error/traceback wrapper, stop early and log it.
            if queue_items is None:
                save_log(provider, 2, f"MDBList queue unexpected response (dict): {str(queue_resp)[:1000]}")
                return {"result": 502, "error": "unexpected_queue_response"}
        elif isinstance(queue_resp, str):
            try:
                decoded = json.loads(queue_resp)
            except Exception:
                save_log(provider, 2, f"MDBList queue unexpected response (str): {queue_resp[:500]}")
                return {"result": 502, "error": "unexpected_queue_response"}

            if isinstance(decoded, list):
                queue_items = decoded
            elif isinstance(decoded, dict):
                for key in ("queue", "results", "data", "items"):
                    if isinstance(decoded.get(key), list):
                        queue_items = decoded[key]
                        break
                if queue_items is None:
                    save_log(provider, 2, f"MDBList queue unexpected decoded response (dict): {str(decoded)[:1000]}")
                    return {"result": 502, "error": "unexpected_queue_response"}
            else:
                save_log(provider, 2, f"MDBList queue unexpected decoded response (type={type(decoded)}): {decoded}")
                return {"result": 502, "error": "unexpected_queue_response"}
        else:
            save_log(provider, 2, f"MDBList queue unexpected response type={type(queue_resp)}: {queue_resp}")
            return {"result": 502, "error": "unexpected_queue_response"}

        for item in queue_items:
            if not isinstance(item, dict):
                save_log(provider, 2, f"Skipping unexpected queue item type={type(item)}: {str(item)[:500]}")
                continue

            mediatype = item.get("mediatype")
            if mediatype is None:
                save_log(provider, 2, f"Skipping queue item missing 'mediatype': {item}")
                continue

            if mediatype == 'movie':
                provider = 1
                instance_id = item.get('instanceid')
                if not RadarrInstance.objects.filter(id=instance_id, enable_queue_import=True).exists():
                    save_log(provider, 2, f"Skipping Radarr queue item because instance is not queue-import enabled: {item.get('title')}")
                    continue
                movie_request_json = {
                    "title": item['title'],
                    "tmdbid": item['tmdbid'],
                    "monitored": True, 
                    "addOptions": {"searchForMovie": True},
                    "qualityProfileId": mdblistarr.get_radarr_quality_profile(instance_id),
                    "rootFolderPath": mdblistarr.get_radarr_root_folder(instance_id)
                }

                radarr_api = RadarrAPI(instance_id=instance_id)
                res = radarr_api.post_movie(movie_request_json)
                if isinstance(res, list):
                    if res[0].get('errorMessage'):
                        msg = res[0].get('errorMessage') or ""
                        # Treat "already added" as a non-error and trigger a search instead.
                        if 'already been added' in msg.lower():
                            search_res = radarr_api.trigger_movie_search(item.get('tmdbid'))
                            if isinstance(search_res, dict) and search_res.get("error") == "movie_not_found":
                                save_log(provider, 2, f"Movie already exists in Radarr but could not resolve by tmdbid for search: {item['title']} (tmdbid={item.get('tmdbid')}).")
                            elif isinstance(search_res, dict) and search_res.get("error"):
                                save_log(provider, 2, f"Movie already exists in Radarr; search trigger failed: {item['title']}. {search_res}")
                            else:
                                save_log(provider, 1, f"Movie already exists in Radarr; triggered search: {item['title']}.")
                        else:
                            save_log(provider, 2, f"Error adding movie to Radarr: {item['title']}. {msg}.")
                    else:
                        save_log(provider, 2, f"Error posting movie to Radarr: {item['title']}. Raw response: {res}")
                        print(f"Error posting movie to Radarr: {item['title']}. Raw response: {res}")  # Print to console
                        # save_log(provider, 2, f"Error posting movie to Radarr")
                elif res.get('title'):
                    save_log(provider, 1, f"Added movie to Radarr: {item['title']}.")
                elif res.get('errorMessage'):
                    msg = res.get('errorMessage') or ""
                    if 'already been added' in msg.lower():
                        search_res = radarr_api.trigger_movie_search(item.get('tmdbid'))
                        if isinstance(search_res, dict) and search_res.get("error") == "movie_not_found":
                            save_log(provider, 2, f"Movie already exists in Radarr but could not resolve by tmdbid for search: {item['title']} (tmdbid={item.get('tmdbid')}).")
                        elif isinstance(search_res, dict) and search_res.get("error"):
                            save_log(provider, 2, f"Movie already exists in Radarr; search trigger failed: {item['title']}. {search_res}")
                        else:
                            save_log(provider, 1, f"Movie already exists in Radarr; triggered search: {item['title']}.")
                    else:
                        save_log(provider, 2, f"Error posting movie to Radarr: {item['title']}. {msg}")
                else:
                    # Log the full response for debugging
                    save_log(provider, 2, f"Error posting movie to Radarr: {item['title']}. Raw response: {res}")
                    print(f"Error posting movie to Radarr: {item['title']}. Raw response: {res}")  # Print to console
                    # save_log(provider, 2, f"Error posting movie to Radarr")
            elif mediatype == 'show':
                provider = 2
                instance_id = item.get('instanceid')
                if not SonarrInstance.objects.filter(id=instance_id, enable_queue_import=True).exists():
                    save_log(provider, 2, f"Skipping Sonarr queue item because instance is not queue-import enabled: {item.get('title')}")
                    continue
                show_request_json = {
                    "title": item['title'],
                    "tvdbid": item['tvdbid'],
                    "monitored": True, 
                    "addOptions": {"searchForMissingEpisodes": True},
                    "qualityProfileId": mdblistarr.get_sonarr_quality_profile(instance_id),
                    "rootFolderPath": mdblistarr.get_sonarr_root_folder(instance_id)
                }

                sonarr_api = SonarrAPI(instance_id=instance_id)
                res = sonarr_api.post_show(show_request_json)
                if isinstance(res, list):
                    if res[0].get('errorMessage'):
                        save_log(provider, 2, f"Error adding show to Sonarr: {item['title']}. {res[0]['errorMessage']}")
                    else:
                        save_log(provider, 2, f"Error posting show to Sonarr: {item['title']}. Raw response: {res}")
                        print(f"Error posting show to Sonarr: {item['title']}. Raw response: {res}")  # Print to console
                        # save_log(provider, 2, f"Error posting show to Sonarr")
                elif res.get('title'):
                    save_log(provider, 1, f"Added show to Sonarr {item['title']}.")
                elif res.get('errorMessage'):
                    save_log(provider, 2, f"Error posting show to Sonarr: {item['title']}. {res['errorMessage']}")
                else:
                    # Log the full response for debugging
                    save_log(provider, 2, f"Error posting show to Sonarr: {item['title']}. Raw response: {res}")
                    print(f"Error posting show to Sonarr: {item['title']}. Raw response: {res}")  # Print to console
                    # save_log(provider, 2, f"Error posting show to Sonarr")
            else:
                save_log(provider, 2, f"Skipping queue item with unknown mediatype={mediatype}: {item}")
    except Exception:
        save_log(provider, 2, sanitize_text(traceback.format_exc()))
        return {'result': 500}
    
    return {'result': 200}

@cron_task(cron_schedule="*/5 * * * *")
@task
def get_mdblist_queue_to_arr_task():
    return get_mdblist_queue_to_arr()

def process_instance_changes():
    provider = 3  # Instance Change Log
    try:
        logs = InstanceChangeLog.objects.filter(processed=False).order_by('timestamp')

        if not logs.exists():
            return {"response": "Log is empty"}

        time.sleep(random.uniform(0.0, 36.0))

        radarr_instances = RadarrInstance.objects.all()
        sonarr_instances = SonarrInstance.objects.all()
        
        json_payload = {
            "instances": {
                "radarr": [
                    {
                        "instance_id": radarr.id,
                        "instance_name": radarr.name,
                    } for radarr in radarr_instances
                ],
                "sonarr": [
                    {
                        "instance_id": sonarr.id,
                        "instance_name": sonarr.name,
                    } for sonarr in sonarr_instances
                ]
            }
        }
        
        if not radarr_instances and not sonarr_instances:
            logs.update(processed=True)
            return {"response": "No instances to sync"}
            
        reset_mdblistarr()
        mdblistarr = get_mdblistarr()
        if mdblistarr.mdblist is None:
            save_log(provider, 2, "MDBList API key not configured")
            return {"response": "Missing API key"}
        res = mdblistarr.mdblist.post_arr_changes(json_payload)
        
        if res.get('response') == 'Ok':
            logs.update(processed=True)
            save_log(provider, 1, f'Configuration uploaded to MDBList.com')
            return res
        else:
            save_log(provider, 2, f'Configuration upload to MDBList.com Failed: {res}')
            return res
    except Exception as e:
        save_log(provider, 2, sanitize_text(traceback.format_exc()))
        return {'result': 500}

@cron_task(cron_schedule="*/15 * * * *")
@task
def process_instance_changes_task():
    return process_instance_changes()



def _positive_int_value(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def validate_sonarr_series_response(series, label):
    if not isinstance(series, list):
        return False, f'{label}_series_not_list'
    for index, item in enumerate(series):
        if not isinstance(item, dict):
            return False, f'{label}_series_item_{index}_not_dict'
        if item.get('result') or item.get('error') or item.get('errorMessage'):
            return False, f'{label}_series_item_{index}_api_error'
        status_code = item.get('status_code')
        if status_code is not None:
            try:
                if int(status_code) < 200 or int(status_code) >= 300:
                    return False, f'{label}_series_item_{index}_http_error'
            except (TypeError, ValueError):
                return False, f'{label}_series_item_{index}_bad_status'
        if _positive_int_value(item.get('id')) is None or _positive_int_value(item.get('tvdbId')) is None:
            return False, f'{label}_series_item_{index}_missing_identifiers'
    return True, ''

RECONCILE_LOCK_PATH = os.environ.get('MDBLISTARR_RECONCILE_LOCK_PATH', '/tmp/mdblistarr-sonarr-reconcile.lock')

def _apply_monitor_batches(target_api, ids, monitored, batch_size=100):
    applied = []
    failed = False
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        if not batch:
            continue
        res = target_api.put_episode_monitor(batch, monitored)
        if arr_api_failed(res):
            failed = True
            continue
        applied.extend(batch)
    return applied, failed


def _search_episode_batches(target_api, ids, batch_size=100):
    searched = 0
    failed = False
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        if not batch:
            continue
        res = target_api.trigger_episode_search(batch)
        if arr_api_failed(res):
            failed = True
            continue
        searched += len(batch)
    return searched, failed


def _season_number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _season_updates_for_series(show, desired_by_season):
    updates = []
    unchanged = 0
    seen = set()
    if not isinstance(show, dict) or 'seasons' not in show:
        return updates, unchanged, 1
    seasons = show.get('seasons')
    if not isinstance(seasons, list):
        return updates, unchanged, 1
    for season in seasons:
        if not isinstance(season, dict):
            return updates, unchanged, 1
        season_number = _season_number(season.get('seasonNumber'))
        if season_number is None or season_number < 0 or season_number in seen:
            return updates, unchanged, 1
        seen.add(season_number)
        if not isinstance(season.get('monitored'), bool):
            return updates, unchanged, 1
        desired = bool(desired_by_season.get(season_number, False))
        current = season.get('monitored') is True
        if current == desired:
            unchanged += 1
        else:
            updates.append((season_number, desired))
    return updates, unchanged, 0


def _apply_series_monitor_update(target_api, series_id, current, desired):
    if current == desired:
        return 0, 0, 1, False
    res = target_api.put_series_monitor([series_id], desired)
    if arr_api_failed(res):
        return 0, 0, 0, True
    if desired:
        return 1, 0, 0, False
    return 0, 1, 0, False


def _apply_season_updates(target_api, series_id, updates):
    if not updates:
        return 0, 0, 0
    res = target_api.post_seasonpass(series_id, updates)
    if arr_api_failed(res):
        return 0, 0, 1
    newly_monitored = sum(1 for _season_number, monitored in updates if monitored)
    newly_unmonitored = len(updates) - newly_monitored
    return newly_monitored, newly_unmonitored, 0

def reconcile_sonarr_ondemand(force=False):
    provider = 2
    if Preferences.get_value('sonarr_reconciliation_enabled', '0') != '1':
        return {'result': 200, 'message': 'Sonarr reconciliation disabled'}
    interval = int(Preferences.get_value('sonarr_reconciliation_interval_minutes', '15') or '15')
    if not force and timezone.now().minute % interval != 0:
        return {'result': 200, 'message': 'Not scheduled interval'}
    os.makedirs(os.path.dirname(RECONCILE_LOCK_PATH), exist_ok=True)
    with open(RECONCILE_LOCK_PATH, 'a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'result': 200, 'message': 'Reconciliation already running'}
        try:
            source = SonarrInstance.objects.get(id=Preferences.get_value('sonarr_reconciliation_source_id'), is_library_source=True)
            target = SonarrInstance.objects.get(id=Preferences.get_value('sonarr_reconciliation_target_id'), is_ondemand_target=True)
            if source.id == target.id:
                save_log(provider, 2, 'Sonarr reconciliation source and target must be different instances')
                return {'result': 400, 'message': 'source_target_same'}
            source_api, target_api = SonarrAPI(instance_id=source.id), SonarrAPI(instance_id=target.id)
            source_series, target_series = source_api.get_series(), target_api.get_series()
            source_ok, source_error = validate_sonarr_series_response(source_series, 'source')
            if not source_ok:
                save_log(provider, 2, f'Sonarr reconciliation failed: permanent source series response invalid ({sanitize_text(source_error)})')
                return {'result': 502, 'message': source_error}
            target_ok, target_error = validate_sonarr_series_response(target_series, 'target')
            if not target_ok:
                save_log(provider, 2, f'Sonarr reconciliation failed: On Demand target series response invalid ({sanitize_text(target_error)})')
                return {'result': 502, 'message': target_error}
            source_by_tvdb = {s.get('tvdbId'): s for s in source_series}
            totals = calculate_episode_monitoring([], [])
            totals.series_compared = totals.episodes_inspected = totals.episodes_unchanged = 0
            include_specials = Preferences.get_value('sonarr_include_specials', '0') == '1'
            search_enabled = Preferences.get_value('sonarr_search_newly_eligible', '0') == '1'
            cleanup_enabled = Preferences.get_value('sonarr_cleanup_enabled', '0') == '1'
            cleanup_dry_run = Preferences.get_value('sonarr_cleanup_dry_run', '1') != '0'
            cleanup_grace_hours = int(Preferences.get_value('sonarr_cleanup_grace_hours', '24') or '24')
            cleanup_max = max(1, min(500, int(Preferences.get_value('sonarr_cleanup_max_deletions_per_run', '25') or '25')))
            cleanup_remaining_delete_attempts = cleanup_max
            cleanup_stop_real_deletes = False
            cleanup_totals = {'cleanup_candidates_new':0,'cleanup_candidates_pending':0,'cleanup_candidates_ready':0,'cleanup_candidates_cancelled':0,'cleanup_would_delete':0,'cleanup_files_deleted':0,'cleanup_files_already_absent':0,'cleanup_deferred_by_limit':0,'cleanup_failures':0}
            search_candidate_totals = {'search_candidates_new':0,'search_candidates_pending':0,'search_candidates_submitted':0,'search_candidates_cancelled':0,'search_candidates_deferred':0,'search_failures':0}
            for show in target_series:
                if not isinstance(show, dict) or _positive_int_value(show.get('tvdbId')) is None or _positive_int_value(show.get('id')) is None or not isinstance(show.get('monitored'), bool) or _season_updates_for_series(show, {})[2]:
                    totals.failures += 1
                    totals.series_update_failures += 1
                    continue
                src = source_by_tvdb.get(show.get('tvdbId'))
                if src:
                    src_eps = source_api.get_episodes(src['id'])
                    totals.series_compared += 1
                else:
                    src_eps = []
                    totals.series_target_only += 1
                tgt_eps = target_api.get_episodes(show['id'])
                stats = calculate_episode_monitoring(src_eps, tgt_eps, include_specials, search_enabled)
                totals.episodes_inspected += stats.episodes_inspected
                totals.episodes_unchanged += stats.episodes_unchanged
                totals.specials_ignored += stats.specials_ignored
                totals.future_episodes_ignored += stats.future_episodes_ignored
                totals.unscheduled_episodes_ignored += stats.unscheduled_episodes_ignored
                totals.malformed_episodes += stats.malformed_episodes
                updates, unchanged, season_failures = _season_updates_for_series(show, stats.desired_season_monitoring)
                totals.seasons_unchanged += unchanged
                if season_failures:
                    totals.failures += season_failures
                    totals.season_update_failures += season_failures
                    continue
                if stats.failures:
                    totals.failures += 1
                    continue

                applied_true, true_failed = _apply_monitor_batches(target_api, stats.monitor_true_ids, True)
                applied_false, false_failed = _apply_monitor_batches(target_api, stats.monitor_false_ids, False)
                totals.episodes_newly_monitored += len(applied_true)
                totals.episodes_newly_unmonitored += len(applied_false)
                if true_failed or false_failed:
                    totals.failures += 1
                    continue

                season_true, season_false, season_failures = _apply_season_updates(target_api, show['id'], updates)
                totals.seasons_newly_monitored += season_true
                totals.seasons_newly_unmonitored += season_false
                totals.season_update_failures += season_failures
                if season_failures:
                    totals.failures += season_failures
                    continue

                series_true, series_false, series_unchanged, series_failed = _apply_series_monitor_update(target_api, show['id'], show.get('monitored'), stats.desired_series_monitoring)
                totals.series_newly_monitored += series_true
                totals.series_newly_unmonitored += series_false
                totals.series_unchanged += series_unchanged
                if series_failed:
                    totals.series_update_failures += 1
                    totals.failures += 1
                    continue

                series_ok_for_cleanup = True
                candidate_counts, candidate_events, candidate_failed = update_search_candidates_for_series(
                    target_instance=target, tvdb_id=show.get('tvdbId'), target_series_id=show['id'],
                    target_episodes=tgt_eps, stats=stats, applied_monitor_true_ids=applied_true,
                    series_monitored_confirmed=stats.desired_series_monitoring is True)
                for key in search_candidate_totals:
                    search_candidate_totals[key] += candidate_counts.get(key, 0)
                for event in candidate_events:
                    save_log(provider, 1 if 'failure' not in event else 2, sanitize_text(event))
                if candidate_failed:
                    totals.failures += 1
                    continue

                if search_enabled:
                    submitted, search_events, search_failed = submit_pending_search_candidates(target_api=target_api, target_instance=target, target_series_id=show['id'])
                    totals.searches_triggered += submitted['submitted']
                    totals.initial_searches_triggered += submitted['initial_submitted']
                    search_candidate_totals['search_candidates_submitted'] += submitted['submitted']
                    search_candidate_totals['search_failures'] += submitted['failures']
                    for event in search_events:
                        save_log(provider, 1 if 'failure' not in event else 2, sanitize_text(event))
                    if search_failed:
                        totals.failures += submitted['failures'] or 1
                        series_ok_for_cleanup = False
                if series_ok_for_cleanup:
                    cleanup = process_cleanup_for_series(
                        target_instance=target, tvdb_id=show.get('tvdbId'), target_series_id=show['id'],
                        source_episodes=src_eps, target_episodes=tgt_eps, stats=stats, target_api=target_api,
                        source_api=source_api, source_series_id=src['id'] if src else None, include_specials=include_specials,
                        cleanup_enabled=cleanup_enabled, dry_run=cleanup_dry_run, grace_hours=cleanup_grace_hours,
                        remaining_delete_budget=cleanup_remaining_delete_attempts, stop_real_deletes=cleanup_stop_real_deletes)
                    cleanup_remaining_delete_attempts = max(0, cleanup_remaining_delete_attempts - cleanup.delete_attempts_consumed)
                    if cleanup.stop_deletes_for_run:
                        cleanup_stop_real_deletes = True
                    for key in cleanup_totals:
                        cleanup_totals[key] += getattr(cleanup, key)
                    totals.failures += cleanup.cleanup_failures
                    for event in cleanup.events:
                        save_log(provider, 1 if 'failure' not in event else 2, sanitize_text(event))
            status = 207 if totals.failures else 200
            log_status = 2 if totals.failures else 1
            save_log(provider, log_status, f'Sonarr reconciliation: series compared={totals.series_compared} target_only={totals.series_target_only} episodes inspected={totals.episodes_inspected} newly_monitored={totals.episodes_newly_monitored} newly_unmonitored={totals.episodes_newly_unmonitored} unchanged={totals.episodes_unchanged} searches={totals.searches_triggered} specials_ignored={totals.specials_ignored} future_ignored={totals.future_episodes_ignored} unscheduled_ignored={totals.unscheduled_episodes_ignored} malformed={totals.malformed_episodes} seasons_newly_monitored={totals.seasons_newly_monitored} seasons_newly_unmonitored={totals.seasons_newly_unmonitored} seasons_unchanged={totals.seasons_unchanged} season_update_failures={totals.season_update_failures} series_newly_monitored={totals.series_newly_monitored} series_newly_unmonitored={totals.series_newly_unmonitored} series_unchanged={totals.series_unchanged} series_update_failures={totals.series_update_failures} initial_searches_triggered={totals.initial_searches_triggered} search_candidates_new={search_candidate_totals['search_candidates_new']} search_candidates_pending={search_candidate_totals['search_candidates_pending']} search_candidates_submitted={search_candidate_totals['search_candidates_submitted']} search_candidates_cancelled={search_candidate_totals['search_candidates_cancelled']} search_candidates_deferred={search_candidate_totals['search_candidates_deferred']} search_failures={search_candidate_totals['search_failures']} failures={totals.failures} cleanup_candidates_new={cleanup_totals['cleanup_candidates_new']} cleanup_candidates_pending={cleanup_totals['cleanup_candidates_pending']} cleanup_candidates_ready={cleanup_totals['cleanup_candidates_ready']} cleanup_candidates_cancelled={cleanup_totals['cleanup_candidates_cancelled']} cleanup_would_delete={cleanup_totals['cleanup_would_delete']} cleanup_files_deleted={cleanup_totals['cleanup_files_deleted']} cleanup_files_already_absent={cleanup_totals['cleanup_files_already_absent']} cleanup_deferred_by_limit={cleanup_totals['cleanup_deferred_by_limit']} cleanup_failures={cleanup_totals['cleanup_failures']}')
            return {'result': status, 'failures': totals.failures, 'message': 'partial_failure' if totals.failures else 'ok'}
        except Exception:
            save_log(provider, 2, sanitize_text(traceback.format_exc()))
            return {'result': 500, 'message': 'exception'}

@cron_task(cron_schedule="*/5 * * * *")
@task
def reconcile_sonarr_ondemand_task():
    return reconcile_sonarr_ondemand()
