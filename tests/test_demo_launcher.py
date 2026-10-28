"""Portable launcher dispatch; fake processes never start a network or server."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class DemoLauncherTests(unittest.TestCase):
    def run_launcher(self,root,override=None,venv=False,fail_import=False):
        (root/'scripts').mkdir();shutil.copyfile(ROOT/'scripts/start_demo.sh',root/'scripts/start_demo.sh')
        (root/'frontend/node_modules').mkdir(parents=True)
        tools=root/'tools';tools.mkdir()
        fake='#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$DEMO_CAPTURE"\nif [[ "${1:-}" == "-c" && "${FAIL_IMPORT:-0}" == "1" ]]; then exit 1; fi\n'
        for name in ('python3','npm','custom-python'):
            (tools/name).write_text(fake);(tools/name).chmod(0o755)
        if venv:
            (root/'.venv/bin').mkdir(parents=True);shutil.copyfile(tools/'python3',root/'.venv/bin/python');(root/'.venv/bin/python').chmod(0o755)
        capture=root/'commands';environment=dict(os.environ,PATH=f'{tools}:/usr/bin:/bin',HOME=str(root/'empty-home'),DEMO_CAPTURE=str(capture),FAIL_IMPORT='1' if fail_import else '0')
        environment.pop('FLOWRIDER_PYTHON',None)
        if override:environment['FLOWRIDER_PYTHON']=str(tools/override)
        result=subprocess.run(['bash',str(root/'scripts/start_demo.sh'),'replay','--port','8123'],env=environment,capture_output=True,text=True)
        return result,capture.read_text() if capture.exists() else ''

    def test_path_python_needs_no_pyenv_mininet_or_sudo(self):
        with tempfile.TemporaryDirectory() as directory:
            result,commands=self.run_launcher(Path(directory))
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('python3 demo_server.py --mode replay --port 8123',commands)
            self.assertNotIn('sudo',commands);self.assertNotIn('mininet',commands)

    def test_local_venv_and_explicit_override_are_supported(self):
        for override in (None,'custom-python'):
            with self.subTest(override=override),tempfile.TemporaryDirectory() as directory:
                result,commands=self.run_launcher(Path(directory),override=override,venv=True)
                self.assertEqual(result.returncode,0,result.stderr)
                expected='custom-python' if override else '.venv/bin/python'
                self.assertIn(expected+' demo_server.py',commands)

    def test_missing_dependency_has_install_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            result,_=self.run_launcher(Path(directory),fail_import=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('requirements-demo.txt',result.stderr)


if __name__=='__main__':unittest.main()
