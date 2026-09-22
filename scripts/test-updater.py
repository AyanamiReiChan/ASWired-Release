import datetime
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('updater',pathlib.Path(__file__).resolve().parents[1]/'deploy/update-request.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)

class RequestTests(unittest.TestCase):
    def test_semver_and_shell_injection(self):
        for value in ['latest','v1.2','v1.2.3;id','v1.2.3/../x','v01.2.3','v1.2.3-01','v1.2.3-a..b']:
            with self.assertRaises(ValueError):updater.version_key(value)
        versions=['v1.2.3-alpha','v1.2.3-alpha.1','v1.2.3-beta','v1.2.3','v1.2.4']
        self.assertEqual(sorted(versions,key=updater.version_key),versions)

    def test_symlink_does_not_read_or_delete_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);data=root/'data';data.mkdir();secret=root/'private';secret.write_text('secret')
            (data/'update-request.json').symlink_to(secret)
            with patch.object(updater,'DATA',data):
                with self.assertRaises(OSError):updater.read_request()
            self.assertEqual(secret.read_text(),'secret')

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

    def test_fifo_does_not_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            data=pathlib.Path(temporary)
            os.mkfifo(data/'update-request.json')
            with patch.object(updater,'DATA',data):
                with self.assertRaises(ValueError):updater.read_request()

if __name__=='__main__':unittest.main()
