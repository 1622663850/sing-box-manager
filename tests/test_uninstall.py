#!/usr/bin/env python3
"""Offline coverage for the sing-box manager uninstall and purge paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class UninstallTest(unittest.TestCase):
    """Use ManagerTest's isolated fixture without inheriting its test suite."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.lib_dir = self.root / "usr/local/lib/sing-box-manager"
        self.wrapper = self.root / "usr/local/bin/sing-box"
        self.config_dir = self.root / "etc/sing-box"
        self.marker = self.lib_dir / ".managed"
        self.source_dir = self.root / "staged"
        self.source = self.source_dir / "sing-box-manager.sh"

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def calls(self, log: Path) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, log)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def shell_env(
        self,
        body: str,
        *args: str,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        variables = dict(self.env)
        if env:
            variables.update(env)
        return subprocess.run(
            [
                "bash", "-c",
                'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body,
                "test-uninstall", str(fixture.SCRIPT), *args,
            ],
            env=variables,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def _install_fixture(self, *, active: bool = False, enabled: bool = False) -> Path:
        """Create a genuine managed installation identity in the temp root."""
        self.lib_dir.mkdir(parents=True, exist_ok=True)
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.wrapper.write_bytes(fixture.SCRIPT.read_bytes())
        self.wrapper.chmod(0o755)
        result = self.shell('write_unit > "$1"', str(self.unit))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.state(active=active, enabled=enabled)
        clients = self.state_dir / "clients"
        clients.mkdir(parents=True, exist_ok=True)
        (clients / "managed-node.json").write_text(
            json.dumps({"inbound": {"tag": "managed-node"}, "server": "example.com"}) + "\n",
            encoding="utf-8",
        )
        (self.state_dir / "backups" / "old.json").write_text("{}\n", encoding="utf-8")
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(fixture.SCRIPT.read_bytes())
        self.source.chmod(0o600)
        return self.source

    def _fixed_paths(self) -> tuple[Path, ...]:
        return (self.lib_dir, self.config_dir, self.state_dir, self.wrapper, self.unit)

    def _purge_locked(
        self,
        source: Path | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        body = 'with_lock purge_install_locked'
        args: tuple[str, ...] = ()
        if source is not None:
            body += ' "$1"'
            args = (str(source),)
        return self.shell_env(body, *args, env=env)

    def _validate(self, source: Path | None = None, body: str = 'validate_purge_paths "$1"'):
        return self.shell_env(body, *( (str(source),) if source is not None else ()))

    def _assert_fixed_install_exists(self) -> None:
        for path in self._fixed_paths():
            self.assertTrue(path.exists() or path.is_symlink(), f"fixture path missing: {path}")

    def _install_systemctl_failure_wrapper(self) -> dict[str, str]:
        original = self.bin / "systemctl"
        real = self.bin / "systemctl.real"
        original.rename(real)
        wrapper = self.bin / "systemctl"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if os.environ.get('SB_UNINSTALL_FAIL_STOP') == '1' and any(x in {'stop', 'disable'} for x in args):\n"
            "    sys.exit(1)\n"
            "os.execv(os.environ['SB_UNINSTALL_REAL_SYSTEMCTL'], [os.environ['SB_UNINSTALL_REAL_SYSTEMCTL'], *args])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {
            "SB_UNINSTALL_FAIL_STOP": "1",
            "SB_UNINSTALL_REAL_SYSTEMCTL": str(real),
        }

    def _install_disable_without_stop_wrapper(self) -> dict[str, str]:
        original = self.bin / "systemctl"
        real = self.bin / "systemctl.real"
        original.rename(real)
        wrapper = self.bin / "systemctl"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "if 'disable' in args:\n"
            "    state_path = pathlib.Path(os.environ['SB_MOCK_STATE_FILE'])\n"
            "    state = json.loads(state_path.read_text())\n"
            "    state['enabled'] = False\n"
            "    state_path.write_text(json.dumps(state))\n"
            "    with open(os.environ['SB_MOCK_SYSTEMCTL_LOG'], 'a', encoding='utf-8') as log:\n"
            "        log.write(json.dumps(args) + '\\n')\n"
            "    sys.exit(0)\n"
            "os.execv(os.environ['SB_UNINSTALL_REAL_SYSTEMCTL'], [os.environ['SB_UNINSTALL_REAL_SYSTEMCTL'], *args])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {"SB_UNINSTALL_REAL_SYSTEMCTL": str(real)}

    def _make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable in this environment: {error}")

    def test_legacy_uninstall_removes_only_core_and_unit(self) -> None:
        self._install_fixture(active=True, enabled=True)
        original_config = self.config.read_bytes()
        result = self.shell("confirm() { return 0; }; uninstall_core")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.core.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue(self.config.exists())
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "sing-box-manager-v1\n")
        self.assertTrue(self.state_dir.exists())
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        backups = list((self.state_dir / "backups").glob("uninstall-*") )
        self.assertTrue(backups)

    def test_purge_removes_owned_install_and_explicit_source_only(self) -> None:
        source = self._install_fixture(active=True, enabled=True)
        foreign = self.root / "usr/bin/xray"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("foreign xray\n", encoding="utf-8")
        foreign_config = self.root / "etc/xray/config.json"
        foreign_config.parent.mkdir(parents=True, exist_ok=True)
        foreign_config.write_text("foreign\n", encoding="utf-8")
        foreign_state = self.root / "var/lib/xray/keep.json"
        foreign_state.parent.mkdir(parents=True, exist_ok=True)
        foreign_state.write_text("keep\n", encoding="utf-8")
        foreign_unit = self.root / "lib/systemd/system/xray.service"
        foreign_unit.parent.mkdir(parents=True, exist_ok=True)
        foreign_unit.write_text("foreign unit\n", encoding="utf-8")
        acme_target = self.root / "external-acme-secret"
        acme_target.write_text("certificate material\n", encoding="utf-8")
        acme_link = self.root / "root/.acme.sh/example/fullchain.cer"
        acme_link.parent.mkdir(parents=True, exist_ok=True)
        self._make_symlink(acme_link, acme_target)

        result = self._purge_locked(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in self._fixed_paths():
            self.assertFalse(path.exists() or path.is_symlink(), f"owned path remains: {path}")
        self.assertFalse(source.exists())
        for path in (foreign, foreign_config, foreign_state, foreign_unit, acme_link, acme_target):
            self.assertTrue(path.exists() or path.is_symlink(), f"foreign path removed: {path}")
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])

    def test_purge_without_optional_source_keeps_source_file(self) -> None:
        source = self._install_fixture()
        result = self._purge_locked()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(source.is_file())
        self.assertFalse(self.lib_dir.exists())
        self.assertFalse(self.config_dir.exists())
        self.assertFalse(self.state_dir.exists())

    def test_purge_allows_missing_core_and_config(self) -> None:
        source = self._install_fixture()
        self.core.unlink()
        self.config.unlink()
        result = self._purge_locked(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in self._fixed_paths():
            self.assertFalse(path.exists() or path.is_symlink(), f"owned path remains: {path}")
        self.assertFalse(source.exists())

    def test_purge_disabled_but_enabled_unit_is_still_disabled_when_files_are_missing(self) -> None:
        source = self._install_fixture(active=False, enabled=True)
        self.unit.unlink()
        self.core.unlink()
        self.config.unlink()
        result = self._purge_locked(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls(self.system_log)
        self.assertTrue(any(call[:2] == ["disable", "--now"] for call in calls))
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        for path in self._fixed_paths():
            self.assertFalse(path.exists() or path.is_symlink(), f"owned path remains: {path}")
        self.assertFalse(source.exists())

    def test_purge_rejects_disable_that_does_not_stop_active_service(self) -> None:
        source = self._install_fixture(active=True, enabled=True)
        before = {path: path.read_bytes() for path in (self.core, self.config, self.unit, self.wrapper, self.marker, source)}
        no_stop_env = self._install_disable_without_stop_wrapper()
        result = self._purge_locked(source, env=no_stop_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(all(path.exists() and path.read_bytes() == content for path, content in before.items()))
        self.assertTrue(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self._assert_fixed_install_exists()

    def test_purge_cancel_or_non_delete_token_keeps_everything(self) -> None:
        self._install_fixture(active=True, enabled=True)
        before = {path: path.read_bytes() for path in (self.core, self.config, self.unit, self.wrapper, self.marker)}
        for source_answer, answer in (("", "NO"), ("0", "DELETE")):
            body = r'''
TEST_SOURCE="$1"
TEST_ANSWER="$2"
prompt() {
    case "$1" in
        source) printf -v "$1" '%s' "$TEST_SOURCE" ;;
        answer) printf -v "$1" '%s' "$TEST_ANSWER" ;;
        bbr_choice) printf -v "$1" '%s' 1 ;;
        *) return 1 ;;
    esac
}
purge_uninstall
'''
            result = self.shell(body, source_answer, answer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if source_answer != "0":
                self.assertIn(str(self.config_dir), result.stdout)
            self.assertTrue(all(path.exists() and path.read_bytes() == content for path, content in before.items()))
            self.assertTrue(self.state()["active"])
        self._assert_fixed_install_exists()

    def test_uninstall_menu_routes_legacy_purge_and_return(self) -> None:
        for choice, expected in (("1", "legacy"), ("2", "purge"), ("0", "")):
            with self.subTest(choice=choice):
                log = self.root / ("menu-" + choice)
                body = r'''
TEST_LOG="$1"
TEST_FIRST="$2"
TEST_STEP=0
prompt() {
    local variable=$1 value
    if (( TEST_STEP == 0 )); then value=$TEST_FIRST; else value=0; fi
    TEST_STEP=$((TEST_STEP + 1))
    printf -v "$variable" '%s' "$value"
}
uninstall_core() { printf 'legacy' >"$TEST_LOG"; }
purge_uninstall() { printf 'purge' >"$TEST_LOG"; }
uninstall_menu
'''
                result = self.shell(body, str(log), choice)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(log.read_text(encoding="utf-8") if log.exists() else "", expected)

    def test_cli_uninstall_purge_requires_exact_option(self) -> None:
        marker = self.root / "cli-route"
        body = r'''
TEST_MARKER="$1"
SB_ROOT_PREFIX=
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
uninstall_menu() { printf 'menu' >"$TEST_MARKER"; }
purge_uninstall() { printf 'purge' >"$TEST_MARKER"; }
main uninstall --purge
'''
        result = self.shell(body, str(marker))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "purge")

        marker.unlink()
        invalid = self.shell_env(
            r'''
TEST_MARKER="$1"
SB_ROOT_PREFIX=
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
uninstall_menu() { printf 'menu' >"$TEST_MARKER"; }
purge_uninstall() { printf 'purge' >"$TEST_MARKER"; }
main uninstall --unexpected
''',
            str(marker),
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(marker.exists())

    def test_validate_rejects_root_and_foreign_xray_path_overrides_before_stop(self) -> None:
        source = self._install_fixture(active=True)
        for variable, value in (
            ("LIB_DIR", "$SB_ROOT_PREFIX"),
            ("CONFIG_DIR", "$SB_ROOT_PREFIX/etc/xray"),
            ("CONFIG_FILE", "$SB_ROOT_PREFIX/etc/xray/config.json"),
            ("STATE_DIR", "$SB_ROOT_PREFIX/var/lib/xray"),
            ("UNIT_FILE", "$SB_ROOT_PREFIX/etc/systemd/system/xray.service"),
            ("WRAPPER", "$SB_ROOT_PREFIX/usr/local/bin/xray"),
            ("CORE_BIN", "$SB_ROOT_PREFIX/usr/bin/xray"),
        ):
            with self.subTest(variable=variable):
                result = self._validate(source, f'{variable}="{value}"; validate_purge_paths "$1"')
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(self.state()["active"])
        calls = self.calls(self.system_log)
        self.assertFalse(any(call and call[0] in {"stop", "disable"} for call in calls))
        self._assert_fixed_install_exists()

    def test_validate_rejects_non_project_marker_unit_wrapper_and_wrong_types(self) -> None:
        source = self._install_fixture()

        self.marker.write_text("not-sing-box-manager\n", encoding="utf-8")
        self.assertNotEqual(self._validate(source).returncode, 0)
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")

        wrapper_bytes = self.wrapper.read_bytes()
        self.wrapper.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        self.assertNotEqual(self._validate(source).returncode, 0)
        self.wrapper.write_bytes(wrapper_bytes)

        unit_bytes = self.unit.read_bytes()
        self.unit.write_text("[Service]\nExecStart=/foreign\n", encoding="utf-8")
        self.assertNotEqual(self._validate(source).returncode, 0)
        self.unit.write_bytes(unit_bytes)

        cases = (
            (self.lib_dir, "lib-real", True),
            (self.config_dir, "config-real", True),
            (self.state_dir, "state-real", True),
            (self.wrapper, "wrapper-real", False),
            (self.unit, "unit-real", False),
        )
        for target, backup_name, directory in cases:
            with self.subTest(path=str(target)):
                backup = target.with_name(backup_name)
                target.rename(backup)
                try:
                    if directory:
                        target.write_text("wrong type\n", encoding="utf-8")
                    else:
                        target.mkdir(parents=True)
                    self.assertNotEqual(self._validate(source).returncode, 0)
                finally:
                    if target.is_dir() and not target.is_symlink():
                        target.rmdir()
                    elif target.exists() or target.is_symlink():
                        target.unlink()
                    backup.rename(target)

    def test_validate_rejects_ancestor_leaf_and_source_symlinks(self) -> None:
        source = self._install_fixture()
        probe_target = self.root / "symlink-probe-target"
        probe_link = self.root / "symlink-probe-link"
        self._make_symlink(probe_link, probe_target)
        probe_link.unlink()

        etc_dir = self.root / "etc"
        etc_real = self.root / "etc-real"
        etc_dir.rename(etc_real)
        try:
            etc_dir.symlink_to(etc_real, target_is_directory=True)
            self.assertNotEqual(self._validate(source).returncode, 0)
        finally:
            if etc_dir.is_symlink():
                etc_dir.unlink()
            etc_real.rename(etc_dir)

        outside = self.root / "outside-wrapper"
        outside.write_text("foreign\n", encoding="utf-8")
        wrapper_bytes = self.wrapper.read_bytes()
        self.wrapper.unlink()
        self.wrapper.symlink_to(outside)
        try:
            self.assertNotEqual(self._validate(source).returncode, 0)
        finally:
            self.wrapper.unlink()
            self.wrapper.write_bytes(wrapper_bytes)
            self.wrapper.chmod(0o755)

        source_real = self.root / "source-real.sh"
        source_real.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(source_real)
        try:
            self.assertNotEqual(self._validate(source).returncode, 0)
        finally:
            source.unlink()
            source.write_bytes(source_real.read_bytes())

    def test_validate_rejects_untrusted_optional_source(self) -> None:
        self._install_fixture()
        bad = self.source_dir / "bad.sh"
        bad.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        self.assertNotEqual(self._validate(bad).returncode, 0)
        bad_dir = self.source_dir / "bad-dir"
        bad_dir.mkdir()
        self.assertNotEqual(self._validate(bad_dir).returncode, 0)
        self.assertNotEqual(self._validate(self.root).returncode, 0)
        self.assertNotEqual(self._validate(self.config_dir).returncode, 0)

    def test_purge_stop_failure_does_not_delete_anything(self) -> None:
        source = self._install_fixture(active=True, enabled=True)
        before = {path: path.read_bytes() for path in (self.core, self.config, self.unit, self.wrapper, self.marker, source)}
        fail_env = self._install_systemctl_failure_wrapper()
        result = self._purge_locked(source, env=fail_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(all(path.exists() and path.read_bytes() == content for path, content in before.items()))
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])
        self._assert_fixed_install_exists()

    def test_purge_rejects_an_occupied_manager_lock(self) -> None:
        source = self._install_fixture()
        if not shutil.which("flock"):
            self.skipTest("flock is required")
        ready = self.root / "lock-ready"
        holder = subprocess.Popen(
            [
                "bash", "-c",
                'exec 9>"$1"; flock -n 9 || exit 2; : >"$2"; sleep 30',
                "lock-holder", str(self.state_dir / "manager.lock"), str(ready),
            ],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            result = self._purge_locked(source)
            self.assertNotEqual(result.returncode, 0)
            self._assert_fixed_install_exists()
            self.assertTrue(source.exists())
        finally:
            if holder.poll() is None:
                holder.terminate()
            holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
