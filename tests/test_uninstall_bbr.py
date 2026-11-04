#!/usr/bin/env python3
"""Isolated purge/BBR ownership regressions for sing-box-manager."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_bbr as bbr_fixture
import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class UninstallBbrTest(unittest.TestCase):
    """Delegate setup to BbrTest without inheriting and rerunning its tests."""

    def _install_mock_sysctl(self) -> None:
        bbr_fixture.BbrTest._install_mock_sysctl(self)

    def _install_mock_modprobe(self) -> None:
        bbr_fixture.BbrTest._install_mock_modprobe(self)

    def setUp(self) -> None:
        bbr_fixture.BbrTest.setUp(self)

        # Build the exact managed layout that setup_panel/install_core would
        # leave behind.  Purge validation must not be satisfied by fixture
        # placeholders or a hand-written unit with a different ExecStart.
        self.wrapper = self.root / "usr/local/bin/sing-box"
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.wrapper.write_bytes(fixture.SCRIPT.read_bytes())
        self.wrapper.chmod(0o755)
        unit = self.shell("write_unit", check=True).stdout
        self.unit.write_text(unit, encoding="utf-8")
        self.unit.chmod(0o644)
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.config.chmod(0o600)
        for directory in (self.state_dir, self.clients, self.state_dir / "backups"):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return bbr_fixture.BbrTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def read_bbr_state(self, **updates: object) -> dict:
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

    def _enable_bbr(self) -> None:
        result = self.shell("confirm() { return 0; }; enable_bbr_locked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _panel_snapshot(self) -> dict[str, bytes | None]:
        paths = (self.core, self.config, self.wrapper, self.unit, self.marker)
        return {str(path): path.read_bytes() if path.exists() else None for path in paths}

    def _assert_panel_removed(self) -> None:
        for path in (self.core, self.config, self.state_dir, self.wrapper, self.unit, self.marker):
            self.assertFalse(path.exists() or path.is_symlink(), str(path))

    def _assert_valid_layout(self) -> None:
        result = self.shell('validate_purge_paths "$1"', "")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _archive_dirs(self) -> list[Path]:
        return sorted((self.root / "var/lib").glob("sing-box-manager-bbr-recovery-*"))

    def test_keep_purges_panel_preserves_live_bbr_and_archives_only_recovery_data(self) -> None:
        original_bbr = (
            b"# Managed by sing-box-manager: TCP BBR\n"
            b"net.core.default_qdisc=fq_codel\n"
            b"net.ipv4.tcp_congestion_control=cubic\n"
        )
        self.bbr_file.write_bytes(original_bbr)
        self.bbr_file.chmod(0o600)
        self._enable_bbr()
        reference = self.restore_file.read_text(encoding="utf-8").strip()
        source_backup = self.state_dir / "backups" / reference
        runtime_before = (source_backup / "runtime.json").read_bytes()

        node_secret = "synthetic-node-secret"
        fixture.dump(
            self.config,
            {"inbounds": [{"type": "vless", "users": [{"uuid": node_secret}]}], "outbounds": []},
        )
        self.config.chmod(0o600)
        fixture.dump(self.clients / "node.json", {"password": node_secret, "inbound": {"tag": "node"}})
        (self.clients / "node.json").chmod(0o600)
        foreign = self.state_dir / "backups" / "foreign-panel"
        foreign.mkdir(mode=0o700)
        (foreign / "credentials.json").write_text("other-panel-secret\n", encoding="utf-8")
        (foreign / "credentials.json").chmod(0o600)
        self._assert_valid_layout()

        bbr_file_before = self.bbr_file.read_bytes()
        bbr_mode_before = self.bbr_file.stat().st_mode & 0o777
        kernel_before = self.read_bbr_state()
        result = self.shell("purge_install_locked", check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_panel_removed()
        self.assertEqual(self.bbr_file.read_bytes(), bbr_file_before)
        self.assertEqual(self.bbr_file.stat().st_mode & 0o777, bbr_mode_before)
        self.assertEqual(
            tuple(self.read_bbr_state()[key] for key in ("tcp_congestion_control", "default_qdisc")),
            tuple(kernel_before[key] for key in ("tcp_congestion_control", "default_qdisc")),
        )

        archives = self._archive_dirs()
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual((archive / "bbr-active").read_text(encoding="utf-8").strip(), reference)
        self.assertEqual((archive / "bbr-active").stat().st_mode & 0o777, 0o600)
        archived_runtime = archive / "backups" / reference / "runtime.json"
        self.assertEqual(archived_runtime.read_bytes(), runtime_before)
        self.assertEqual(archived_runtime.stat().st_mode & 0o777, 0o600)
        archived_sysctl = archive / "backups" / reference / "sysctl.conf"
        self.assertEqual(archived_sysctl.read_bytes(), original_bbr)
        self.assertEqual(archived_sysctl.stat().st_mode & 0o777, 0o600)
        for path in archive.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(node_secret.encode(), content)
                self.assertNotIn(b"other-panel-secret", content)
        self.assertFalse((archive / "config.json").exists())
        self.assertFalse((archive / "clients").exists())

    def test_purge_without_bbr_has_no_external_recovery_archive(self) -> None:
        kernel_before = self.read_bbr_state()
        state_before = self.bbr_state.read_bytes()
        self._assert_valid_layout()
        result = self.shell("purge_install_locked", check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_panel_removed()
        self.assertFalse(self.bbr_file.exists())
        self.assertEqual(self._archive_dirs(), [])
        self.assertEqual(self.bbr_state.read_bytes(), state_before)
        self.assertEqual(
            tuple(self.read_bbr_state()[key] for key in ("tcp_congestion_control", "default_qdisc")),
            tuple(kernel_before[key] for key in ("tcp_congestion_control", "default_qdisc")),
        )

    def test_keep_filters_false_config_runtime_and_does_not_migrate_sysctl_file(self) -> None:
        self._enable_bbr()
        reference = self.restore_file.read_text(encoding="utf-8").strip()
        source_backup = self.state_dir / "backups" / reference
        runtime = json.loads((source_backup / "runtime.json").read_text(encoding="utf-8"))
        expected_runtime = {
            key: runtime[key]
            for key in ("tcp_congestion_control", "default_qdisc", "config_existed", "config_mode")
        }
        runtime["node_password"] = "synthetic-node-password"
        fixture.dump(source_backup / "runtime.json", runtime)
        (source_backup / "sysctl.conf").write_text(
            "# Managed by sing-box-manager: TCP BBR\nnode_password=synthetic-node-password\n",
            encoding="utf-8",
        )
        (source_backup / "sysctl.conf").chmod(0o600)
        bbr_file_before = self.bbr_file.read_bytes()
        bbr_mode_before = self.bbr_file.stat().st_mode & 0o777
        kernel_before = self.read_bbr_state()

        result = self.shell("purge_install_locked", check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_panel_removed()
        archive_dirs = self._archive_dirs()
        self.assertEqual(len(archive_dirs), 1)
        archive = archive_dirs[0]
        archived_runtime = archive / "backups" / reference / "runtime.json"
        archived_value = json.loads(archived_runtime.read_text(encoding="utf-8"))
        self.assertEqual(archived_value, expected_runtime)
        self.assertNotIn("node_password", archived_value)
        self.assertFalse((archive / "backups" / reference / "sysctl.conf").exists())
        self.assertNotIn(b"synthetic-node-password", archived_runtime.read_bytes())
        for path in archive.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-node-password", path.read_bytes())
        self.assertEqual(self.bbr_file.read_bytes(), bbr_file_before)
        self.assertEqual(self.bbr_file.stat().st_mode & 0o777, bbr_mode_before)
        self.assertEqual(
            tuple(self.read_bbr_state()[key] for key in ("tcp_congestion_control", "default_qdisc")),
            tuple(kernel_before[key] for key in ("tcp_congestion_control", "default_qdisc")),
        )

    def test_missing_or_corrupt_bbr_record_refuses_before_stopping_service(self) -> None:
        self.state(active=True, enabled=True)
        self.read_bbr_state(tcp_congestion_control="bbr", default_qdisc="fq")
        self.bbr_file.write_bytes(self._desired_conf())
        before = self._panel_snapshot()
        result = self.shell("purge_install_locked")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._panel_snapshot(), before)
        self.assertEqual(self.read_bbr_state()["tcp_congestion_control"], "bbr")
        self.assertFalse(any(any(action in call for action in ("disable", "stop")) for call in fixture.ManagerTest.calls(self, self.system_log)))

        self.restore_file.write_text("../outside\n", encoding="utf-8")
        before = self._panel_snapshot()
        result = self.shell("purge_install_locked")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._panel_snapshot(), before)
        self.assertFalse(any(any(action in call for action in ("disable", "stop")) for call in fixture.ManagerTest.calls(self, self.system_log)))

    def test_bbr_archive_copy_failure_keeps_original_panel_and_bbr_inputs(self) -> None:
        self._enable_bbr()
        before = self._panel_snapshot()
        bbr_file_before = self.bbr_file.read_bytes()
        restore_before = self.restore_file.read_bytes()
        body = r'''
install() {
    for argument in "$@"; do
        [[ $argument == *sing-box-manager-bbr-recovery-* ]] && return 1
    done
    command install "$@"
}
cp() {
    for argument in "$@"; do
        [[ $argument == *sing-box-manager-bbr-recovery-* ]] && return 1
    done
    command cp "$@"
}
purge_install_locked
'''
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._panel_snapshot(), before)
        self.assertEqual(self.bbr_file.read_bytes(), bbr_file_before)
        self.assertEqual(self.restore_file.read_bytes(), restore_before)
        self.assertEqual(self.read_bbr_state()["tcp_congestion_control"], "bbr")
        self.assertTrue(self.core.exists())
        self.assertTrue(self.wrapper.exists())
        self.assertTrue(self.unit.exists())

    def test_cancelled_purge_confirmation_preserves_panel_and_bbr(self) -> None:
        self._enable_bbr()
        before = self._panel_snapshot()
        bbr_before = self.bbr_file.read_bytes()
        restore_before = self.restore_file.read_bytes()
        body = r'''
prompt() {
    case "$1" in
        source) value="" ;;
        bbr_choice) value="0" ;;
        answer) value="CANCEL" ;;
        *) value="" ;;
    esac
    printf -v "$1" '%s' "$value"
}
purge_uninstall
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._panel_snapshot(), before)
        self.assertEqual(self.bbr_file.read_bytes(), bbr_before)
        self.assertEqual(self.restore_file.read_bytes(), restore_before)
        self.assertEqual(self._archive_dirs(), [])


if __name__ == "__main__":
    unittest.main()
