import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from edge_tool.app import Edge
from edge_tool.core import EdgeError, Store, parse_json_rows
from edge_tool.nginx import render_site


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.fail_reload = False
        self.fail_test = False
        self.rows = []
        self.model = {
            'name': 'sample',
            'services': {
                'web': {'networks': {'edge': {'aliases': ['sample-web']}}},
                'maintenance': {'profiles': ['maintenance']},
            },
            'networks': {'edge': {'external': True, 'name': 'web_edge'}},
        }

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[-2:] == ['-s', 'reload'] and self.fail_reload:
            self.fail_reload = False
            raise EdgeError('reload rejeitado')
        if args[-1:] == ['-t'] and self.fail_test:
            raise EdgeError('configuração rejeitada')
        if 'inspect' in args and 'network' in args:
            return json.dumps([{'Containers': {}}])
        if 'ps' in args and '--format' in args:
            return json.dumps(self.rows)
        if 'config' in args:
            return json.dumps(self.model)
        return ''

    def compose(self, project, *args, **kwargs):
        return self.run(['docker', 'compose', *args], **kwargs)

    def containers(self):
        return []

    def prerequisites(self):
        pass


class EdgeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_tmp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.cert_tmp.name) / 'cert.pem'
        cls.key = Path(cls.cert_tmp.name) / 'key.pem'
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '2',
            '-subj', '/CN=sample.example', '-addext',
            'subjectAltName=DNS:sample.example,DNS:other.example',
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.cert_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.directory = Path(self.tmp.name) / 'app'
        self.directory.mkdir()
        (self.directory / 'compose.yaml').write_text('services: {}\n')
        self.docker = FakeDocker()
        self.edge = Edge(self.root, self.docker)
        self.edge.init(certificates_dir=Path(self.tmp.name) / 'certificates')
        self.edge.cert_import('shared', self.cert, self.key)
        self.project = {
            'version': 1, 'id': 'sample', 'directory': str(self.directory),
            'compose_files': [str(self.directory / 'compose.yaml')],
            'env_file': None, 'services': ['web'],
            'routes': [{'domain': 'sample.example', 'upstream': 'sample-web:8080',
                        'certificate': 'shared'}],
        }

    def add(self):
        self.edge.project_save(copy.deepcopy(self.project))

    def gateway_running(self):
        self.docker.rows = [{'Service': 'nginx', 'State': 'running', 'Health': ''}]

