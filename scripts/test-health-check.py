"""Portable regression checks for custom ports and conservative release verification."""
import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('health', pathlib.Path(__file__).resolve().parents[1]/'deploy/verify-health.py')
health = importlib.util.module_from_spec(spec); spec.loader.exec_module(health)


class HealthTests(unittest.TestCase):
    def test_configured_ports_and_quoted_systemd_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root/'server.env').write_text('ASWIRED_LISTEN="127.0.0.1:12890"\nASWIRED_SECRET=not-an-endpoint\n')
            (root/'komari.env').write_text("KOMARI_LISTEN='[::]:25775'\n")
            (root/'web.env').write_text('HOST=0.0.0.0\nPORT="13000"\nIGNORED=$(never-execute)\n')
            with patch.object(health,'ENV',root):
                self.assertEqual(health.endpoints(), {
                    'aswired-server':'http://127.0.0.1:12890/healthz',
                    'komari':'http://[::1]:25775/api/version',
                    'aswired-web':'http://127.0.0.1:13000/',
                })
            self.assertEqual(health.environment(root/'web.env',{'PORT'}),{'PORT':'13000'})

    def test_no_shell_expansion_or_arbitrary_url(self):
        for value in ['localhost:3000;id','http://host:3000','host:3000','127.0.0.1:0','[::1]:65536']:
            with self.assertRaises(health.VerificationError):health.listen(value)
        self.assertEqual(health.listen(':3000'),'http://127.0.0.1:3000')
        self.assertEqual(health.listen('[::1]:3000'),'http://[::1]:3000')

    def test_current_version_file_alone_does_not_prove_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);(root/'VERSION').write_text('v1.0.6')
            with patch.object(health,'CURRENT',root),patch.object(health,'running_release',side_effect=health.VerificationError('old process')):
                with self.assertRaisesRegex(health.VerificationError,'old process'):health.verify('v1.0.6',0)
                with self.assertRaises(health.VerificationError):health.verify('v1.0.7',0)

    def test_actual_http_responses_require_target_version_and_html(self):
        payload={'status':'ok','version':'v1.0.6'}
        code=200; content_type='application/json';redirect=''
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(code);self.send_header('Content-Type',content_type)
                if redirect:self.send_header('Location',redirect)
                self.end_headers();self.wfile.write(json.dumps(payload).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}/'
        try:
            health.check_http('aswired-server',url,'v1.0.6')
            with self.assertRaises(health.VerificationError):health.check_http('aswired-server',url,'v1.0.7')
            payload={'status':'success','data':{'version':'1.2.5-fix2-aswired.1.0.6'}}
            health.check_http('komari',url,'v1.0.6')
            content_type='text/html';health.check_http('aswired-web',url,'v1.0.6')
            code=503
            with self.assertRaises(health.VerificationError):health.check_http('aswired-web',url,'v1.0.6')
            code=302;redirect=url
            with self.assertRaises(health.VerificationError):health.check_http('aswired-web',url,'v1.0.6')
        finally:
            server.shutdown();thread.join();server.server_close()


if __name__=='__main__':unittest.main()
