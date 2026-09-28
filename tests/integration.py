"""Teste Docker opt-in: python3 tests/integration.py (somente recursos edge-it-*)."""
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge_tool.app import Edge
from edge_tool.core import EdgeError


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class DockerIntegration(unittest.TestCase):
    def test_two_generic_projects_and_gateway(self):
        with tempfile.TemporaryDirectory(prefix='edge-it-') as temporary:
            base = Path(temporary)
            token = uuid.uuid4().hex[:10]
            network = f'edge-it-{token}'
            edge = Edge(base / 'state')
            http_port, https_port = free_port(), free_port()
            while https_port == http_port:
                https_port = free_port()
            projects = []
            try:
                edge.init(network=network, compose_name=f'edge-it-{token}', http_port=http_port,
                          https_port=https_port, certificates_dir=base / 'certificates')
                cert, key = base / 'certificate.pem', base / 'private.key'
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                '-keyout', str(key), '-out', str(cert), '-days', '2',
                                '-subj', '/CN=one.edge.example', '-addext',
                                'subjectAltName=DNS:one.edge.example,DNS:two.edge.example'],
                               check=True, capture_output=True)
                edge.cert_import('test-cert', cert, key)
                edge.gateway_operation('up')
                for name in ['one', 'two']:
                    directory = base / name
                    directory.mkdir()
                    server = directory / 'default.conf'
                    server.write_text(f'server {{ listen 8080; location / {{ return 200 "{name}"; }} }}\n')
                    compose = directory / 'compose.yaml'
                    alias = f'edge-it-{name}-{token}'
                    compose.write_text(json.dumps({
                        'name': alias,
                        'services': {
                            'web': {'image': 'nginx:1.27.5-alpine', 'volumes': [f'{server}:/etc/nginx/conf.d/default.conf:ro'],
                                    'networks': {'edge': {'aliases': [alias]}}},
                            'maintenance': {'image': 'nginx:1.27.5-alpine', 'profiles': ['maintenance']},
                        },
                        'networks': {'edge': {'external': True, 'name': network}},
                    }))
                    project = {'version': 1, 'id': name, 'directory': str(directory),
                               'compose_files': [str(compose)], 'env_file': None, 'services': ['web'],
                               'routes': [{'domain': f'{name}.edge.example', 'upstream': f'{alias}:8080', 'certificate': 'test-cert'}]}
                    projects.append(project)
                    edge.project_save(project)
                    edge.project_operation(name, 'up')
                    edge.route_apply(name)
                time.sleep(1)
                for name in ['one', 'two']:
                    connection = http.client.HTTPSConnection('127.0.0.1', https_port, context=ssl._create_unverified_context(), timeout=10)
                    connection.request('GET', '/', headers={'Host': f'{name}.edge.example'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read().decode(), name)
                    connection.close()
                    connection = http.client.HTTPConnection('127.0.0.1', http_port, timeout=10)
                    connection.request('GET', '/path?q=1', headers={'Host': f'{name}.edge.example'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 301)
                    self.assertEqual(response.getheader('Location'), f'https://{name}.edge.example:{https_port}/path?q=1')
                    response.read()
                    connection.close()
                self.assertTrue(edge.validate()['ok'])
                rows = edge.list_projects()
                managed = [row for row in rows if row.get('id') in ['one', 'two']]
                self.assertEqual(len(managed), 2)
                self.assertTrue(all(len(row['containers']) == 1 for row in managed))
                self.assertTrue(all(row['containers'][0]['service'] == 'web' for row in managed))
                edge.project_operation('one', 'stop')
                self.assertFalse(edge.validate('one')['ok'])
                edge.project_operation('one', 'up')
                edge.project_operation('two', 'restart')
                site = edge.store.root / 'sites/one.conf'
                before = site.read_bytes()
                # A real Nginx rejection must restore the previously active route.
                original = __import__('edge_tool.app', fromlist=['render_site']).render_site
                from unittest.mock import patch
                with patch('edge_tool.app.render_site', side_effect=lambda project, config: original(project, config) + '\ninvalid_directive;\n'):
                    with self.assertRaises(EdgeError):
                        edge.route_apply('one')
                self.assertEqual(site.read_bytes(), before)
                new_https = free_port()
                while new_https in [http_port, https_port]:
                    new_https = free_port()
                edge.configure(https_port=new_https)
                time.sleep(1)
                connection = http.client.HTTPSConnection('127.0.0.1', new_https, context=ssl._create_unverified_context(), timeout=10)
                connection.request('GET', '/', headers={'Host': 'one.edge.example'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
                edge.project_remove('two')
                with self.assertRaises(EdgeError):
                    edge.cert_remove('test-cert')
                edge.project_remove('one')
                edge.cert_remove('test-cert')
                self.assertEqual(edge.cert_list(), [])
            finally:
                for project in projects:
                    edge.docker.compose(project, 'down', sensitive=True)
                if (edge.store.root / 'compose.yaml').exists():
                    edge.docker.compose(edge.gateway_project(), 'down', sensitive=True)
                subprocess.run(['docker', 'network', 'rm', network], capture_output=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