class EdgeTest(EdgeFixture):
    def test_registration_is_generic_and_does_not_publish(self):
        self.add()
        self.assertEqual(self.edge.store.project('sample'), self.project)
        self.assertFalse((self.root / 'sites' / 'sample.conf').exists())
        self.assertEqual(os.stat(self.root / 'projects' / 'sample.json').st_mode & 0o777, 0o600)

    def test_duplicate_domain_is_rejected_without_changing_registry(self):
        self.add()
        self.project['id'] = 'another'
        with self.assertRaises(EdgeError):
            self.edge.project_save(self.project)
        self.assertEqual(len(self.edge.store.projects()), 1)

    def test_invalid_ids_domains_upstreams_and_ports_are_rejected(self):
        for field, value in [('id', '../escape'), ('domain', 'x;return 200;'),
                             ('upstream', 'web:99999'), ('upstream', 'web:80/path')]:
            with self.subTest(field=field):
                project = copy.deepcopy(self.project)
                if field == 'id':
                    project['id'] = value
                else:
                    project['routes'][0][field] = value
                with self.assertRaises(EdgeError):
                    self.edge.project_save(project)
        for port in [0, 65536, True]:
            with self.assertRaises(EdgeError):
                self.edge.configure(http_port=port)

    def test_compose_services_profiles_network_and_alias_are_checked(self):
        for change in ['missing', 'profile', 'network', 'alias']:
            with self.subTest(change=change):
                model = copy.deepcopy(self.docker.model)
                project = copy.deepcopy(self.project)
                if change == 'missing':
                    project['services'] = ['unknown']
                elif change == 'profile':
                    project['services'] = ['maintenance']
                elif change == 'network':
                    model['networks']['edge']['external'] = False
                else:
                    project['routes'][0]['upstream'] = 'unknown:8080'
                old = self.docker.model
                self.docker.model = model
                try:
                    with self.assertRaises(EdgeError):
                        self.edge.project_save(project)
                finally:
                    self.docker.model = old

    def test_failed_test_restores_previous_site(self):
        self.add()
        self.gateway_running()
        site = self.root / 'sites' / 'sample.conf'
        site.write_text('previous')
        self.docker.fail_test = True
        with self.assertRaises(EdgeError):
            self.edge.route_apply('sample')
        self.assertEqual(site.read_text(), 'previous')
        self.assertFalse(any(call[-2:] == ['-s', 'reload'] for call in self.docker.calls))

    def test_failed_reload_restores_site_and_attempts_recovery(self):
        self.add()
        self.gateway_running()
        site = self.root / 'sites' / 'sample.conf'
        site.write_text('previous')
        self.docker.fail_reload = True
        with self.assertRaises(EdgeError):
            self.edge.route_apply('sample')
        self.assertEqual(site.read_text(), 'previous')
        self.assertEqual(sum(call[-2:] == ['-s', 'reload'] for call in self.docker.calls), 2)

    def test_failed_edit_keeps_previous_registration(self):
        self.add()
        self.gateway_running()
        self.edge.route_apply('sample')
        self.project['routes'][0]['domain'] = 'other.example'
        self.docker.fail_test = True
        with self.assertRaises(EdgeError):
            self.edge.project_save(self.project, edit=True)
        self.assertEqual(self.edge.store.project('sample')['routes'][0]['domain'], 'sample.example')

    def test_certificate_in_use_cannot_be_removed(self):
        self.add()
        with self.assertRaises(EdgeError):
            self.edge.cert_remove('shared')

    def test_certificate_hostname_and_key_match_are_enforced(self):
        bad = copy.deepcopy(self.project)
        bad['routes'][0]['domain'] = 'uncovered.example'
        with self.assertRaises(EdgeError):
            self.edge.project_save(bad)
        other_key = Path(self.tmp.name) / 'other.key'
        subprocess.run(['openssl', 'genrsa', '-out', str(other_key), '2048'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self.assertRaises(EdgeError):
            self.edge.cert_import('bad', self.cert, other_key)
        self.assertFalse((Path(self.edge.store.config()['certificates_dir']) / 'bad').exists())
        key = Path(self.edge.store.config()['certificates_dir']) / 'shared' / 'private.key'
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_project_operations_only_target_registered_services(self):
        self.add()
        self.edge.project_operation('sample', 'up')
        command = self.docker.calls[-1]
        self.assertIn('--no-build', command)
        self.assertIn('--no-deps', command)
        self.assertEqual(command[-1], 'web')
        self.assertNotIn('maintenance', command)
        self.edge.project_operation('sample', 'stop')
        self.assertEqual(self.docker.calls[-1][-2:], ['stop', 'web'])

    def test_runtime_validation_reports_stopped_services(self):
        self.add()
        result = self.edge.validate('sample')
        self.assertTrue(result['configuration_valid'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['projects'][0]['running'])

    def test_static_validation_can_preflight_before_first_start(self):
        self.add()
        self.gateway_running()
        result = self.edge.validate('sample', configuration_only=True, require_gateway=True,
                                    directory=self.directory)
        self.assertTrue(result['ok'])
        with self.assertRaises(EdgeError):
            self.edge.validate('sample', configuration_only=True, directory='/wrong/checkout')

    def test_json_lines_and_arrays_are_supported(self):
        rows = [{'State': 'running'}, {'State': 'exited'}]
        self.assertEqual(parse_json_rows(json.dumps(rows)), rows)
        self.assertEqual(parse_json_rows('\n'.join(map(json.dumps, rows))), rows)

    def test_nginx_respects_custom_https_port_and_dynamic_dns(self):
        site = render_site(self.project, {'https_port': 8443})
        self.assertIn('https://sample.example:8443$request_uri', site)
        self.assertIn('X-Forwarded-Port 8443;', site)
        self.assertIn('proxy_pass http://$upstream;', site)
        self.assertIn('sample-web:8080', site)
        self.assertNotIn('botdivulga', site)


if __name__ == '__main__':
    unittest.main()
