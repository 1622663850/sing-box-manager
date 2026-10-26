#!/usr/bin/env python3
"""Focused tests for the opt-in TCP BBR manager operation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import time
import unittest

import test_manager as fixture

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no termios/pty.
    pty = None


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class BbrTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.bbr_dir = self.root / "etc/sysctl.d"
        self.bbr_dir.mkdir(parents=True, exist_ok=True)
        self.bbr_file = self.bbr_dir / "99-sing-box-manager-bbr.conf"
        self.restore_file = self.state_dir / "bbr-active"
        self.bbr_state = self.root / "bbr-state.json"
        self.sysctl_log = self.root / "sysctl.jsonl"
        self.modprobe_log = self.root / "modprobe.jsonl"
        self.default = self.state_dir / "default-server"
        self.default.write_text("example.com\n", encoding="utf-8")
        self.default.chmod(0o600)
        self.clients = self.state_dir / "clients"
        self.clients.mkdir(parents=True, exist_ok=True)
        fixture.dump(self.clients / "node.json", {"server": "example.com", "inbound": {"tag": "node"}})
        (self.clients / "node.json").chmod(0o600)
        fixture.dump(self.bbr_state, {
            "tcp_congestion_control": "cubic",
            "default_qdisc": "fq_codel",
            "available": "reno cubic bbr",
            "fail_write_once": False,
            "fail_cc_once": False,
            "no_effect_once": False,
            "modprobe_fail": False,
        })
        self._install_mock_sysctl()
        self._install_mock_modprobe()
        self.env.update({
            "BBR_MOCK_STATE": str(self.bbr_state),
            "BBR_MOCK_SYSCTL_LOG": str(self.sysctl_log),
            "BBR_MOCK_MODPROBE_LOG": str(self.modprobe_log),
        })

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def _install_mock_sysctl(self) -> None:
        path = self.bin / "sysctl"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "state_path = pathlib.Path(os.environ['BBR_MOCK_STATE'])\n"
            "state = json.loads(state_path.read_text())\n"
            "args = sys.argv[1:]\n"
            "log = pathlib.Path(os.environ['BBR_MOCK_SYSCTL_LOG'])\n"
            "with log.open('a', encoding='utf-8') as file: file.write(json.dumps(args) + '\\n')\n"
            "def kind(key):\n"
            "    if key.endswith('tcp_congestion_control'): return 'tcp_congestion_control'\n"
            "    if key.endswith('default_qdisc'): return 'default_qdisc'\n"
            "    if key.endswith('tcp_available_congestion_control') or key.endswith('tcp_allowed_congestion_control'): return 'available'\n"
            "    return key\n"
            "if '-n' in args:\n"
            "    key = args[args.index('-n') + 1]\n"
            "    print(state.get(kind(key), ''))\n"
            "    raise SystemExit(0)\n"
            "if '-w' in args:\n"
            "    assignments = [item for item in args if '=' in item]\n"
            "    if state.get('fail_write_once'):\n"
            "        state['fail_write_once'] = False; state_path.write_text(json.dumps(state)); raise SystemExit(1)\n"
            "    if state.get('fail_cc_once') and any(kind(item.split('=', 1)[0]) == 'tcp_congestion_control' for item in assignments):\n"
            "        state['fail_cc_once'] = False; state_path.write_text(json.dumps(state)); raise SystemExit(1)\n"
            "    no_effect = bool(state.get('no_effect_once'))\n"
            "    state['no_effect_once'] = False\n"
            "    if not no_effect:\n"
            "        for item in assignments:\n"
            "            key, value = item.split('=', 1); state[kind(key)] = value\n"
            "    state_path.write_text(json.dumps(state)); raise SystemExit(0)\n"
            "if '--system' in args or '-p' in args: raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _install_mock_modprobe(self) -> None:
        path = self.bin / "modprobe"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "state_path = pathlib.Path(os.environ['BBR_MOCK_STATE'])\n"
            "state = json.loads(state_path.read_text())\n"
            "args = sys.argv[1:]\n"
            "log = pathlib.Path(os.environ['BBR_MOCK_MODPROBE_LOG'])\n"
            "with log.open('a', encoding='utf-8') as file: file.write(json.dumps(args) + '\\n')\n"
            "if state.get('modprobe_fail'): raise SystemExit(1)\n"
            "if 'tcp_bbr' in args and 'bbr' not in state['available'].split(): state['available'] += ' bbr'\n"
            "state_path.write_text(json.dumps(state)); raise SystemExit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _bbr_state(self, **updates: object) -> dict:
        state = json.loads(self.bbr_state.read_text(encoding="utf-8"))
        if updates:
            state.update(updates)
            fixture.dump(self.bbr_state, state)
        return state

    def _desired_conf(self) -> bytes:
        return (
            b"# Managed by sing-box-manager: TCP BBR\n"
            b"net.core.default_qdisc=fq\n"
            b"net.ipv4.tcp_congestion_control=bbr\n"
        )

    def _snapshot_protected(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for path in (self.config, self.core, self.unit, self.marker, self.default):
            values[str(path)] = path.read_bytes() if path.exists() else None
        clients = self.state_dir / "clients"
        values["clients"] = {
            path.name: path.read_bytes()
            for path in sorted(clients.glob("*.json"))
            if path.is_file() and not path.is_symlink()
        } if clients.exists() else None
        values["service"] = self.mock_state.read_bytes()
        values["systemctl"] = self.system_log.read_bytes() if self.system_log.exists() else None
        state = json.loads(self.bbr_state.read_text(encoding="utf-8"))
        values["sysctl-state"] = tuple(state.get(key) for key in ("tcp_congestion_control", "default_qdisc", "available"))
        values["bbr-file"] = (
            self.bbr_file.read_bytes(), self.bbr_file.stat().st_mode & 0o777
        ) if self.bbr_file.exists() else None
        values["bbr-restore"] = self.restore_file.read_bytes() if self.restore_file.exists() else None
        return values

    def test_status_is_read_only(self) -> None:
        before = self._snapshot_protected()
        result = self.shell("show_bbr_status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cubic", result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertFalse(self.bbr_file.exists())

    def test_enable_writes_runtime_and_persistent_config_with_backup(self) -> None:
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self._bbr_state()
        self.assertEqual(state["tcp_congestion_control"], "bbr")
        self.assertEqual(state["default_qdisc"], "fq")
        self.assertEqual(self.bbr_file.read_bytes(), self._desired_conf())
        self.assertEqual(self.bbr_file.stat().st_mode & 0o777, 0o644)
        backups = sorted((self.state_dir / "backups").glob("bbr-*/runtime.json"))
        self.assertEqual(len(backups), 1)
        runtime = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(runtime, {
            "tcp_congestion_control": "cubic",
            "default_qdisc": "fq_codel",
            "config_existed": False,
            "config_mode": "644",
        })
        after = self._snapshot_protected()
        for key, value in before.items():
            if key not in {"sysctl-state", "bbr-file", "bbr-restore"}:
                self.assertEqual(after[key], value, key)
        self.assertFalse(self.system_log.exists())

    def _enable_for_restore(self) -> bytes:
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reference = self.restore_file.read_bytes()
        self.assertEqual(self.restore_file.stat().st_mode & 0o777, 0o600)
        return reference

    def test_disable_restores_runtime_and_removes_new_persistence(self) -> None:
        before = self._snapshot_protected()
        self._enable_for_restore()
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertFalse(self.restore_file.exists())
        self.assertEqual(len(list((self.state_dir / 'backups').glob('bbr-*/runtime.json'))), 2)
        self.assertFalse(self.system_log.exists())

    def test_disable_restores_original_file_bytes_and_mode(self) -> None:
        original = b'# Managed by sing-box-manager: TCP BBR\n# previous setting\nnet.core.default_qdisc=fq_codel\nnet.ipv4.tcp_congestion_control=cubic\n'
        self.bbr_file.write_bytes(original)
        self.bbr_file.chmod(0o600)
        before = self._snapshot_protected()
        self._enable_for_restore()
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)

    def test_disable_preserves_preexisting_bbr_and_unrelated_sysctl(self) -> None:
        self._bbr_state(tcp_congestion_control='bbr', default_qdisc='cake')
        external = self.bbr_dir / '50-external.conf'
        external.write_text('net.ipv4.tcp_congestion_control=bbr\nnet.core.default_qdisc=cake\n')
        before = self._snapshot_protected()
        self._enable_for_restore()
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('恢复后仍为 bbr', result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertEqual(external.read_text(), 'net.ipv4.tcp_congestion_control=bbr\nnet.core.default_qdisc=cake\n')

    def test_cancel_disable_and_repeated_enable_keep_original_reference(self) -> None:
        reference = self._enable_for_restore()
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 1; }; disable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)
        self._enable_for_restore()
        self.assertEqual(self.restore_file.read_bytes(), reference)
        self.assertEqual(len(list((self.state_dir / 'backups').glob('bbr-*/runtime.json'))), 1)
        result = self.shell('confirm() { return 0; }; disable_bbr_locked; enable_bbr_locked; disable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._bbr_state()['tcp_congestion_control'], 'cubic')
        self.assertFalse(self.restore_file.exists())

    def test_disable_without_record_refuses_to_guess_including_legacy_files(self) -> None:
        self._bbr_state(tcp_congestion_control='bbr', default_qdisc='fq')
        self.bbr_file.write_bytes(self._desired_conf())
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('没有本面板开启 BBR 前的恢复记录', result.stdout + result.stderr)
        self.assertEqual(self._snapshot_protected(), before)

    def test_disable_failure_restores_enabled_state_and_reference(self) -> None:
        self._enable_for_restore()
        before = self._snapshot_protected()
        for flag in ('fail_write_once', 'fail_cc_once', 'no_effect_once'):
            with self.subTest(flag=flag):
                self._bbr_state(**{flag: True})
                result = self.shell('confirm() { return 0; }; disable_bbr_locked')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._snapshot_protected(), before)

    def test_disable_removal_failure_after_unlink_rolls_back(self) -> None:
        self._enable_for_restore()
        before = self._snapshot_protected()
        for variable in ('BBR_SYSCTL_FILE', 'BBR_RESTORE_FILE'):
            with self.subTest(variable=variable):
                body = 'BBR_TEST_FAIL_PATH=$' + variable + r'''
rm() { command rm "$@"; local result=$?; if [[ ${*: -1} == "$BBR_TEST_FAIL_PATH" ]]; then return 1; fi; return "$result"; }
confirm() { return 0; }; disable_bbr_locked
'''
                result = self.shell(body)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._snapshot_protected(), before)

    def test_disable_term_after_restore_rolls_back(self) -> None:
        self._enable_for_restore()
        before = self._snapshot_protected()
        result = self.shell(r'''
rm() { command rm "$@"; if [[ ${*: -1} == "$BBR_RESTORE_FILE" ]]; then kill -TERM "$BASHPID"; fi; }
confirm() { return 0; }; disable_bbr_locked
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)

    def test_enable_restore_pointer_write_failure_rolls_back(self) -> None:
        before = self._snapshot_protected()
        result = self.shell(r'''
eval "$(declare -f atomic_copy | sed '1s/^atomic_copy/real_atomic_copy/')"
atomic_copy() { real_atomic_copy "$@"; if [[ $2 == "$BBR_RESTORE_FILE" ]]; then return 1; fi; }
confirm() { return 0; }; enable_bbr_locked
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)

    def test_disable_rejects_missing_corrupt_or_symlink_backup(self) -> None:
        reference = self._enable_for_restore()
        backup = self.state_dir / 'backups' / reference.decode().strip()
        runtime = backup / 'runtime.json'
        original = runtime.read_bytes()
        for bad in (b'{', b'{}', b'{"tcp_congestion_control":"bad value","default_qdisc":"fq","config_existed":false,"config_mode":"644"}'):
            with self.subTest(bad=bad):
                runtime.write_bytes(bad)
                before = self._snapshot_protected()
                result = self.shell('confirm() { return 0; }; disable_bbr_locked')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._snapshot_protected(), before)
        runtime.write_bytes(original)
        self.restore_file.write_text('../outside')
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.restore_file.unlink()
        self.restore_file.symlink_to(runtime)
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.restore_file.unlink()
        self.restore_file.write_bytes(reference)
        runtime.unlink()
        result = self.shell('confirm() { return 0; }; disable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)

    def test_disable_confirmation_change_refuses_without_overwrite(self) -> None:
        self._enable_for_restore()
        before = self._snapshot_protected()
        result = self.shell(r'''
confirm() { printf '# foreign update\n' > "$BBR_SYSCTL_FILE"; return 0; }; disable_bbr_locked
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.bbr_file.read_bytes(), b'# foreign update\n')
        after = self._snapshot_protected()
        for key in before:
            if key != 'bbr-file':
                self.assertEqual(after[key], before[key])

    def test_disable_rejects_corrupt_original_config_backup(self) -> None:
        self.bbr_file.write_bytes(b'# Managed by sing-box-manager: TCP BBR\nnet.core.default_qdisc=fq_codel\n')
        reference = self._enable_for_restore()
        saved = self.state_dir / 'backups' / reference.decode().strip() / 'sysctl.conf'
        for bad in (b'# broken\n', b'# Managed by sing-box-manager: TCP BBR\nnet.ipv4.ip_forward=1\n'):
            with self.subTest(bad=bad):
                saved.write_bytes(bad)
                before = self._snapshot_protected()
                result = self.shell('confirm() { return 0; }; disable_bbr_locked')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._snapshot_protected(), before)

    def test_failed_pointer_rollback_keeps_permanent_original_reference(self) -> None:
        reference = self._enable_for_restore()
        result = self.shell(r'''
eval "$(declare -f atomic_copy | sed '1s/^atomic_copy/real_atomic_copy/')"
atomic_copy() { if [[ $2 == "$BBR_RESTORE_FILE" ]]; then return 1; fi; real_atomic_copy "$@"; }
rm() { command rm "$@"; local result=$?; if [[ ${*: -1} == "$BBR_RESTORE_FILE" ]]; then return 1; fi; return "$result"; }
confirm() { return 0; }; disable_bbr_locked
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('自动恢复未完全成功', result.stdout + result.stderr)
        saved = list((self.state_dir / 'backups').glob('bbr-*/active-before'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), reference)
        self.assertEqual(saved[0].stat().st_mode & 0o777, 0o600)

    def test_enable_loads_missing_bbr_module_before_writing(self) -> None:
        self._bbr_state(available="reno cubic")
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bbr", self._bbr_state()["available"].split())
        calls = self.modprobe_log.read_text(encoding="utf-8")
        self.assertIn("tcp_bbr", calls)

    def test_unsupported_kernel_leaves_runtime_and_files_unchanged(self) -> None:
        self._bbr_state(available="reno cubic", modprobe_fail=True)
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertFalse(self.bbr_file.exists())

    def test_cancel_returns_success_without_changes(self) -> None:
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 1; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertFalse(self.modprobe_log.exists() and self.modprobe_log.read_text(encoding="utf-8").strip())

    def test_same_runtime_and_persistent_file_is_noop_without_confirmation_or_backup(self) -> None:
        self._bbr_state(tcp_congestion_control="bbr", default_qdisc="fq")
        self.bbr_file.write_bytes(self._desired_conf())
        self.bbr_file.chmod(0o644)
        before_mtime = self.bbr_file.stat().st_mtime_ns
        result = self.shell('confirm() { printf CONFIRM; return 1; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("CONFIRM", result.stdout)
        self.assertEqual(self.bbr_file.stat().st_mtime_ns, before_mtime)
        self.assertEqual(list((self.state_dir / "backups").glob("bbr-*")), [])

    def test_runtime_write_failure_and_no_effect_are_rolled_back(self) -> None:
        for flag in ("fail_write_once", "fail_cc_once", "no_effect_once"):
            with self.subTest(flag=flag):
                self._bbr_state(**{flag: True})
                before = self._snapshot_protected()
                result = self.shell('confirm() { return 0; }; enable_bbr_locked')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._snapshot_protected(), before)
                self.assertTrue(list((self.state_dir / "backups").glob("bbr-*/runtime.json")))

    def test_persistent_write_failure_after_write_restores_runtime_and_file(self) -> None:
        self._bbr_state(tcp_congestion_control="cubic", default_qdisc="fq_codel")
        old_file = b"# Managed by sing-box-manager: TCP BBR\nnet.core.default_qdisc=fq_codel\nnet.ipv4.tcp_congestion_control=cubic\n"
        self.bbr_file.write_bytes(old_file)
        self.bbr_file.chmod(0o600)
        before = self._snapshot_protected()
        body = r'''
real_atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"
}
atomic_copy() { real_atomic_copy "$@"; if [[ $2 == "$BBR_SYSCTL_FILE" ]]; then return 1; fi; }
'''
        result = self.shell(body + 'confirm() { return 0; }; enable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)
        self.assertEqual(self.bbr_file.read_bytes(), old_file)
        self.assertEqual(self.bbr_file.stat().st_mode & 0o777, 0o600)

    def test_term_during_persistent_write_restores_runtime_and_file(self) -> None:
        self._bbr_state(tcp_congestion_control="cubic", default_qdisc="fq_codel")
        self.bbr_file.write_bytes(
            b"# Managed by sing-box-manager: TCP BBR\n"
            b"net.core.default_qdisc=fq_codel\n"
            b"net.ipv4.tcp_congestion_control=cubic\n"
        )
        before = self._snapshot_protected()
        marker = self.root / "bbr-term-once"
        self.env["BBR_TERM_ONCE"] = str(marker)
        body = r'''
real_atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"
}
atomic_copy() {
    real_atomic_copy "$@" || return
    if [[ $2 == "$BBR_SYSCTL_FILE" && ! -e "$BBR_TERM_ONCE" ]]; then : >"$BBR_TERM_ONCE"; kill -TERM "$BASHPID"; fi
}
'''
        result = self.shell(body + 'confirm() { return 0; }; enable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)

    def test_existing_project_file_is_backed_up_before_replacement(self) -> None:
        old_file = b"# Managed by sing-box-manager: TCP BBR\nnet.core.default_qdisc=fq_codel\nnet.ipv4.tcp_congestion_control=cubic\n"
        self.bbr_file.write_bytes(old_file)
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.bbr_file.read_bytes(), self._desired_conf())
        copies = [path for path in (self.state_dir / "backups").glob("bbr-*/*") if path.read_bytes() == old_file]
        self.assertTrue(copies)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)

    def test_unknown_file_and_symlink_are_rejected(self) -> None:
        self.bbr_file.write_text("# user owned\nnet.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        before = self._snapshot_protected()
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot_protected(), before)

        protected = self.root / "protected-sysctl-dir"
        protected.mkdir()
        self.bbr_file.unlink()
        self.bbr_dir.rmdir()
        self.bbr_dir.symlink_to(protected, target_is_directory=True)
        result = self.shell('confirm() { return 0; }; enable_bbr_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(protected.iterdir()))

    def test_persistence_priority_allows_early_default_but_rejects_late_and_global_conflicts(self) -> None:
        early = self.bbr_dir / "50-default.conf"
        early.write_text("net.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        self.assertEqual(self.shell("check_bbr_persistence").returncode, 0)

        late = self.bbr_dir / "zz-conflict.conf"
        late.write_text("net.core.default_qdisc=fq_codel\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)

        late.unlink()
        late.write_text("net/ipv4/tcp_congestion_control=cubic\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)

        late.write_text("net.ipv4.tcp_congestion_control=bbr # inline ambiguity\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)

        late.unlink()
        global_conf = self.root / "etc/sysctl.conf"
        global_conf.write_text("net.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)

    def test_persistence_same_filename_uses_higher_priority_directory(self) -> None:
        lower = self.root / "run/sysctl.d"
        lower.mkdir(parents=True)
        (lower / "zz.conf").write_text("net.core.default_qdisc=fq_codel\n", encoding="utf-8")
        (self.bbr_dir / "zz.conf").write_text("net.core.default_qdisc=fq\n", encoding="utf-8")
        self.assertEqual(self.shell("check_bbr_persistence").returncode, 0)

    def test_late_sysctld_cubic_is_not_hidden_by_global_bbr_value(self) -> None:
        late = self.bbr_dir / "zz-late.conf"
        late.write_text("net.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        global_conf = self.root / "etc/sysctl.conf"
        global_conf.write_text("net.ipv4.tcp_congestion_control=bbr\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)

    def test_dev_null_masking_does_not_hide_earlier_high_priority_conflict(self) -> None:
        lower = self.root / "usr/lib/sysctl.d"
        lower.mkdir(parents=True)
        (lower / "zz-last.conf").write_text("net.ipv4.tcp_congestion_control=bbr\n", encoding="utf-8")
        (self.bbr_dir / "zz-last.conf").symlink_to(Path("/dev/null"))
        first = self.bbr_dir / "zz-first.conf"
        first.write_text("net.ipv4.tcp_congestion_control=cubic\n", encoding="utf-8")
        self.assertNotEqual(self.shell("check_bbr_persistence").returncode, 0)
        first.unlink()
        self.assertEqual(self.shell("check_bbr_persistence").returncode, 0)

    def test_confirmation_foreign_file_change_cancels_without_runtime_writes(self) -> None:
        foreign = b"# foreign process\nnet.ipv4.tcp_congestion_control=cubic\n"
        before = self._snapshot_protected()
        result = self.shell(
            'BBR_TEST_FOREIGN_TEXT=$1; confirm() { printf "%s" "$BBR_TEST_FOREIGN_TEXT" > "$BBR_SYSCTL_FILE"; return 0; }; '
            'enable_bbr_locked',
            foreign.decode("utf-8"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.bbr_file.read_bytes(), foreign)
        self.assertEqual(self._snapshot_protected()["sysctl-state"], before["sysctl-state"])
        self.assertFalse(self.modprobe_log.exists() and self.modprobe_log.read_text(encoding="utf-8").strip())
        writes = [json.loads(line) for line in self.sysctl_log.read_text(encoding="utf-8").splitlines()] if self.sysctl_log.exists() else []
        self.assertFalse(any("-w" in call for call in writes))

    def test_confirmation_late_conflict_cancels_without_modprobe_or_sysctl_write(self) -> None:
        late = self.bbr_dir / "zz-created-during-confirm.conf"
        result = self.shell(
            'BBR_TEST_LATE_FILE=$1; confirm() { printf "net.core.default_qdisc=fq_codel\\n" > "$BBR_TEST_LATE_FILE"; return 0; }; '
            'enable_bbr_locked',
            str(late),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(late.is_file())
        self.assertFalse(self.modprobe_log.exists() and self.modprobe_log.read_text(encoding="utf-8").strip())
        writes = [json.loads(line) for line in self.sysctl_log.read_text(encoding="utf-8").splitlines()] if self.sysctl_log.exists() else []
        self.assertFalse(any("-w" in call for call in writes))

    def test_confirmation_runtime_change_cancels_without_second_write(self) -> None:
        result = self.shell(
            "confirm() { python3 -c 'import json,sys; p=sys.argv[1]; s=json.load(open(p)); s[\"tcp_congestion_control\"]=\"bbr\"; json.dump(s,open(p,\"w\"))' \"$BBR_MOCK_STATE\"; return 0; }; "
            'enable_bbr_locked',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.modprobe_log.exists() and self.modprobe_log.read_text(encoding="utf-8").strip())
        writes = [json.loads(line) for line in self.sysctl_log.read_text(encoding="utf-8").splitlines()] if self.sysctl_log.exists() else []
        self.assertFalse(any("-w" in call for call in writes))

    def test_cli_and_menu_fifteen_route_to_bbr_operations(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
enable_bbr_locked() { printf 'BBR_ENABLED\n'; }
disable_bbr_locked() { printf 'BBR_DISABLED\n'; }
show_bbr_status() { printf 'BBR_STATUS\n'; }
main bbr
main bbr-status
main bbr-disable
main bbr extra >/dev/null 2>&1 || printf 'INVALID\n'
main bbr-disable extra >/dev/null 2>&1 || printf 'INVALID_DISABLE\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LOCK:enable_bbr_locked", result.stdout)
        self.assertIn("BBR_STATUS", result.stdout)
        self.assertIn("LOCK:disable_bbr_locked", result.stdout)
        self.assertIn("BBR_DISABLED", result.stdout)
        self.assertIn("INVALID_DISABLE", result.stdout)
        self.assertIn("INVALID", result.stdout)

    @unittest.skipUnless(pty is not None, "PTY support is required for menu routing")
    def test_menu_fifteen_calls_bbr(self) -> None:
        command = 'source "$1"; show_status(){ :; }; enable_bbr(){ printf "BBR_MENU\\n"; }; disable_bbr(){ printf "BBR_DISABLE_MENU\\n"; }; menu'
        env = self.env.copy()
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", "-c", command, "bbr-menu", str(fixture.SCRIPT)], env)
        os.write(fd, b"15\n\n16\n\n0\n")
        chunks: list[bytes] = []
        deadline = time.time() + 5
        status: int | None = None
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunks.append(os.read(fd, 8192))
                except OSError:
                    pass
            waited, candidate_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate_status
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            self.fail("BBR menu PTY did not exit")
        output = b"".join(chunks).decode("utf-8", errors="replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("BBR_MENU", output)
        self.assertIn("BBR_DISABLE_MENU", output)


if __name__ == "__main__":
    unittest.main()
