"""Portable config/rollback tests plus POSIX request-boundary regression tests."""
import copy
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
    for name in ('fcntl', 'grp', 'pwd'):
        sys.modules[name] = types.ModuleType(name)
    sys.modules['grp'].getgrnam = lambda name: types.SimpleNamespace(gr_gid=0)
spec = importlib.util.spec_from_file_location('worker', pathlib.Path(__file__).resolve().parents[1]/'deploy/site-certificates.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def config():
    return {'apps':{'http':{'servers':{'web':{'listen':[':443'],
        'routes':[{'match':[{'host':['panel.example.test','probe.example.test']}],
                   'handle':[{'handler':'reverse_proxy','upstreams':[{'dial':'127.0.0.1:3000'}]}]}],
        'tls_connection_policies':[{'protocol_min':'tls1.3'}],
        'automatic_https':{'skip_certificates':['unrelated.example.test']}}}},
        'tls':{'certificates':{'load_files':[{'certificate':'/original/cert.pem','key':'/original/key.pem','tags':['keep']}]}}}}


TARGETS = {'panel':{'host':'panel.example.test','port':443}, 'komari':{'host':'probe.example.test','port':443}}
MATERIAL = {'certificate':'/managed/fullchain.pem','key':'/managed/privkey.pem'}


def request(operation='deploy'):
    return dict(id='fixture-request-12345678', operation=operation, certificateId='test-certificate' if operation=='deploy' else '',
        sites=['panel','komari'], certificate='public-certificate' if operation=='deploy' else '',
        privateKey='secret-test-material' if operation=='deploy' else '', createdAt=worker.now().isoformat())


class ConfigurationTests(unittest.TestCase):
    def test_both_sites_preserve_routes_and_tls_policy(self):
        original = config()
        updated, bindings = worker.compile_config(original, {}, request(), TARGETS, MATERIAL, '123')
        self.assertEqual(original, config(), 'compiler mutated active snapshot')
        self.assertEqual(updated['apps']['http']['servers']['web']['routes'], original['apps']['http']['servers']['web']['routes'])
        policies = updated['apps']['http']['servers']['web']['tls_connection_policies']
        self.assertEqual(len(policies), 3)
        self.assertTrue(all(p['protocol_min']=='tls1.3' for p in policies))
        self.assertEqual(set(bindings), {'panel','komari'})
        loaders = updated['apps']['tls']['certificates']['load_files']
        self.assertEqual(len(loaders),3)
        self.assertEqual(loaders[0]['tags'],['keep'])

    def test_renewal_replaces_only_owned_material_and_external_restores_automation(self):
        updated, bindings = worker.compile_config(config(), {}, request(), TARGETS, MATERIAL, '123')
        updated, bindings = worker.compile_config(updated, bindings, request(), TARGETS, MATERIAL, '124')
        self.assertEqual(len(updated['apps']['tls']['certificates']['load_files']),3)
        self.assertEqual(bindings['panel']['serial'],'124')
        restored, bindings = worker.compile_config(updated, bindings, request('external'), TARGETS)
        self.assertEqual(restored,config())
        self.assertEqual(bindings,{})

    def test_preexisting_disabled_automation_is_not_enabled_on_return(self):
        original=config();original['apps']['http']['servers']['web']['automatic_https']['skip_certificates'].append('panel.example.test')
        updated,bindings=worker.compile_config(original,{},request(),TARGETS,MATERIAL,'123')
        restored,_=worker.compile_config(updated,bindings,request('external'),TARGETS)
        self.assertEqual(restored,original)

    def test_unrelated_site_and_custom_tls_constraints_are_rejected(self):
        for policies in [[{'match':{'sni':['panel.example.test']},'client_auth':{'mode':'require_and_verify'}},{}],
                         [{'match':{'remote_ip':['127.0.0.1/32']},'client_auth':{'mode':'require_and_verify'}},{}]]:
            original=config();original['apps']['http']['servers']['web']['tls_connection_policies']=policies
            with self.assertRaises(ValueError):worker.compile_config(original,{},request(),TARGETS,MATERIAL,'123')
        targets=copy.deepcopy(TARGETS);targets['panel']['host']='unmanaged.example.test'
        with self.assertRaises(ValueError):worker.compile_config(config(),{},request(),targets,MATERIAL,'123')

    def test_failed_reload_restores_persisted_and_live_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);state=root/'state';state.mkdir();(state/'backups').mkdir();(state/'material').mkdir()
            active_file=root/'config.json';dropin=root/'dropin.conf'
            active_file.write_text('original-file');dropin.write_text('original-dropin')
            active=config();loads=[]
            def proxy(value=None):
                if value is None:return copy.deepcopy(active)
                loads.append(copy.deepcopy(value))
                if len(loads)==1:raise ValueError('simulated Caddy rejection')
            def atomic(path,value,*args):path.write_bytes(value)
            def write_json(path,value,**kwargs):path.write_text(json.dumps(value))
            with patch.object(worker,'STATE',state),patch.object(worker,'CONFIG',active_file),patch.object(worker,'DROPIN',dropin),\
                 patch.object(worker,'website_targets',return_value=TARGETS),patch.object(worker,'caddy',side_effect=proxy),\
                 patch.object(worker,'validate_material',return_value=(MATERIAL,'fingerprint','123')),\
                 patch.object(worker,'run',return_value=b'caddy\n'),patch.object(worker,'atomic',side_effect=atomic),\
                 patch.object(worker,'write_json',side_effect=write_json),patch.object(worker.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=0)):
                with self.assertRaises(ValueError):worker.apply(request())
            self.assertEqual(loads[-1],active)
            self.assertEqual(active_file.read_text(),'original-file')
            self.assertEqual(dropin.read_text(),'original-dropin')
            self.assertFalse((state/'transaction.json').exists())
            self.assertEqual(json.loads((state/'bindings.json').read_text()),{})


@unittest.skipUnless(os.name=='posix','POSIX permission boundary')
class RequestTests(unittest.TestCase):
    def read(self,path):
        with patch.object(worker,'DATA',path),patch.object(worker.pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=os.getuid())):
            return worker.read_request()

    def test_schema_target_expiry_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);path=root/worker.REQUEST
            valid=request()
            for invalid in [{**valid,'command':'id'},{**valid,'sites':['../../root']},
                            {**valid,'createdAt':'2000-01-01T00:00:00Z'},
                            {**valid,'operation':'external'}, {**valid,'id':'../../outside'},
                            {**valid,'certificate':'x'*(worker.LIMIT+1)}]:
                path.write_text(json.dumps(invalid));path.chmod(0o600)
                with self.assertRaises(ValueError):self.read(root)
            path.write_text(json.dumps(valid));path.chmod(0o644)
            with self.assertRaises(ValueError):self.read(root)
            path.chmod(0o600);self.assertEqual(self.read(root)['certificateId'],'test-certificate')

    def test_symlink_and_fifo_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);secret=root/'untouched';secret.write_text('private')
            path=root/worker.REQUEST;path.symlink_to(secret)
            with self.assertRaises(OSError):self.read(root)
            path.unlink();os.mkfifo(path)
            with self.assertRaises(ValueError):self.read(root)
            self.assertEqual(secret.read_text(),'private')


if __name__=='__main__':
    unittest.main()
