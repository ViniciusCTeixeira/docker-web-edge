import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from edge_tool.core import EdgeError, run
from edge_tool.docker import Docker, available_ports
from test_edge import EdgeFixture


class OperationsTest(EdgeFixture):
    def test_new_route_is_removed_when_reload_fails(self):
        self.add()
        self.gateway_running()
        self.docker.fail_reload = True
        with self.assertRaises(EdgeError):
            self.edge.route_apply('sample')
        self.assertFalse((self.root / 'sites' / 'sample.conf').exists())
        self.assertEqual(sum(call[-2:] == ['-s', 'reload'] for call in self.docker.calls), 2)

    def test_failed_removal_restores_site_and_registration(self):
        self.add()
        self.gateway_running()
        self.edge.route_apply('sample')
        site = self.root / 'sites' / 'sample.conf'
        before = site.read_text()
        self.docker.fail_reload = True
        with self.assertRaises(EdgeError):
            self.edge.project_remove('sample')
        self.assertEqual(site.read_text(), before)
        self.assertEqual(self.edge.store.project('sample'), self.project)

    def test_invalid_certificate_replacement_preserves_original(self):
        self.add()
        config = self.edge.store.config()
        cert_path = Path(config['certificates_dir']) / 'shared/certificate.pem'
        previous = cert_path.read_bytes()
        wrong = Path(self.tmp.name) / 'wrong.pem'
        wrong.write_text('invalid secret PEM')
        with self.assertRaises(EdgeError):
            self.edge.cert_import('shared', wrong, self.key)
        self.assertEqual(cert_path.read_bytes(), previous)

    def test_compose_failure_does_not_expose_stderr_secrets(self):
        result = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'password=very-secret')
        with patch('subprocess.run', return_value=result):
            with self.assertRaises(EdgeError) as caught:
                run(['docker', 'compose', 'config'], sensitive=True)
        self.assertNotIn('very-secret', str(caught.exception))

    def test_compose_command_preserves_file_order_and_literal_arguments(self):
        docker = Docker()
        project = {'directory': '/tmp/project with spaces', 'compose_files': ['/tmp/base.yaml', '/tmp/override.yaml'],
                   'env_file': '/tmp/settings.env'}
        with patch.object(docker, 'run', return_value='') as runner:
            docker.compose(project, 'stop', 'web')
        self.assertEqual(runner.call_args.args[0], ['docker', 'compose', '--project-directory', '/tmp/project with spaces',
                                                  '--env-file', '/tmp/settings.env', '-f', '/tmp/base.yaml',
                                                  '-f', '/tmp/override.yaml', 'stop', 'web'])

    def test_port_conflict_names_only_the_container(self):
        container = {'name': '/another', 'labels': {}, 'state': {'Running': True},
                     'ports': {'80/tcp': [{'HostPort': '80'}]}}
        with self.assertRaises(EdgeError):
            available_ports(self.edge.store.config(), [container], 'edge')

    def test_registered_and_unregistered_projects_are_listed_from_containers(self):
        self.add()
        self.docker.containers = lambda: [
            {'name': '/sample-web-1', 'labels': {'com.docker.compose.project': 'sample', 'com.docker.compose.service': 'web'},
             'state': {'Status': 'running'}, 'ports': {}},
            {'name': '/extra-web-1', 'labels': {'com.docker.compose.project': 'extra', 'com.docker.compose.service': 'web'},
             'state': {'Status': 'exited'}, 'ports': {}},
        ]
        rows = self.edge.list_projects()
        self.assertTrue(rows[0]['registered'])
        self.assertEqual(rows[0]['containers'][0]['status'], 'running')
        self.assertFalse(rows[1]['registered'])
        with self.assertRaises(EdgeError):
            self.edge.project_operation('extra', 'up')

    def test_configuration_rollback_restores_ports_and_site(self):
        self.add()
        self.gateway_running()
        self.edge.route_apply('sample')
        old = self.edge.store.config()
        site = self.root / 'sites' / 'sample.conf'
        previous = site.read_text()
        def fail_once(project, *args, **kwargs):
            if args[:3] == ('up', '-d', '--wait'):
                self.docker.compose = lambda project, *a, **kw: self.docker.run(['docker', 'compose', *a], **kw)
                raise EdgeError('startup falhou')
            return self.docker.run(['docker', 'compose', *args], **kwargs)
        self.docker.compose = fail_once
        with patch('edge_tool.app.available_ports'):
            with self.assertRaises(EdgeError):
                self.edge.configure(https_port=8443)
        self.assertEqual(self.edge.store.config(), old)
        self.assertEqual(site.read_text(), previous)


if __name__ == '__main__':
    unittest.main()

