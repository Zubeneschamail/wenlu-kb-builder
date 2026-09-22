import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from kbtool.wenlu_import import import_package, launcher_command


class WenluImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.package = self.folder / 'customer.wlkb'
        self.package.write_bytes(b'fixture validated separately by core tests')
        self.endpoint = {'protocol': 1, 'port': 12345, 'token': 'x' * 43}
        (self.folder / 'knowledge-import.json').write_text(json.dumps(self.endpoint), encoding='utf-8')

    def client(self, handler):
        original = httpx.Client
        return patch('kbtool.wenlu_import.httpx.Client', side_effect=lambda **kwargs:
                     original(transport=httpx.MockTransport(handler), **kwargs))

    def test_running_wenlu_requires_success_acknowledgement(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.endpoint['token'])
            if request.url.path == '/health':
                return httpx.Response(200, json={'ok': True, 'protocol': 1})
            self.assertEqual(json.loads(request.content)['path'], str(self.package.resolve()))
            return httpx.Response(200, json={'ok': True, 'path': 'managed.wlkb', 'name': '客户资料'})
        with self.client(handle), patch('kbtool.wenlu_import.inspect_package'), \
                patch('kbtool.wenlu_import.subprocess.Popen') as launch:
            result = import_package(self.package, data=self.folder, progress=lambda _: None)
        self.assertEqual(result['name'], '客户资料')
        self.assertEqual(calls, ['/health', '/import'])
        launch.assert_not_called()

    def test_receiver_rejection_is_not_reported_as_success(self):
        def handle(request):
            return httpx.Response(200, json={'ok': True, 'protocol': 1} if request.url.path == '/health'
                                  else {'ok': False, 'error': '不能混用不同客户的知识包'})
        with self.client(handle), patch('kbtool.wenlu_import.inspect_package'), self.assertRaisesRegex(ValueError, '不同客户'):
            import_package(self.package, data=self.folder, progress=lambda _: None)

    def test_starts_closed_app_then_hands_off(self):
        count = 0
        def handle(request):
            nonlocal count
            if request.url.path == '/health':
                count += 1
                if count == 1:
                    raise httpx.ConnectError('not running')
                return httpx.Response(200, json={'ok': True, 'protocol': 1})
            return httpx.Response(200, json={'ok': True, 'name': '资料', 'path': 'managed.wlkb'})
        with self.client(handle), patch('kbtool.wenlu_import.inspect_package'), \
                patch('kbtool.wenlu_import.find_launcher', return_value=['C:/Program Files/Wenlu/Wenlu.exe']), \
                patch('kbtool.wenlu_import.subprocess.Popen') as launch:
            import_package(self.package, data=self.folder, progress=lambda _: None)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0], ['C:/Program Files/Wenlu/Wenlu.exe'])

    def test_corrupt_package_never_contacts_or_starts_app(self):
        with patch('kbtool.wenlu_import.httpx.Client') as client, \
                patch('kbtool.wenlu_import.subprocess.Popen') as launch, self.assertRaises(Exception):
            import_package(self.package, data=self.folder)
        client.assert_not_called()
        launch.assert_not_called()

    def test_only_supported_launch_targets(self):
        for name in ('cmd.exe', 'powershell.exe', 'unknown.py'):
            target = self.folder / name
            target.touch()
            with self.assertRaises(ValueError):
                launcher_command(target)
