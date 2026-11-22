"""Authenticated MDBList snapshots and canonical blacklist identities.

Only complete, repeatable reads replace the last successful snapshot. Errors
are fixed messages, never remote bodies, credential-bearing URLs or exceptions.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from .models import BlacklistSource
from .services import MDBListarr

MAX_PAGES = 100
PAGE_SIZE = 1000
MAX_SOURCES = 20


class BlacklistError(ValueError):
    pass


def positive_id(value):
    return value if type(value) is int and 0 < value < 2**31 else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def list_path(value):
    value = value.strip()
    if re.fullmatch(r'[1-9][0-9]{0,9}', value) and int(value) < 2**31:
        return f'/lists/{int(value)}'
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in {'mdblist.com', 'www.mdblist.com'}
                or url.port is not None or url.username or url.password or url.query or url.fragment):
            raise ValueError
    except ValueError:
        raise BlacklistError('Enter a numeric list ID or an HTTPS MDBList list URL.') from None
    path = url.path.rstrip('/')
    if not re.fullmatch(r'/lists/(?:[1-9][0-9]{0,9}|share/[A-Za-z0-9_-]{16,128}|[A-Za-z0-9_-]{1,80}/[A-Za-z0-9_-]{1,160})', path):
        raise BlacklistError('This MDBList list URL is not supported.')
    return path


class ListClient:
    def __init__(self, api=None):
        self.api = api if api is not None else MDBListarr().mdblist
        if self.api is None:
            raise BlacklistError('Connect MDBList or configure its API key first.')
        self.translations = {}
        self.deadline = time.monotonic() + 120

    def get(self, path, params=None):
        if time.monotonic() >= self.deadline:
            raise BlacklistError('Blacklist refresh time budget exceeded; evaluation deferred.')
        self.api._ensure_valid_token()
        auth = self.api._auth()
        query = {**(auth.get('params') or {}), **(params or {})}
        try:
            response = self.api.connect.session.get(
                'https://api.mdblist.com' + path, headers=auth.get('headers'),
                params=query, timeout=(5, 20), allow_redirects=False)
            if response.status_code in (401, 403):
                raise BlacklistError('MDBList authentication or list access was denied.')
            if response.status_code == 429:
                raise BlacklistError('MDBList rate limit reached; refresh deferred.')
            if response.status_code != 200:
                raise BlacklistError('MDBList list request failed. Check access and try later.')
            data = response.json()
            if isinstance(data, dict) and any(k in data for k in ('error', 'errorMessage')):
                raise BlacklistError('MDBList returned an error instead of list evidence.')
            return data, response.headers
        except (requests.RequestException, ValueError) as exc:
            if isinstance(exc, BlacklistError):
                raise
            raise BlacklistError('MDBList returned unavailable or malformed evidence.') from None

    def metadata(self, path):
        data, _ = self.get(path)
        rows = data if isinstance(data, list) else [data]
        if not rows or len(rows) > MAX_SOURCES:
            raise BlacklistError('MDBList returned invalid list metadata.')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not positive_id(row.get('id'))
                    or not isinstance(row.get('name'), str) or not row['name'].strip()
                    or type(row.get('items')) is not int or row['items'] < 0
                    or row['id'] in seen):
                raise BlacklistError('MDBList returned invalid list metadata.')
            seen.add(row['id'])
        return rows

    def identity(self, row, media):
        if not isinstance(row, dict) or row.get('mediatype') != media:
            raise BlacklistError('List contains an unsupported or malformed media identity.')
        ids = row.get('ids', {})
        if not isinstance(ids, dict):
            raise BlacklistError('List contains malformed external identities.')
        tmdb = positive_id(row.get('id'))
        if not tmdb or (ids.get('tmdb') is not None and ids['tmdb'] != tmdb):
            raise BlacklistError('List contains missing or conflicting TMDb identities.')
        tvdb = row.get('tvdb_id')
        if tvdb is not None and not positive_id(tvdb):
            raise BlacklistError('List contains an invalid TVDb identity.')
        if ids.get('tvdb') is not None:
            if not positive_id(ids['tvdb']) or (tvdb is not None and ids['tvdb'] != tvdb):
                raise BlacklistError('List contains conflicting TVDb identities.')
            tvdb = ids['tvdb']
        if media == 'show' and tvdb is None:
            if tmdb not in self.translations:
                data, _ = self.get(f'/tmdb/show/{tmdb}')
                translated = data.get('ids', {}) if isinstance(data, dict) else {}
                if (not isinstance(data, dict) or data.get('type') != 'show'
                        or not isinstance(translated, dict) or translated.get('tmdb') != tmdb
                        or not positive_id(translated.get('tvdb'))):
                    raise BlacklistError('A series has no verified TMDb to TVDb translation.')
                self.translations[tmdb] = translated['tvdb']
            tvdb = self.translations[tmdb]
        return {'media': media, 'tmdb': tmdb, 'tvdb': tvdb if media == 'show' else None}

    def membership(self, path, expected_count):
        rows, cursors = [], set()
        params = {'limit': PAGE_SIZE, 'sort': 'rank', 'order': 'asc'}
        total = None
        for _ in range(MAX_PAGES):
            data, headers = self.get(path + '/items', params)
            if (not isinstance(data, dict) or not isinstance(data.get('movies'), list)
                    or not isinstance(data.get('shows'), list)
                    or data.get('seasons') or data.get('episodes')):
                raise BlacklistError('MDBList returned incomplete or unsupported list membership.')
            page = ([self.identity(row, 'movie') for row in data['movies']]
                    + [self.identity(row, 'show') for row in data['shows']])
            if len(page) > PAGE_SIZE:
                raise BlacklistError('MDBList returned an oversized page.')
            rows.extend(page)
            raw_total = headers.get('X-Total-Items')
            if raw_total is not None:
                if not re.fullmatch(r'[0-9]+', str(raw_total)):
                    raise BlacklistError('MDBList returned invalid pagination totals.')
                current_total = int(raw_total)
                if total is not None and total != current_total:
                    raise BlacklistError('List changed while it was being read. Refresh deferred.')
                total = current_total
            pagination = data.get('pagination', {})
            if not isinstance(pagination, dict):
                raise BlacklistError('MDBList returned malformed pagination.')
            for key in ('total', 'offset', 'limit'):
                if key in pagination and (type(pagination[key]) is not int or pagination[key] < 0):
                    raise BlacklistError('MDBList returned malformed pagination values.')
            if 'total' in pagination and pagination['total'] != expected_count:
                raise BlacklistError('MDBList pagination contradicts list membership counts.')
            if 'offset' in pagination and pagination['offset'] != len(rows) - len(page):
                raise BlacklistError('MDBList pagination offset did not match the requested page.')
            cursor = pagination.get('next_cursor', data.get('next_cursor'))
            if ('next_cursor' in pagination and 'next_cursor' in data
                    and pagination['next_cursor'] != data['next_cursor']):
                raise BlacklistError('MDBList pagination cursors disagree.')
            more = headers.get('X-Has-More')
            if more not in (None, 'true', 'false'):
                raise BlacklistError('MDBList returned invalid pagination evidence.')
            if 'has_more' in pagination:
                if type(pagination['has_more']) is not bool:
                    raise BlacklistError('MDBList returned malformed pagination continuation.')
                body_more = str(pagination['has_more']).lower()
                if more is not None and more != body_more:
                    raise BlacklistError('MDBList pagination continuation signals disagree.')
                more = body_more
            if cursor:
                if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in cursors or not page or more == 'false':
                    raise BlacklistError('MDBList pagination did not advance reliably.')
                cursors.add(cursor)
                params = {**params, 'cursor': cursor}
            elif more == 'true':
                # Documented legacy offset pagination, still returned by older API deployments.
                if not page or 'cursor' in params:
                    raise BlacklistError('MDBList returned an incomplete page sequence.')
                params = {**params, 'offset': len(rows)}
            else:
                if more is None and len(rows) != expected_count:
                    raise BlacklistError('MDBList did not prove complete list membership.')
                break
        else:
            raise BlacklistError('List exceeds the supported bounded refresh size.')
        if len(rows) != expected_count or (total is not None and total != expected_count):
            raise BlacklistError('List membership count changed or the response was incomplete.')
        unique = {}
        tvdb_to_tmdb = {}
        for row in rows:
            key = (row['media'], row['tmdb'])
            if key in unique and unique[key] != row:
                raise BlacklistError('List contains conflicting canonical identities.')
            if row['media'] == 'show':
                previous = tvdb_to_tmdb.setdefault(row['tvdb'], row['tmdb'])
                if previous != row['tmdb']:
                    raise BlacklistError('Multiple series map to the same TVDb identity.')
            unique[key] = row
        return [unique[key] for key in sorted(unique)]

    def snapshot(self, path, expected_id):
        metadata = self.metadata(path)
        if len(metadata) != 1 or metadata[0]['id'] != expected_id:
            raise BlacklistError('Configured list identity changed.')
        first = self.membership(path, metadata[0]['items'])
        # No atomic snapshot token is promised by MDBList. Repeat the bounded read
        # to detect edits, including same-count substitutions during pagination.
        after = self.metadata(path)
        if len(after) != 1 or after[0]['id'] != expected_id or after[0]['items'] != metadata[0]['items']:
            raise BlacklistError('List changed while it was being read. Refresh deferred.')
        self.translations.clear()
        if first != self.membership(path, after[0]['items']):
            raise BlacklistError('List changed while it was being read. Refresh deferred.')
        return after[0]['name'][:255], first


@dataclass
class BlacklistEvidence:
    sources: tuple
    members: dict
    verified_at: object

    def contains(self, product, external_id):
        return (product, external_id) in self.members

    def current(self):
        return self.sources == configuration_signature()

    def fresh(self):
        return self.current() and timezone.now() - self.verified_at <= timedelta(minutes=5)


def configuration_signature():
    return tuple((row.pk, row.list_id, digest(row.api_path)) for row in BlacklistSource.objects.order_by('pk'))


def _combined_members(snapshots):
    members, tmdb_to_tvdb, tvdb_to_tmdb = {}, {}, {}
    for source_id, snapshot in snapshots:
        for row in snapshot:
            if row['media'] == 'show':
                if (tmdb_to_tvdb.setdefault(row['tmdb'], row['tvdb']) != row['tvdb']
                        or tvdb_to_tmdb.setdefault(row['tvdb'], row['tmdb']) != row['tmdb']):
                    raise BlacklistError('Blacklist sources disagree about series identities.')
            key = ('radarr', row['tmdb']) if row['media'] == 'movie' else ('sonarr', row['tvdb'])
            members.setdefault(key, []).append(source_id)
    return members


def refresh_blacklists(force=True):
    sources = list(BlacklistSource.objects.order_by('pk'))
    signature = tuple((row.pk, row.list_id, digest(row.api_path)) for row in sources)
    if not sources:
        return BlacklistEvidence(signature, {}, timezone.now())
    now = timezone.now()
    if not force and all(source.last_success_at and not source.last_error
            and now - source.last_success_at <= timedelta(seconds=60)
            and source.snapshot_digest == digest(source.snapshot) for source in sources):
        return BlacklistEvidence(signature, _combined_members(
            [(source.list_id, source.snapshot) for source in sources]), min(source.last_success_at for source in sources))
    prepared = []
    try:
        client = ListClient()
        for source in sources:
            if timezone.now() - now > timedelta(seconds=120):
                raise BlacklistError('Blacklist refresh time budget exceeded; evaluation deferred.')
            name, snapshot = client.snapshot(source.api_path, source.list_id)
            prepared.append((source, name, snapshot))
        members = _combined_members([(source.list_id, snapshot) for source, _, snapshot in prepared])
        if timezone.now() - now > timedelta(seconds=120):
            raise BlacklistError('Blacklist refresh time budget exceeded; evaluation deferred.')
    except BlacklistError as exc:
        BlacklistSource.objects.filter(pk__in=[source.pk for source in sources]).update(last_attempt_at=now, last_error=str(exc))
        raise
    with transaction.atomic():
        if configuration_signature() != signature:
            raise BlacklistError('Blacklist configuration changed during refresh.')
        for source, name, snapshot in prepared:
            BlacklistSource.objects.filter(pk=source.pk).update(
                name=name, snapshot=snapshot, snapshot_digest=digest(snapshot),
                movie_count=sum(row['media'] == 'movie' for row in snapshot),
                series_count=sum(row['media'] == 'show' for row in snapshot),
                last_success_at=now, last_attempt_at=now, last_error='')
    if configuration_signature() != signature:
        raise BlacklistError('Blacklist configuration changed during refresh.')
    return BlacklistEvidence(signature, members, timezone.now())


def add_source(value):
    path = list_path(value)
    client = ListClient()
    metadata = client.metadata(path)
    if re.fullmatch(r'/lists/[0-9]+', path) and (len(metadata) != 1 or metadata[0]['id'] != int(path.split('/')[-1])):
        raise BlacklistError('MDBList returned a different list identity than requested.')
    prepared = []
    for row in metadata:
        item_path = path if '/share/' in path else f"/lists/{row['id']}"
        name, snapshot = client.snapshot(item_path, row['id'])
        prepared.append((row['id'], item_path, name, snapshot))
    with transaction.atomic():
        existing = list(BlacklistSource.objects.exclude(list_id__in=[row[0] for row in prepared]))
        if len(existing) + len(prepared) > MAX_SOURCES:
            raise BlacklistError('At most 20 blacklist sources can be configured.')
        _combined_members([(source.list_id, source.snapshot) for source in existing]
                          + [(list_id, snapshot) for list_id, _, _, snapshot in prepared])
        for list_id, item_path, name, snapshot in prepared:
            BlacklistSource.objects.update_or_create(list_id=list_id, defaults={
                'api_path': item_path, 'name': name, 'snapshot': snapshot,
                'snapshot_digest': digest(snapshot), 'last_success_at': timezone.now(),
                'last_attempt_at': timezone.now(), 'last_error': '',
                'movie_count': sum(row['media'] == 'movie' for row in snapshot),
                'series_count': sum(row['media'] == 'show' for row in snapshot)})
    return len(prepared)
