"""Wizard completion status must describe the committed operation, not its preview."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'xray-manager.sh'
BASH = shutil.which('bash')
if BASH is None and os.name == 'nt':
    candidate = Path('D:/Program Files/Git/bin/bash.exe')
    if candidate.is_file():
        BASH = str(candidate)

DRIVER = r'''
source "$1"
STATE_DIR=$2
export_status=$3
save_status=$4
CORE_BIN=echo
require_core() { return 0; }
safe_paths() { return 0; }
choose_template() { printf -v "$1" vless-tcp; }
read_default_server() { printf '%s' example.com; }
prompt() { printf -v "$1" '%s' "${3:-}"; }
prompt_secret() { printf -v "$1" '%s' "${3:-}"; }
confirm() { return 0; }
generate_inbound() { printf '%s\n' '{"tag":"fixture"}'; }
add_managed_inbound() {
    [[ $save_status == 0 ]] || return "$save_status"
    cp -- "$1" "$STATE_DIR/committed.json"
}
show_client() {
    : >"$STATE_DIR/export-attempted"
    return "$export_status"
}
node_wizard_locked
'''


@unittest.skipUnless(BASH, 'Bash is required')
class XrayWizardStatusTest(unittest.TestCase):
    def invoke(self, export_status, save_status=0):
        with tempfile.TemporaryDirectory(prefix='xray-wizard-status-') as temporary:
            root = Path(temporary)
            result = subprocess.run(
                [BASH, '-c', DRIVER, 'wizard-status', SCRIPT.as_posix(), root.as_posix(),
                 str(export_status), str(save_status)],
                capture_output=True, text=True, encoding='utf-8', timeout=20,
            )
            return result, (root / 'committed.json').is_file(), (root / 'export-attempted').is_file()

    def test_committed_add_is_success_even_when_preview_fails(self):
        result, committed, exported = self.invoke(1)
        self.assertTrue(committed, 'fixture must reach the committed-state boundary')
        self.assertTrue(exported)
        self.assertEqual(result.returncode, 0, 'preview failure incorrectly reports a committed add as failed')
        self.assertIn('配置已保存', result.stdout)
        self.assertIn('不要重复添加', result.stderr)

    def test_successful_add_and_preview_succeed(self):
        result, committed, exported = self.invoke(0)
        self.assertTrue(committed and exported)
        self.assertEqual(result.returncode, 0)

    def test_failed_save_stays_failure_and_does_not_preview(self):
        result, committed, exported = self.invoke(1, 1)
        self.assertFalse(committed or exported)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('配置已保存', result.stdout)


if __name__ == '__main__':
    unittest.main()
