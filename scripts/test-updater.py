import datetime
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if os.name != 'posix':
    for name in ('fcntl', 'grp'):
        sys.modules[name] = types.ModuleType(name)

spec=importlib.util.spec_from_file_location('updater',pathlib.Path(__file__).resolve().parents[1]/'deploy/update-request.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)

class RequestTests(unittest.TestCase):
    def test_semver_and_shell_injection(self):
        for value in ['latest','v1.2','v1.2.3;id','v1.2.3/../x','v01.2.3','v1.2.3-01','v1.2.3-a..b']:
            with self.assertRaises(ValueError):updater.version_key(value)
        versions=['v1.2.3-alpha','v1.2.3-alpha.1','v1.2.3-beta','v1.2.3','v1.2.4']
        self.assertEqual(sorted(versions,key=updater.version_key),versions)

    @unittest.skipUnless(os.name=='posix','POSIX file boundary')
    def test_symlink_does_not_read_or_delete_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);data=root/'data';data.mkdir();secret=root/'private';secret.write_text('secret')
            (data/'update-request.json').symlink_to(secret)
            with patch.object(updater,'DATA',data):
                with self.assertRaises(OSError):updater.read_request()
            self.assertEqual(secret.read_text(),'secret')

    @unittest.skipUnless(os.name=='posix','POSIX file boundary')
    def test_request_expiry_size_and_extra_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            data=pathlib.Path(temporary);path=data/'update-request.json'
            valid={'version':'v1.0.2','createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
            for row in [{**valid,'command':'id'},{**valid,'createdAt':'2000-01-01T00:00:00Z'},{**valid,'version':'a'*5000}]:
                path.write_text(json.dumps(row))
                with patch.object(updater,'DATA',data):
                    with self.assertRaises(ValueError):updater.read_request()
                path.unlink()
            path.write_text(json.dumps(valid))
            with patch.object(updater,'DATA',data):self.assertEqual(updater.read_request(),'v1.0.2')
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name=='posix','POSIX file boundary')
    def test_fifo_does_not_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            data=pathlib.Path(temporary)
            os.mkfifo(data/'update-request.json')
            with patch.object(updater,'DATA',data):
                with self.assertRaises(ValueError):updater.read_request()

    def test_legacy_failure_requires_complete_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);(root/'VERSION').write_text('v1.0.6')
            with patch.object(updater,'CURRENT',root),patch.object(updater,'status') as status,patch.object(updater.subprocess,'run') as run:
                self.assertFalse(updater.reconcile({'version':'v1.0.7'}));run.assert_not_called()
                run.return_value.returncode=1
                self.assertFalse(updater.reconcile({'version':'v1.0.6'}));status.assert_not_called()
                run.return_value.returncode=0
                self.assertTrue(updater.reconcile({'version':'v1.0.6'}))
                self.assertEqual(status.call_args.args[0],'completed')
                self.assertIn('--version',run.call_args.args[0])

if __name__=='__main__':unittest.main()
