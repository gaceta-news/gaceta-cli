"""Disposable rehearsal: run with old CLI path and candidate wheel path.

Old CLI must be independently installed from the prior tag. Never uses an
existing data directory; retains the temp root for diagnosis. No live feeds.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

old, wheel = map(lambda p: Path(p).resolve(), sys.argv[1:])
root = Path(tempfile.mkdtemp(prefix='gaceta-rehearsal-'))
data = root / 'paper'

def run(*args, state=data):
    subprocess.run(args, check=True, cwd=root, env={**os.environ, 'GACETA_DATA': str(state)})

def pipeline(cli, state=data):
    for args in [('ingest','rss'), ('embed',), ('cluster',), ('write',), ('edition','--force'), ('render',)]:
        run(cli, *args, '--reader', 'main', state=state)
    assert (state/'site/main/index.html').stat().st_size

def inventory(state):
    with sqlite3.connect(state/'gaceta.db') as db:
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)
        return [db.execute(f'select {cols} from {table} order by 1,2').fetchall() for table, cols in [('stories','reader_id,id'),('items','id,url'),('seen','reader_id,by_name,story_id,rev,at'),('saves','reader_id,by_name,story_id,note,at')]]

def digest(root):
    return {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

run(old, 'init'); pipeline(old)
with sqlite3.connect(data/'gaceta.db') as db:
    ids = [x[0] for x in db.execute('select id from stories order by id limit 2')]
    assert len(ids) == 2
    db.execute("insert into seen values ('main','synthetic',?,'test-rev',42)", (ids[0],))
    db.execute("insert into saves(reader_id,by_name,story_id,origin,note,at) values('main','synthetic',?,'gaceta','synthetic bookmark',42)", (ids[1],))
expected = inventory(data)
backup = root/'backup'; shutil.copytree(data,backup); hashes=digest(backup)
venv = root/'venv'; run(sys.executable,'-m','venv',venv)
python = venv/'bin/python'; cli = venv/'bin/gaceta'
run(python,'-m','pip','install',wheel); pipeline(cli)
assert inventory(data)==expected
run(cli,'agent','--help')
run(python,'-m','pip','uninstall','-y','gaceta'); assert not cli.exists()
assert inventory(data)==expected and digest(backup)==hashes
# Restore in place: relocating a file-backed RSS source changes its identity.
# Move the upgraded state aside, retaining a second copy for diagnosis.
data.rename(root/'upgraded'); shutil.copytree(backup,data); assert digest(data)==hashes
pipeline(old,data); assert inventory(data)==expected
with zipfile.ZipFile(wheel) as z: assert 'gaceta/agent.py' in z.namelist()
print('PASS synthetic upgrade, uninstall preserving data, restored old-CLI rebuild; inventory counts:', list(map(len,expected)))
print('Wheel:', wheel.name, 'SHA256:', hashlib.sha256(wheel.read_bytes()).hexdigest())
