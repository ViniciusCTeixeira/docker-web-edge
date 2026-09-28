import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from edge_tool.cli import main
from test_edge import EdgeFixture


class CliTest(EdgeFixture):
    def test_help_is_available_without_docker_or_state(self):
        result = subprocess.run(['python3', 'bin/edge', '--help'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('wizard', result.stdout)
        self.assertIn('validate', result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_add_show_and_validation_use_machine_readable_output(self):
        with patch('edge_tool.cli.Edge', return_value=self.edge), patch('builtins.print') as output:
            self.assertEqual(main(['project', 'add', 'sample', '--directory', str(self.directory),
                                   '--service', 'web', '--route', 'sample.example', 'sample-web:8080',
                                   'shared', '--json', '--non-interactive']), 0)
            self.assertEqual(json.loads(output.call_args.args[0])['id'], 'sample')
            self.assertEqual(main(['project', 'show', 'sample', '--json']), 0)
            self.assertEqual(json.loads(output.call_args.args[0])['routes'][0]['certificate'], 'shared')
            self.assertEqual(main(['validate', 'sample', '--configuration-only', '--json']), 0)
            self.assertEqual(main(['validate', 'sample', '--json']), 1)

    def test_error_output_has_no_traceback_or_secret(self):
        self.docker.run = lambda *a, **kw: (_ for _ in ()).throw(Exception('secret'))
        with patch('edge_tool.cli.Edge', return_value=self.edge), patch('builtins.print') as output:
            self.assertEqual(main(['project', 'show', 'missing', '--json']), 1)
            message = output.call_args.args[0]
            self.assertNotIn('Traceback', message)
            self.assertNotIn('secret', message)

    def test_wizard_uses_project_registration(self):
        answers = ['sample', str(self.directory), 'compose.yaml', '', 'web',
                   'sample.example', 'sample-web:8080', 'shared', 'n', 's']
        with patch('edge_tool.cli.Edge', return_value=self.edge), patch('builtins.input', side_effect=answers), patch('builtins.print'):
            self.assertEqual(main(['wizard']), 0)
        self.assertEqual(self.edge.store.project('sample')['services'], ['web'])


if __name__ == '__main__':
    unittest.main()
