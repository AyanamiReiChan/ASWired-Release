"""CI-only verification of installed privilege boundaries and worker outcomes."""
import datetime
import grp
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

assert os.geteuid()==0
assert os.environ.get('GITHUB_ACTIONS')=='true', 'Run only on a disposable GitHub Actions runner'
spec=importlib.util.spec_from_file_location('updater','/opt/aswired/current/deploy/update-request.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)
def run(args):return subprocess.check_output(args,text=True).strip()
def request(path,version):
    path.write_text(json.dumps({'version':version,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}))

# An unprivileged request triggers the installed root worker; downgrade is denied
# without restarting the running application or exposing root-owned status writes.
pid=run(['systemctl','show','aswired-server','-p','MainPID','--value'])
command="import datetime,json,pathlib; pathlib.Path('/var/lib/aswired/update-request.json').write_text(json.dumps({'version':'v0.0.0','createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}))"
subprocess.run(['runuser','-u','aswired','--','python3','-c',command],check=True)
for _ in range(100):
    if not pathlib.Path('/var/lib/aswired/update-request.json').exists() and pathlib.Path('/var/lib/aswired-updater/status.json').exists():break
    time.sleep(.1)
state=json.loads(pathlib.Path('/var/lib/aswired-updater/status.json').read_text())
assert state['phase']=='failed'
assert run(['systemctl','show','aswired-server','-p','MainPID','--value'])==pid
subprocess.run(['runuser','-u','aswired','--','test','-r','/var/lib/aswired-updater/status.json'],check=True)
assert subprocess.run(['runuser','-u','aswired','--','test','-w','/var/lib/aswired-updater/status.json']).returncode!=0

# Exercise successful and failed execution through a real shell subprocess, in
# isolated directories. The production worker has no path or command input.
with tempfile.TemporaryDirectory(prefix='aswired-updater-ci-') as temporary:
    root=pathlib.Path(temporary);data=root/'data';data.mkdir();current=root/'current';current.mkdir();state=root/'state';state.mkdir(mode=0o750)
    (current/'VERSION').write_text('v1.0.1\n')
    (current/'update.sh').write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$1" > "$(dirname "$0")/VERSION"\n')
    with patch.object(updater,'DATA',data),patch.object(updater,'STATE',state),patch.object(updater,'CURRENT',current),patch.object(sys,'argv',['update-request.py']):
        request(data/'update-request.json','v1.0.2');updater.main()
        assert json.loads((state/'status.json').read_text())['phase']=='completed'
        assert not (data/'update-request.json').exists()
        (current/'update.sh').write_text('#!/bin/bash\nexit 7\n')
        request(data/'update-request.json','v1.0.3');updater.main()
        assert json.loads((state/'status.json').read_text())['phase']=='failed'
        assert (current/'VERSION').read_text().strip()=='v1.0.2'
        assert not (data/'update-request.json').exists()
print('PASS: installed root worker, non-admin filesystem boundary, downgrade denial, actual subprocess success/failure and request cleanup')

# Verify the real installed stack on a non-default frontend port. A legacy
# worker's failed status is only reconciled when all target services pass.
web_env=pathlib.Path('/etc/aswired/web.env')
original_env=web_env.read_bytes()
version=pathlib.Path('/opt/aswired/current/VERSION').read_text().strip()
checker=['/usr/bin/python3','/opt/aswired/current/deploy/verify-health.py','--version',version]
recovery=['/usr/bin/python3','/opt/aswired/current/deploy/update-request.py','--recover']
subprocess.run(checker,check=True)
try:
    web_env.write_bytes(original_env.replace(b'PORT=3000',b'PORT=13000'))
    subprocess.run(['systemctl','restart','aswired-web'],check=True)
    subprocess.run(checker,check=True)
    updater.status('failed',version,'legacy fixed-port verification failed')
    subprocess.run(recovery,check=True)
    assert json.loads((updater.STATE/'status.json').read_text())['phase']=='completed'
    # An installed version file with a stopped service must remain a failure.
    subprocess.run(['systemctl','stop','aswired-web'],check=True)
    updater.status('failed',version,'simulated stopped frontend')
    subprocess.run(recovery,check=True)
    assert json.loads((updater.STATE/'status.json').read_text())['phase']=='failed'
finally:
    web_env.write_bytes(original_env)
    subprocess.run(['systemctl','start','aswired-web'],check=True)
subprocess.run(checker,check=True)
print('PASS: configured frontend port 13000, legacy updater outcome reconciliation, and no false success with an unavailable service')
