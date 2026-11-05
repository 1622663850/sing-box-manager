#!/usr/bin/env python3
"""Isolation tests for Xray core-only and complete purge uninstall paths.

The fixture never touches the host installation.  Complete purge is exercised
only under a temporary ``XR_ROOT_PREFIX`` with mock systemctl/sysctl commands.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import unittest

try:
    import fcntl
    import pty
    import select
    import struct
    import termios
except ImportError:  # pragma: no cover - Windows has no POSIX PTY.
    fcntl = pty = select = struct = termios = None

import test_xray_manager as fixture
import test_xray_platform as platform_fixture


SCRIPT = fixture.SCRIPT


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"),
                     "bash and jq are required")
class XrayUninstallTest(fixture.XrayFixture):
    """Complete purge must be explicit, owned, transactional, and verifiable."""

    def setUp(self) -> None:
        super().setUp()
        self._install_managed_layout()

    def _install_managed_layout(self, *, active: bool = False,
                                enabled: bool = False) -> None:
        for directory in (self.lib_dir, self.config.parent, self.state_dir,
                          self.clients_dir, self.backups_dir, self.unit.parent,
                          self.wrapper.parent):
            directory.mkdir(parents=True, exist_ok=True)
        self.wrapper.write_bytes(SCRIPT.read_bytes())
        self.wrapper.chmod(0o755)
        self.unit.write_text(self.shell("write_unit", check=True).stdout,
                             encoding="utf-8")
        self.unit.chmod(0o644)
        self.marker.write_text("xray-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.state(active=active, enabled=enabled)
        for path in (self.lib_dir / "geoip.dat", self.lib_dir / "geosite.dat"):
            path.write_bytes(b"synthetic geo data\n")
            path.chmod(0o600)
        node = self.write_node("purge-node", 18443)
        self.write_config([node])
        metadata = self.write_metadata(node, server="example.com")
        metadata.chmod(0o600)
        backup = self.backups_dir / "bundle-test.json"
        backup.write_text('{"test":true}\n', encoding="utf-8")
        backup.chmod(0o600)
        self.default_server.write_text("example.com\n", encoding="utf-8")
        self.default_server.chmod(0o600)

    def shell_env(self, body: str, *args: str, env: dict[str, str] | None = None,
                  check: bool = False) -> subprocess.CompletedProcess[str]:
        variables = dict(self.env)
        if env:
            variables.update(env)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; wait_healthy() { service_is_active; }; '
             + body, "xray-uninstall-test", str(SCRIPT), *args],
            env=variables,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=25,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _purge(self, source: Path | None = None, *, check: bool = False,
               prefix: str = "") -> subprocess.CompletedProcess[str]:
        body = prefix + 'with_lock purge_install_locked'
        args: tuple[str, ...] = ()
        if source is not None:
            body += ' "$1"'
            args = (str(source),)
        return self.shell_env(body, *args, check=check)

    def _validate(self, source: Path | None = None, *, prefix: str = "") -> subprocess.CompletedProcess[str]:
        args: tuple[str, ...] = ()
        body = prefix + 'validate_purge_paths'
        if source is not None:
            body += ' "$1"'
            args = (str(source),)
        else:
            body += ' ""'
        return self.shell_env(body, *args)

    def _fixed_paths(self) -> tuple[Path, ...]:
        return (self.lib_dir, self.config.parent, self.state_dir,
                self.wrapper, self.unit)

    def _assert_removed(self) -> None:
        for path in self._fixed_paths():
            self.assertFalse(path.exists() or path.is_symlink(), str(path))

    def _install_bbr(self) -> Path:
        platform_fixture.XrayPlatformTest._install_bbr_stubs(self)
        self.shell("confirm() { return 0; }; enable_bbr_locked", check=True)
        return self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"

    def test_core_only_uninstall_preserves_panel_config_users_backups_and_bbr(self) -> None:
        bbr_file = self._install_bbr()
        config_before = self.config.read_bytes()
        metadata_before = (self.clients_dir / "purge-node.json").read_bytes()
        result = self.shell("confirm() { return 0; }; uninstall_core_locked", check=True)
        self.assertFalse(self.core.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue(self.wrapper.is_file())
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual((self.clients_dir / "purge-node.json").read_bytes(), metadata_before)
        self.assertTrue(self.backups_dir.exists())
        self.assertTrue(bbr_file.exists())
        self.assertEqual(self.state(active=False, enabled=False)["active"], False)
        self.assertNotIn("彻底", result.stdout)

    def test_purge_removes_owned_install_and_keeps_unrelated_xray_singbox_acme_and_source(self) -> None:
        source = self.root / "staged" / "xray-manager.sh"
        source.parent.mkdir(parents=True)
        source.write_bytes(SCRIPT.read_bytes())
        source.chmod(0o600)
        foreign = {
            self.root / "usr/local/bin/sing-box": b"foreign sing-box\n",
            self.root / "etc/sing-box/config.json": b"foreign config\n",
            self.root / "var/lib/sing-box/keep.json": b"foreign state\n",
            self.root / "etc/ssl/foreign-cert.pem": b"foreign cert\n",
            self.root / "root/.acme.sh/example/fullchain.cer": b"foreign acme\n",
        }
        for path, data in foreign.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        result = self._purge(source, check=True)
        self._assert_removed()
        for path, data in foreign.items():
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(source.exists())
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self.assertIn("彻底卸载", result.stdout)

    def test_purge_allows_core_and_unit_already_missing(self) -> None:
        self.core.unlink()
        self.unit.unlink()
        result = self._purge(check=True)
        self._assert_removed()
        self.assertIn("彻底卸载", result.stdout)

    def test_core_only_uninstall_refuses_foreign_unit_without_deleting(self) -> None:
        self.unit.write_text('[Service]\nExecStart=/foreign/core\n',encoding='utf-8')
        before = self.config.read_bytes()
        result = self.shell('uninstall_core_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.core.is_file() and self.marker.is_file())
        self.assertEqual(self.config.read_bytes(), before)

    def test_purge_clears_dangling_owned_autostart_link(self) -> None:
        link = self.unit.parent / "multi-user.target.wants/xray.service"
        link.parent.mkdir()
        link.symlink_to("../xray.service")
        self.core.unlink()
        self.unit.unlink()
        self._purge(check=True)
        self._assert_removed()
        self.assertFalse(link.exists() or link.is_symlink())

    def test_validate_rejects_nested_mount_before_deleting(self) -> None:
        customization = self.root / "python-startup"
        customization.mkdir()
        mount = self.state_dir / "mounted"
        mount.mkdir()
        (customization / "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            "original=Path.read_text\n"
            "def read(self,*args,**kwargs):\n"
            " text=original(self,*args,**kwargs)\n"
            " if str(self)=='/proc/self/mountinfo':\n"
            f"  text+='\\n99999 1 0:1 / {mount} rw - tmpfs tmpfs rw\\n'\n"
            " return text\n"
            "Path.read_text=read\n", encoding="utf-8")
        result = self.shell_env("validate_purge_paths", env={"PYTHONPATH":str(customization)})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.core.exists() and self.config.exists() and self.marker.exists())

    def test_valid_bbr_restore_removes_new_owned_file_and_restores_runtime(self) -> None:
        bbr_file = self._install_bbr()
        result = self._purge(check=True)
        bbr_state = json.loads((self.root / "bbr-state.json").read_text(encoding="utf-8"))
        self.assertEqual((bbr_state["tcp_congestion_control"], bbr_state["default_qdisc"]),
                         ("cubic", "fq_codel"))
        self.assertFalse(bbr_file.exists())
        self._assert_removed()
        self.assertIn("BBR", result.stdout)

    def test_valid_bbr_restore_restores_preexisting_owned_file_and_mode(self) -> None:
        bbr_file = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
        original = b"# Managed by xray-manager: TCP BBR\n# baseline\nnet.core.default_qdisc=fq_codel\n"
        bbr_file.parent.mkdir(parents=True, exist_ok=True)
        bbr_file.write_bytes(original)
        bbr_file.chmod(0o640)
        self._install_bbr()
        self._purge(check=True)
        self.assertTrue(bbr_file.is_file())
        self.assertEqual(bbr_file.read_bytes(), original)
        self.assertEqual(bbr_file.stat().st_mode & 0o777, 0o640)
        self._assert_removed()

    def test_retry_preserves_already_restored_original_bbr_file(self) -> None:
        bbr_file = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
        bbr_file.parent.mkdir(parents=True, exist_ok=True)
        original = b"# Managed by xray-manager: TCP BBR\nnet.core.default_qdisc=fq_codel\n"
        bbr_file.write_bytes(original)
        bbr_file.chmod(0o640)
        self._install_bbr()
        source = self.root / "source-copy.sh"
        source.write_bytes(SCRIPT.read_bytes())
        command = r'''rm() {
    for arg in "$@"; do [[ $arg == "$TARGET" ]] && return 1; done
    command rm "$@"
}
'''
        self._assert_retryable_purge_failure(command=command, target=self.wrapper, source=source)
        self.assertEqual(bbr_file.read_bytes(), original)
        self.assertEqual(bbr_file.stat().st_mode & 0o777, 0o640)
        state = json.loads((self.root / "bbr-state.json").read_text())
        self.assertEqual((state["tcp_congestion_control"],state["default_qdisc"]),('cubic','fq_codel'))

    def test_missing_or_corrupt_bbr_record_does_not_block_owned_file_cleanup(self) -> None:
        platform_fixture.XrayPlatformTest._install_bbr_stubs(self)
        before_runtime = (self.root / "bbr-state.json").read_bytes()
        bbr_file = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
        for index,value in enumerate((None, "invalid-record\n", "bbr-20261105-010101-AbC123\n", "bbr-20261105-010101-AbC124\n")):
            with self.subTest(value=value):
                if not self.config.exists(): self._install_managed_layout()
                bbr_file.parent.mkdir(parents=True, exist_ok=True)
                bbr_file.write_text("# Managed by xray-manager: TCP BBR\nnet.core.default_qdisc=fq\n",encoding="utf-8")
                bbr_file.chmod(0o600)
                self.bbr_restore = self.state_dir / "bbr-active"
                if value is None:
                    self.bbr_restore.unlink(missing_ok=True)
                else:
                    self.bbr_restore.write_text(value, encoding="utf-8")
                    self.bbr_restore.chmod(0o600)
                    if index == 3:
                        invalid = self.backups_dir / value.strip()
                        invalid.mkdir()
                        (invalid / "runtime.json").write_text("{invalid-json",encoding="utf-8")
                result = self._purge(check=True)
                self.assertFalse(bbr_file.exists())
                self.assertFalse((self.state_dir / "bbr-active").exists())
                self.assertIn("BBR", result.stdout + result.stderr)
                self.assertEqual((self.root / "bbr-state.json").read_bytes(), before_runtime)

    def test_foreign_or_mixed_bbr_file_is_preserved(self) -> None:
        for original in (
            b"# foreign setting\nnet.ipv4.tcp_congestion_control=bbr\n",
            (b"# Managed by xray-manager: TCP BBR\n"
             b"net.core.default_qdisc=fq\n"
             b"net.ipv4.tcp_congestion_control=bbr\n"
             b"net.ipv4.ip_forward=1\n"),
        ):
            with self.subTest(mixed=original.startswith(b"# Managed")):
                if not self.config.exists():
                    self._install_managed_layout()
                bbr_file = self.root / "etc/sysctl.d/99-xray-manager-bbr.conf"
                bbr_file.parent.mkdir(parents=True, exist_ok=True)
                bbr_file.write_bytes(original)
                bbr_file.chmod(0o600)
                self._purge(check=True)
                self.assertEqual(bbr_file.read_bytes(), original)
                self._assert_removed()

    def test_restore_failure_keeps_runtime_bbr_but_cleans_owned_file(self) -> None:
        bbr_file = self._install_bbr()
        self.state_after_bbr = json.loads((self.root / "bbr-state.json").read_text(encoding="utf-8"))
        self.state_after_bbr["fail_write_once"] = True
        fixture.dump(self.root / "bbr-state.json", self.state_after_bbr)
        result = self._purge(check=True)
        state = json.loads((self.root / "bbr-state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["tcp_congestion_control"], state["default_qdisc"]), ("bbr", "fq"))
        self.assertFalse(bbr_file.exists())
        self._assert_removed()
        self.assertIn("BBR", result.stdout + result.stderr)

    def test_bbr_file_delete_failure_is_reported_and_keeps_retryable_state(self) -> None:
        bbr_file = self._install_bbr()
        body = r'''rm() {
    for arg in "$@"; do [[ "$arg" == "$BBR_SYSCTL_FILE" ]] && return 1; done
    command rm "$@"
}
with_lock purge_install_locked'''
        result = self.shell_env(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(bbr_file.exists())
        self.assertTrue(self.config.exists())
        self.assertTrue(self.wrapper.exists())
        self.assertIn("BBR", result.stdout + result.stderr)

    def test_stop_failure_does_not_touch_bbr_or_config(self) -> None:
        bbr_file = self._install_bbr()
        before_config = self.config.read_bytes()
        body = r'''systemctl() { return 1; }
with_lock purge_install_locked'''
        result = self.shell_env(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertTrue(bbr_file.exists())
        self.assertTrue(self.wrapper.exists())
        self.assertTrue(self.core.exists())

    def test_validate_rejects_wrong_owner_symlink_and_path_override(self) -> None:
        for mutation in (
            lambda: self.marker.write_text("foreign\n", encoding="utf-8"),
            lambda: self.wrapper.write_text("#!/bin/sh\n", encoding="utf-8"),
            lambda: self.unit.write_text("[Service]\nExecStart=/foreign\n", encoding="utf-8"),
        ):
            with self.subTest(mutation=mutation):
                self._install_managed_layout()
                mutation()
                self.assertNotEqual(self._validate().returncode, 0)
        self._install_managed_layout()
        outside = self.root / "outside"
        outside.write_text("outside\n", encoding="utf-8")
        self.wrapper.unlink()
        self.wrapper.symlink_to(outside)
        try:
            self.assertNotEqual(self._validate().returncode, 0)
        finally:
            self.wrapper.unlink()
            self.wrapper.write_bytes(SCRIPT.read_bytes())
            self.wrapper.chmod(0o755)
        result = self._validate(prefix=f'LIB_DIR="$XR_ROOT_PREFIX/usr/local/lib/other"; ')
        self.assertNotEqual(result.returncode, 0)
        result = self._validate(prefix='SCRIPT_FILE="$XR_ROOT_PREFIX/outside"; ')
        self.assertNotEqual(result.returncode, 0)
        state = {"format":"xray-manager-purge-bbr-v1","existed":False,"sha256":"","mode":""}
        fixture.dump(self.state_dir / "purge-bbr-restored.json", state)
        self.shell("purge_bbr_state check", check=True)
        fixture.dump(self.state_dir / "purge-bbr-restored.json", {**state,"unexpected":"synthetic"})
        self.assertNotEqual(self.shell("purge_bbr_state check").returncode, 0)

    def test_purge_cancel_and_cli_require_exact_mode(self) -> None:
        before = {path: path.read_bytes() for path in (self.core, self.config, self.wrapper, self.unit, self.marker)}
        body = r'''TEST_CHOICE="$1"
prompt() { if [[ $1 == source ]]; then printf -v "$1" ''; else printf -v "$1" '%s' "$TEST_CHOICE"; fi; }
purge_uninstall'''
        for choice in ("", "0", "NO"):
            result = self.shell(body, choice)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(all(path.read_bytes() == value for path, value in before.items()))
        marker = self.root / "cli-route"
        result = self.shell_env(r'''
MARKER="$1"
XR_ROOT_PREFIX=
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
purge_uninstall() { printf purge >"$MARKER"; }
uninstall_menu() { printf menu >"$MARKER"; }
main uninstall --purge
''', str(marker))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "purge")
        marker.unlink()
        result = self.shell_env(r'''
MARKER="$1"
XR_ROOT_PREFIX=
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
purge_uninstall() { printf purge >"$MARKER"; }
uninstall_menu() { printf menu >"$MARKER"; }
main uninstall --unexpected
''', str(marker))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_uninstall_menu_routes_core_only_purge_and_return(self) -> None:
        for choice, expected in (("1", "core"), ("2", "purge"), ("0", "")):
            marker = self.root / ("menu-" + choice)
            body = r'''MARKER="$1"
CHOICE="$2"
prompt() { printf -v "$1" '%s' "$CHOICE"; }
uninstall_core() { printf core >"$MARKER"; }
purge_uninstall() { printf purge >"$MARKER"; }
uninstall_menu'''
            result = self.shell(body, str(marker), choice)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8") if marker.exists() else "", expected)

    def _assert_retryable_purge_failure(self, *, command: str,
                                        target: Path, source: Path) -> None:
        body = f'''TARGET="$1"
SOURCE="$2"
{command}
with_lock purge_install_locked "$SOURCE"'''
        first = self.shell_env(body, str(target), str(source))
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(self.marker.is_file(), "trusted marker was not restored")
        self.assertTrue(self.wrapper.is_file(), "executable wrapper was not restored")
        self.assertTrue(self.wrapper.stat().st_mode & 0o100,
                        "restored wrapper is not executable")
        self.assertIn("卸载", first.stdout + first.stderr)
        second = self._purge(source, check=True)
        self._assert_removed()
        self.assertFalse(source.exists())
        self.assertFalse(list(self.state_dir.parent.glob("xray-purge.*")))
        if self.state_dir.exists():
            self.assertFalse(list(self.state_dir.glob("xray-purge.*")))
        self.assertIn("彻底卸载", second.stdout)

    def test_partial_state_directory_delete_failure_restores_marker_and_retries(self) -> None:
        source = self.root / "staged" / "xray-manager.sh"
        source.parent.mkdir(parents=True)
        source.write_bytes(SCRIPT.read_bytes())
        source.chmod(0o600)
        self._assert_retryable_purge_failure(
            command=r'''rm() {
    for arg in "$@"; do [[ "$arg" == "$TARGET" ]] && return 1; done
    command rm "$@"
}''',
            target=self.state_dir,
            source=source,
        )

    def test_wrapper_delete_failure_restores_entrypoint_and_retries(self) -> None:
        source = self.root / "staged" / "xray-manager.sh"
        source.parent.mkdir(parents=True)
        source.write_bytes(SCRIPT.read_bytes())
        source.chmod(0o600)
        self._assert_retryable_purge_failure(
            command=r'''rm() {
    for arg in "$@"; do [[ "$arg" == "$TARGET" ]] && return 1; done
    command rm "$@"
}''',
            target=self.wrapper,
            source=source,
        )

    def test_optional_source_delete_failure_restores_marker_and_retries(self) -> None:
        source = self.root / "staged" / "xray-manager.sh"
        source.parent.mkdir(parents=True)
        source.write_bytes(SCRIPT.read_bytes())
        source.chmod(0o600)
        self._assert_retryable_purge_failure(
            command=r'''rm() {
    for arg in "$@"; do [[ "$arg" == "$TARGET" ]] && return 1; done
    command rm "$@"
}''',
            target=source,
            source=source,
        )

    def test_library_directory_remove_failure_restores_marker_and_retries(self) -> None:
        source = self.root / "staged" / "xray-manager.sh"
        source.parent.mkdir(parents=True)
        source.write_bytes(SCRIPT.read_bytes())
        source.chmod(0o600)
        self._assert_retryable_purge_failure(
            command=r'''rmdir() {
    for arg in "$@"; do [[ "$arg" == "$LIB_DIR" ]] && return 1; done
    command rmdir "$@"
}''',
            target=self.lib_dir,
            source=source,
        )

    def test_temporary_cleanup_failure_does_not_report_complete_success(self) -> None:
        body = r'''rm() {
    for arg in "$@"; do [[ $arg == "$XR_ROOT_PREFIX"/var/lib/xray-purge.* ]] && return 1; done
    command rm "$@"
}
with_lock purge_install_locked'''
        result = self.shell_env(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("已彻底卸载", result.stdout)
        self.assertIn("临时清理文件未删除", result.stdout + result.stderr)
        self._assert_removed()
        self.assertTrue(list(self.state_dir.parent.glob("xray-purge.*")))

    @unittest.skipIf(os.name == "nt" or pty is None, "requires a POSIX controlling terminal")
    def test_real_main_menu_purge_exits_after_successful_delete(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 240, 0, 0))
        process = subprocess.Popen(
            ["bash", "-c", 'source "$1"; menu', "xray-uninstall-menu", str(SCRIPT)],
            env=self.env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        transcript = bytearray()

        def wait_for(needle: str) -> None:
            target = needle.encode("utf-8")
            deadline = time.monotonic() + 12
            while target not in transcript:
                self.assertLess(time.monotonic(), deadline, "prompt not shown: " + needle)
                if process.poll() is not None:
                    self.fail("menu exited before prompt: " + needle)
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as error:
                        self.fail(f"PTY closed before prompt: {error}")
                    self.assertTrue(chunk)
                    transcript.extend(chunk)

        try:
            wait_for("请输入选项 [0-21]")
            os.write(master, b"2\n")
            wait_for("请选择卸载方式")
            os.write(master, b"2\n")
            wait_for("额外删除的原下载脚本绝对路径")
            os.write(master, b"\n")
            wait_for("输入 DELETE 确认")
            os.write(master, b"DELETE\n")
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertEqual(transcript.decode("utf-8", errors="replace").count("请输入选项"), 1)
            self._assert_removed()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
