import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallTest(unittest.TestCase):
    def test_install_and_upgrade_preserve_working_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            for _ in range(2):
                result = subprocess.run(['sh', 'install.sh', '--prefix', str(prefix)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([str(prefix / 'bin/edge'), '--help'], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('gateway', result.stdout)

    def test_install_refuses_to_overwrite_unrelated_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / 'bin').mkdir()
            command = prefix / 'bin/edge'
            command.write_text('unrelated')
            result = subprocess.run(['sh', 'install.sh', '--prefix', str(prefix)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(command.read_text(), 'unrelated')


if __name__ == '__main__':
    unittest.main()
