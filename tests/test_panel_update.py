#!/usr/bin/env python3
"""Regression coverage for updating the installed manager wrapper."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import unittest

import test_manager as fixture

try:
    import pty
except ImportError:  # pragma: no cover - Windows test discovery has no termios.
    pty = None


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class PanelUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.wrapper = self.root / "usr/local/bin/sing-box"
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.old_wrapper = (
            "#!/usr/bin/env bash\n"
            "# sing-box-manager: a self-contained Chinese terminal manager.\n"
            "SB_MANAGER_VERSION=\"1.4.5\"\n"
            "printf 'old-menu\\n'\n"
        ).encode("utf-8")
        self.wrapper.write_bytes(self.old_wrapper)
        self.wrapper.chmod(0o755)
        self.default = self.state_dir / "default-server"
        self.default.write_text("example.com\n", encoding="utf-8")
        self.default.chmod(0o600)
        self.clients = self.state_dir / "clients"
        self.clients.mkdir(parents=True, exist_ok=True)
        fixture.dump(self.clients / "node.json", {"server": "example.com", "inbound": {"tag": "node"}})
        (self.clients / "node.json").chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def _candidate(self, *, version: str = "1.5.0", help_ok: bool = True, syntax_ok: bool = True,
                  help_fd_safe: bool = False, help_hangs: bool = False) -> bytes:
        if not syntax_ok:
            return (
                "#!/usr/bin/env bash\n"
                "# sing-box-manager: a self-contained Chinese terminal manager.\n"
                f'SB_MANAGER_VERSION="{version}"\n'
                "if (\n"
            ).encode("utf-8")
        if help_hangs:
            help_body = "trap '' TERM; sleep 60"
        elif not help_ok:
            help_body = "return 1"
        elif help_fd_safe:
            help_body = "[[ ! -e /proc/$$/fd/9 && ! -t 0 ]] || return 1; printf 'sing-box 中文终端管理面板\\n'"
        else:
            help_body = "printf 'sing-box 中文终端管理面板\\n'"
        return (
            "#!/usr/bin/env bash\n"
            "# sing-box-manager: a self-contained Chinese terminal manager.\n"
            f'SB_MANAGER_VERSION="{version}"\n'
            "usage() { " + help_body + "; }\n"
            "menu() { printf 'new-menu\\n'; }\n"
            "main() { case \"${1:-menu}\" in help|-h|--help) usage;; menu) menu;; *) usage;; esac; }\n"
            'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi\n'
        ).encode("utf-8")

    def _menu_candidate(self) -> bytes:
        return (
            "#!/usr/bin/env bash\n"
            "# sing-box-manager: a self-contained Chinese terminal manager.\n"
            'SB_MANAGER_VERSION="1.5.0"\n'
            "usage() { [[ ! -e /proc/$$/fd/9 && ! -t 0 ]] || return 1; printf 'sing-box 中文终端管理面板\\n'; }\n"
            "menu() {\n"
            '  lock="${SB_ROOT_PREFIX}/var/lib/sing-box-manager/manager.lock"\n'
            '  exec 9>"$lock" || exit 1\n'
            '  flock -n 9 || exit 1\n'
            "  printf 'NEW_MENU_WITH_UNLOCKED_LOCK\\n'\n"
            "}\n"
            "main() { case \"${1:-menu}\" in help|-h|--help) usage;; menu) menu;; *) usage;; esac; }\n"
            'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi\n'
        ).encode("utf-8")

    def _payload(self, content: bytes, *, size: int | None = None, sha: str | None = None,
                 encoding: str = "base64", kind: str = "file", path: str = "sing-box-manager.sh",
                 raw_content: str | None = None) -> Path:
        digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        payload = {
            "type": kind,
            "path": path,
            "encoding": encoding,
            "size": len(content) if size is None else size,
            "sha": digest if sha is None else sha,
            "content": base64.b64encode(content).decode("ascii") if raw_content is None else raw_content,
        }
        path = self.root / "panel-release.json"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.env["SB_PANEL_FIXTURE"] = str(path)
        return path

    def _prepare_download(self, content: bytes, **kwargs: object) -> None:
        self._payload(content, **kwargs)

    def _update_shell(self, body: str = "") -> subprocess.CompletedProcess[str]:
        download = r'''
download_https() {
    [[ $1 == "https://api.github.com/repos/1622663850/sing-box-manager/contents/sing-box-manager.sh?ref=main" ]] || return 77
    cp -- "$SB_PANEL_FIXTURE" "$2"
}
'''
        return self.shell(download + body + " update_panel_locked")

    def _snapshot(self) -> dict[str, object]:
        paths = [self.config, self.core, self.unit, self.marker, self.default]
        values: dict[str, object] = {
            str(path): path.read_bytes() if path.exists() and not path.is_symlink() else None
            for path in paths
        }
        values["clients"] = {
            path.name: path.read_bytes()
            for path in sorted(self.clients.glob("*.json"))
            if path.is_file() and not path.is_symlink()
        }
        values["service-state"] = self.mock_state.read_bytes()
        values["systemctl-log"] = self.system_log.read_bytes() if self.system_log.exists() else None
        return values

    def test_valid_update_backups_old_wrapper_and_preserves_all_runtime_state(self) -> None:
        candidate = self._candidate(help_fd_safe=True)
        self._prepare_download(candidate)
        self.state(active=True, enabled=True)
        before = self._snapshot()
        result = self._update_shell()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.wrapper.read_bytes(), candidate)
        self.assertEqual(self.wrapper.stat().st_mode & 0o777, 0o755)
        backups = sorted((self.state_dir / "backups").glob("panel-*.sh"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old_wrapper)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        after = self._snapshot()
        for key, value in before.items():
            if key != str(self.wrapper):
                self.assertEqual(after[key], value, key)
        help_result = subprocess.run(
            ["bash", str(self.wrapper), "help"],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=10,
        )
        menu_result = subprocess.run(["bash", str(self.wrapper), "menu"], text=True, capture_output=True, timeout=10)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("sing-box 中文终端管理面板", help_result.stdout)
        self.assertEqual(menu_result.stdout.strip(), "new-menu")

    def test_update_does_not_require_installed_core(self) -> None:
        candidate = self._candidate()
        self._prepare_download(candidate)
        self.core.unlink()
        result = self._update_shell()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.wrapper.read_bytes(), candidate)
        self.assertFalse(self.core.exists())

    def test_same_bytes_update_skips_write_and_backup(self) -> None:
        candidate = self._candidate()
        self.wrapper.write_bytes(candidate)
        self.wrapper.chmod(0o755)
        self._prepare_download(candidate)
        before_mtime = self.wrapper.stat().st_mtime_ns
        result = self._update_shell()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.wrapper.read_bytes(), candidate)
        self.assertEqual(self.wrapper.stat().st_mtime_ns, before_mtime)
        self.assertEqual(list((self.state_dir / "backups").glob("panel-*.sh")), [])

    def test_download_and_candidate_validation_failures_leave_old_wrapper(self) -> None:
        wrong_shebang = self._candidate().replace(b"#!/usr/bin/env bash\n", b"#!/usr/bin/env python3\n", 1)
        missing_marker = self._candidate().replace(
            b"# sing-box-manager: a self-contained Chinese terminal manager.",
            b"# unrelated script marker",
            1,
        )
        cases: list[tuple[str, bytes | None, dict[str, object]]] = [
            ("download", None, {}),
            ("digest", self._candidate(), {"sha": "0" * 40}),
            ("size", self._candidate(), {"size": len(self._candidate()) + 1}),
            ("base64", self._candidate(), {"raw_content": "%%%"}),
            ("header", b"<html>error</html>\n", {"kind": "dir", "encoding": "utf-8"}),
            ("syntax", self._candidate(syntax_ok=False), {}),
            ("help", self._candidate(help_ok=False), {}),
            ("shebang", wrong_shebang, {}),
            ("marker", missing_marker, {}),
            ("version", self._candidate(version="1.5"), {}),
        ]
        for name, content, kwargs in cases:
            with self.subTest(name=name):
                self._prepare_download(content or b"unused", **kwargs)
                before = self._snapshot()
                previous_backups = set((self.state_dir / "backups").glob("panel-*.sh"))
                if name == "download":
                    result = self.shell('download_https() { return 1; }; update_panel_locked')
                else:
                    result = self._update_shell()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(self.wrapper.read_bytes(), self.old_wrapper)
                backups = sorted(set((self.state_dir / "backups").glob("panel-*.sh")) - previous_backups)
                if name == "help":
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), self.old_wrapper)
                    self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
                else:
                    self.assertEqual(backups, [])

    def test_help_timeout_kills_hung_candidate_after_validating_timeout_arguments(self) -> None:
        real_timeout = shutil.which("timeout")
        if not real_timeout:
            self.skipTest("timeout is required for the panel help timeout test")
        candidate = self._candidate(help_hangs=True)
        self._prepare_download(candidate)
        timeout_log = self.root / "timeout-args.log"
        shim = self.bin / "timeout"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n%s\\n' \"$1\" \"$2\" >\"$PANEL_TIMEOUT_LOG\"\n"
            "[[ $1 == --kill-after=2s && $2 == 15s ]] || exit 90\n"
            "shift 2\n"
            "exec \"$PANEL_REAL_TIMEOUT\" --kill-after=.1s .2s \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        self.env["PANEL_REAL_TIMEOUT"] = real_timeout
        self.env["PANEL_TIMEOUT_LOG"] = str(timeout_log)
        started = time.monotonic()
        result = self._update_shell()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(timeout_log.read_text(encoding="utf-8").splitlines()[:2], ["--kill-after=2s", "15s"])
        self.assertEqual(self.wrapper.read_bytes(), self.old_wrapper)
        backups = sorted((self.state_dir / "backups").glob("panel-*.sh"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old_wrapper)

    def test_wrong_digest_is_rejected_when_python_optimized(self) -> None:
        candidate = self._candidate()
        self._prepare_download(candidate, sha="0" * 40)
        self.env["PYTHONOPTIMIZE"] = "1"
        before = self._snapshot()
        result = self._update_shell()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.wrapper.read_bytes(), self.old_wrapper)
        self.assertEqual(list((self.state_dir / "backups").glob("panel-*.sh")), [])

    def test_atomic_copy_failure_after_write_restores_old_wrapper(self) -> None:
        candidate = self._candidate()
        self._prepare_download(candidate)
        before = self._snapshot()
        body = r'''
real_atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"
}
atomic_copy() { real_atomic_copy "$@"; return 1; }
'''
        result = self._update_shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.wrapper.read_bytes(), self.old_wrapper)

    def test_term_after_wrapper_write_restores_old_wrapper(self) -> None:
        candidate = self._candidate()
        self._prepare_download(candidate)
        before = self._snapshot()
        marker = self.root / "term-once"
        self.env["PANEL_TERM_ONCE"] = str(marker)
        body = r'''
real_atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"
}
atomic_copy() {
    real_atomic_copy "$@" || return
    if [[ $2 == "$WRAPPER" && ! -e "$PANEL_TERM_ONCE" ]]; then
        : >"$PANEL_TERM_ONCE"
        kill -TERM "$BASHPID"
    fi
}
'''
        result = self._update_shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.wrapper.read_bytes(), self.old_wrapper)

    def test_symlink_wrapper_is_rejected_without_touching_target(self) -> None:
        protected = self.root / "protected-wrapper"
        protected.write_bytes(self.old_wrapper)
        self.wrapper.unlink()
        self.wrapper.symlink_to(protected)
        self._prepare_download(self._candidate())
        result = self._update_shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(protected.read_bytes(), self.old_wrapper)
        self.assertTrue(self.wrapper.is_symlink())

    def test_cli_update_panel_routes_lock_and_rejects_arguments(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
check_conflicts() { printf 'CHECK\n'; }
install_dependencies() { printf 'DEPS\n'; }
with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
update_panel_locked() { printf 'UPDATED\n'; }
main update-panel
main update-panel extra >/dev/null 2>&1 || printf 'INVALID\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHECK", result.stdout)
        self.assertIn("DEPS", result.stdout)
        self.assertIn("LOCK:update_panel_locked", result.stdout)
        self.assertIn("UPDATED", result.stdout)
        self.assertIn("INVALID", result.stdout)

    @unittest.skipUnless(pty is not None, "PTY support is required for menu routing")
    def test_menu_fourteen_runs_real_update_and_loads_new_menu_after_lock_release(self) -> None:
        self._prepare_download(self._menu_candidate())
        command = (
            'source "$1"; '
            'require_systemd() { return 0; }; '
            'check_conflicts() { return 0; }; '
            'install_dependencies() { return 0; }; '
            'download_https() { cp -- "$SB_PANEL_FIXTURE" "$2"; }; '
            'menu'
        )
        env = self.env.copy()
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", "-c", command, "menu-test", str(fixture.SCRIPT)], env)
        os.write(fd, b"14\n")
        chunks: list[bytes] = []
        deadline = time.time() + 5
        status: int | None = None
        while time.time() < deadline:
            import select
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
            self.fail("menu PTY did not exit before timeout: " + b"".join(chunks).decode("utf-8", errors="replace"))
        output = b"".join(chunks).decode("utf-8", errors="replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("NEW_MENU_WITH_UNLOCKED_LOCK", output, output)


if __name__ == "__main__":
    unittest.main()