class ConfigurationTest(EdgeFixture):
    def test_init_is_idempotent_with_unspecified_cli_options(self):
        previous = self.edge.store.config()
        result = self.edge.init(network=None, http_port=None, https_port=None, certificates_dir=None,
                                image=None, compose_name=None)
        self.assertEqual(result, previous)

    def test_validation_output_does_not_expose_container_command(self):
        self.add()
        self.docker.rows = [{'Service': 'web', 'State': 'running', 'Health': '', 'Command': 'app --password=secret'}]
        result = self.edge.validate('sample')
        self.assertNotIn('secret', json.dumps(result))

    def test_duplicate_compose_identity_is_rejected(self):
        self.add()
        project = copy.deepcopy(self.project)
        project['id'] = 'second'
        project['routes'][0] = {'domain': 'other.example', 'upstream': 'other-web:8080', 'certificate': 'shared'}
        self.docker.model['services']['web']['networks']['edge']['aliases'].append('other-web')
        with self.assertRaises(EdgeError):
            self.edge.project_save(project)

    def test_configuring_an_unhealthy_running_gateway_still_recreates_it(self):
        self.docker.rows = [{'Service': 'nginx', 'State': 'running', 'Health': 'unhealthy'}]
        with patch('edge_tool.app.available_ports'):
            self.edge.configure(https_port=8443)
        self.assertTrue(any('up' in call and '--wait' in call for call in self.docker.calls))

    def test_gateway_namespace_cannot_be_used_by_application(self):
        self.docker.model['name'] = 'edge'
        with self.assertRaises(EdgeError):
            self.add()

    def test_interrupt_after_route_write_restores_previous_site(self):
        self.add()
        self.gateway_running()
        site = self.root / 'sites/sample.conf'
        site.write_text('previous')
        original = self.edge._nginx
        def interrupt(*args):
            if args == ('-t',):
                raise KeyboardInterrupt()
            return original(*args)
        with patch.object(self.edge, '_nginx', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.edge.route_apply('sample')
        self.assertEqual(site.read_text(), 'previous')

    def test_shared_implicit_alias_cannot_shadow_registered_route(self):
        self.project['routes'][0]['upstream'] = 'web:8080'
        self.add()
        other = copy.deepcopy(self.project)
        other['id'] = 'other'
        other['routes'][0] = {'domain': 'other.example', 'upstream': 'other-web:8080', 'certificate': 'shared'}
        original_compose = self.docker.compose
        other_model = copy.deepcopy(self.docker.model)
        other_model['name'] = 'other'
        other_model['services']['web']['networks']['edge']['aliases'] = ['other-web']
        def compose(project, *args, **kwargs):
            if 'config' in args and project['id'] == 'other':
                return json.dumps(other_model)
            return original_compose(project, *args, **kwargs)
        self.docker.compose = compose
        with self.assertRaises(EdgeError):
            self.edge.project_save(other)

    def test_ambiguous_alias_within_compose_is_rejected(self):
        self.docker.model['services']['another'] = {'networks': {'edge': {'aliases': ['sample-web']}}}
        with self.assertRaises(EdgeError):
            self.add()

    def test_foreign_runtime_alias_is_rejected(self):
        self.docker.containers = lambda: [{'name': '/foreign',
            'labels': {'com.docker.compose.project': 'foreign', 'com.docker.compose.service': 'web'},
            'state': {'Running': True}, 'networks': {'web_edge': {'Aliases': ['sample-web']}}}]
        with self.assertRaises(EdgeError):
            self.add()

class HardeningTest(EdgeFixture):
    def test_certificate_import_installs_validated_snapshot(self):
        cert_data = self.cert.read_bytes()
        from edge_tool.certificates import check_pair
        source = Path(self.tmp.name) / 'source.pem'
        source.write_bytes(cert_data)
        def validate_and_change_source(cert, key, domains):
            result = check_pair(cert, key, domains)
            source.write_text('changed after validation')
            return result
        with patch('edge_tool.app.check_pair', side_effect=validate_and_change_source):
            self.edge.cert_import('snapshot', source, self.key)
        installed = Path(self.edge.store.config()['certificates_dir']) / 'snapshot/certificate.pem'
        self.assertEqual(installed.read_bytes(), cert_data)

    def test_installer_waits_for_an_existing_installation_lock(self):
        import fcntl
        import time
        prefix = Path(self.tmp.name) / 'install'
        (prefix / 'lib').mkdir(parents=True)
        with (prefix / 'lib/.edge-install.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(['sh', 'install.sh', '--prefix', str(prefix)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                time.sleep(0.3)
                self.assertIsNone(process.poll())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_runtime_failure_does_not_erase_static_validity(self):
        self.add()
        original = self.docker.compose
        def unavailable(project, *args, **kwargs):
            if 'ps' in args:
                raise EdgeError('Docker indisponível')
            return original(project, *args, **kwargs)
        self.docker.compose = unavailable
        result = self.edge.validate('sample')
        self.assertTrue(result['configuration_valid'])
        self.assertFalse(result['ok'])

class OwnershipTest(EdgeFixture):
    def test_init_refuses_to_overwrite_an_unmanaged_gateway(self):
        from edge_tool.app import Edge
        other_root = Path(self.tmp.name) / 'unmanaged'
        other_root.mkdir()
        compose = other_root / 'compose.yaml'
        compose.write_text('unrelated gateway')
        with self.assertRaises(EdgeError):
            Edge(other_root, self.docker).init(certificates_dir=Path(self.tmp.name) / 'new-certs')
        self.assertEqual(compose.read_text(), 'unrelated gateway')

    def test_restart_only_targets_registered_services_without_dependencies(self):
        self.add()
        self.edge.project_operation('sample', 'restart')
        self.assertIn('--no-deps', self.docker.calls[-1])
        self.assertEqual(self.docker.calls[-1][-1], 'web')
