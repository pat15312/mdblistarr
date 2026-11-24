"""Disposable Linux/Docker qualification; never supply production bind mounts.

Usage: python tests/jellyfin_container_smoke.py IMAGE
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

image = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='mdblistarr-jellyfin-') as scratch:
    root = Path(scratch)
    for directory in ('state', 'remote', 'nested', 'library/tv/Show/Season 01',
                      'library/tv/Mounted/Season 01', 'library/movies/Movie',
                      'library/movies/ReadOnly'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / 'remote/backing').write_bytes(b'synthetic preserved backing object')
    for relative in ('tv/Show/Season 01/episode.mkv', 'movies/Movie/version1.mkv',
                     'movies/Movie/version2.mkv'):
        (root / 'library' / relative).symlink_to('/remote/backing')

    def run(code, *, readonly=False, nested=False):
        command = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                   '--tmpfs', '/tmp', '--mount', f'type=bind,src={root / "state"},dst=/state',
                   '--mount', f'type=bind,src={root / "remote"},dst=/remote,readonly',
                   '--mount', f'type=bind,src={root / "library"},dst=/library' + (',readonly' if readonly else '')]
        if nested:
            command += ['--mount', f'type=bind,src={root / "nested"},dst=/library/tv/Mounted/Season 01']
        command += ['--entrypoint', 'python', image, '-c',
                    'import json,os; from pathlib import Path; from mdblistrr import empty_directories as d\n' + code]
        subprocess.run(command, check=True)

    run('''plans = {
 'show': d.snapshot('/library/tv', '/library/tv/Show', 'sonarr', ['/library/tv/Show/Season 01']),
 'movie': d.snapshot('/library/movies', '/library/movies/Movie', 'radarr'),
 'mount': d.snapshot('/library/tv', '/library/tv/Mounted', 'sonarr', ['/library/tv/Mounted/Season 01']),
 'readonly': d.snapshot('/library/movies', '/library/movies/ReadOnly', 'radarr')}
Path('/state/plans.json').write_text(json.dumps(plans))
print('Captured original identities in first container')
''')
    run('''plans=json.loads(Path('/state/plans.json').read_text())
plan=plans['show']
assert d.inspect(plan, 'Season 01') == 'nonempty'
os.unlink('/library/tv/Show/Season 01/episode.mkv') # Disposable native-removal simulation.
def intent():
 plan['intent']='Season 01'
 Path('/state/plans.json').write_text(json.dumps(plans))
d.remove(plan, 'Season 01', intent)
# Exit with intent retained: simulate death before the outcome is persisted.
print('Second container removed only the empty season and retained original intent')
''')
    run('''plans=json.loads(Path('/state/plans.json').read_text())
plan=plans['show']
assert plan['intent']=='Season 01' and d.inspect(plan, 'Season 01')=='absent'
plan['intent']=None
d.remove(plan, '', lambda: None)
for version in ('version1.mkv','version2.mkv'):
 os.unlink('/library/movies/Movie/'+version) # Disposable native-removal simulation.
d.remove(plans['movie'], '', lambda: None)
assert d.inspect(plan, '')=='absent' and d.inspect(plans['movie'], '')=='absent'
assert Path('/remote/backing').read_bytes()==b'synthetic preserved backing object'
print('Third container recovered original identities; TV/movie directories absent, backing object preserved')
''')
    run('''plan=json.loads(Path('/state/plans.json').read_text())['mount']
try:
 d.inspect(plan, 'Season 01')
except d.DirectoryDeferred:
 print('Actual nested bind mount rejected')
else:
 raise AssertionError('Nested mount accepted')
''', nested=True)
    run('''plan=json.loads(Path('/state/plans.json').read_text())['readonly']
try:
 d.remove(plan, '', lambda: None)
except OSError as error:
 assert error.errno==30, error
 assert d.inspect(plan, '')=='empty'
 print('Actual read-only mount refuses removal and preserves directory')
else:
 raise AssertionError('Read-only removal succeeded')
''', readonly=True)
    assert (root / 'remote/backing').read_bytes() == b'synthetic preserved backing object'
print(json.dumps({'container_recreation': 'passed', 'uncertain_intent_recovery': 'passed',
                  'nested_bind_mount': 'rejected', 'readonly_mount': 'preserved',
                  'remote_object': 'preserved', 'products': ['sonarr', 'radarr']}))
