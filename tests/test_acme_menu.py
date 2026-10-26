#!/usr/bin/env python3
"""Offline tests for the optional acme-script menu bridge."""

from __future__ import annotations

import base64
import hashlib
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
except ImportError:  # pragma: no cover - Windows has no PTY support.
    pty = None


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class AcmeMenuTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.acme_root = self.root / "root"
        self.acme_dir = self.acme_root / "acme-script"
        self.acme_script = self.acme_dir / "acme_cf_token.sh"
        self.legacy_script = self.acme_root / "acme_cf_token.sh"
        self.acme_state = self.acme_root / ".acme.sh"
        self.acme_state.mkdir(parents=True)
        (self.acme_state / "account.conf").write_text("existing-acme-state\n", encoding="utf-8")
        self.cron = self.root / "var/spool/cron"
        self.cron.mkdir(parents=True)
        (self.cron / "erpdeploy").write_text("existing-cron\n", encoding="utf-8")
        self.fixture_json = self.root / "acme-release.json"
        self.env["ACME_FIXTURE"] = str(self.fixture_json)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def _candidate(self, *, menu_lock_check: bool = True, syntax_ok: bool = True) -> bytes:
        if not syntax_ok:
            return (
                "#!/usr/bin/env bash\n"
                "# https://github.com/1622663850/acme-script\n"
                "menu() { if (; then\n"
            ).encode("utf-8")
        check = (
            '  [[ ! -e /proc/$$/fd/9 ]] || exit 1\n'
            '  lock="${SB_ROOT_PREFIX}/var/lib/sing-box-manager/manager.lock"\n'
            '  exec 9>"$lock" || exit 1\n'
            '  flock -n 9 || exit 1\n'
            '  printf "ACME_MENU_OK\\n"\n'
        ) if menu_lock_check else '  printf "ACME_MENU_OK\\n"\n'
        return (
            "#!/usr/bin/env bash\n"
            "# https://github.com/1622663850/acme-script\n"
            "menu() {\n"
            + check +
            "}\n"
            'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then menu "$@"; fi\n'
        ).encode("utf-8")

    def _payload(self, content: bytes, *, size: int | None = None, sha: str | None = None,
                 path: str = "acme_cf_token.sh", encoding: str = "base64", kind: str = "file",
                 raw_content: str | None = None) -> None:
        digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        payload = {
            "type": kind,
            "path": path,
            "encoding": encoding,
            "size": len(content) if size is None else size,
            "sha": digest if sha is None else sha,
            "content": base64.b64encode(content).decode("ascii") if raw_content is None else raw_content,
        }
        self.fixture_json.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _download_stub(self) -> str:
        return r'''
download_https() {
    [[ $1 == "https://api.github.com/repos/1622663850/acme-script/contents/acme_cf_token.sh?ref=main" ]] || return 77
    cp -- "$ACME_FIXTURE" "$2"
}
'''

    def _protected_snapshot(self) -> dict[str, object]:
        paths = [self.config, self.core, self.unit, self.marker]
        values: dict[str, object] = {
            str(path): path.read_bytes() if path.exists() else None
            for path in paths
        }
        values["service"] = self.mock_state.read_bytes()
        values["systemctl"] = self.system_log.read_bytes() if self.system_log.exists() else None
        values["acme-state"] = {
            str(path.relative_to(self.acme_state)): path.read_bytes()
            for path in self.acme_state.rglob("*") if path.is_file()
        }
        values["cron"] = {
            str(path.relative_to(self.cron)): path.read_bytes()
            for path in self.cron.rglob("*") if path.is_file()
        }
        return values

    def test_first_download_validates_and_installs_menu_without_touching_existing_state(self) -> None:
        candidate = self._candidate()
        self._payload(candidate)
        before = self._protected_snapshot()
        result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.acme_script)
        self.assertEqual(self.acme_script.read_bytes(), candidate)
        self.assertEqual(self.acme_script.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self._protected_snapshot(), before)
        self.assertFalse(self.legacy_script.exists())

    def test_repeated_open_skips_download_and_reuses_legacy_path(self) -> None:
        candidate = self._candidate()
        self.acme_dir.mkdir(parents=True)
        self.acme_script.write_bytes(candidate)
        self.acme_script.chmod(0o700)
        result = self.shell('download_https() { return 1; }; ensure_acme_menu_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.acme_script)

        self.acme_script.unlink()
        self.legacy_script.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_script.write_bytes(candidate)
        self.legacy_script.chmod(0o700)
        result = self.shell('download_https() { return 1; }; ensure_acme_menu_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.legacy_script)

    def test_bad_legacy_path_is_preserved_while_dedicated_path_is_installed(self) -> None:
        self.legacy_script.parent.mkdir(parents=True, exist_ok=True)
        legacy = b"legacy non-project script\n"
        self.legacy_script.write_bytes(legacy)
        self._payload(self._candidate())
        result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.acme_script)
        self.assertEqual(self.legacy_script.read_bytes(), legacy)
        self.assertEqual(self.acme_script.read_bytes(), self._candidate())

    def test_bad_existing_file_and_symlinks_are_never_overwritten(self) -> None:
        self.acme_dir.mkdir(parents=True)
        self.acme_script.write_text("bad existing file\n", encoding="utf-8")
        self._payload(self._candidate())
        before = self.acme_script.read_bytes()
        result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.acme_script.read_bytes(), before)

        protected = self.root / "protected-acme"
        protected.write_bytes(b"protected\n")
        self.acme_script.unlink()
        self.acme_script.symlink_to(protected)
        result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(protected.read_bytes(), b"protected\n")

        self.acme_script.unlink()
        self.acme_dir.rmdir()
        self.acme_dir.symlink_to(self.root / "protected-dir", target_is_directory=True)
        (self.root / "protected-dir").mkdir()
        result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)

    def test_download_and_content_validation_fail_without_install(self) -> None:
        cases = [
            ("download", None, {}),
            ("path", self._candidate(), {"path": "other.sh"}),
            ("type", self._candidate(), {"kind": "dir"}),
            ("size", self._candidate(), {"size": len(self._candidate()) + 1}),
            ("digest", self._candidate(), {"sha": "0" * 40}),
            ("base64", self._candidate(), {"raw_content": "%%%"}),
            ("no-project-url", self._candidate().replace(b"https://github.com/1622663850/acme-script", b"https://example.invalid/acme"), {}),
            ("no-menu", self._candidate().replace(b"menu() {", b"other() {"), {}),
            ("syntax", self._candidate(syntax_ok=False), {}),
        ]
        for name, content, kwargs in cases:
            with self.subTest(name=name):
                self._payload(content or b"unused", **kwargs)
                before = self._protected_snapshot()
                if name == "download":
                    result = self.shell('download_https() { return 1; }; ensure_acme_menu_locked')
                else:
                    result = self.shell(self._download_stub() + "ensure_acme_menu_locked")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._protected_snapshot(), before)
                self.assertFalse(self.acme_script.exists())

    def test_publish_ln_failure_and_term_remove_same_inode_partial_install(self) -> None:
        candidate = self._candidate()
        self._payload(candidate)
        body = r'''
ln() { command ln "$@"; return 1; }
'''
        result = self.shell(self._download_stub() + body + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.acme_script.exists())
        self.assertFalse(list(self.state_dir.glob("acme-menu.*")))

        body = r'''
ln() {
    command ln "$@"
    kill -TERM "$BASHPID"
}
'''
        result = self.shell(self._download_stub() + body + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.acme_script.exists())
        self.assertFalse(list(self.state_dir.glob("acme-menu.*")))

    def test_term_while_staging_install_cleans_known_tmp_before_publish(self) -> None:
        candidate = self._candidate()
        self._payload(candidate)
        self.env["ACME_TERM_ONCE"] = str(self.root / "acme-install-term-once")
        body = r'''
install() {
    command install "$@"
    local target="${!#}"
    if [[ $target == "$ACME_MENU_SCRIPT.tmp."* && ! -e "$ACME_TERM_ONCE" ]]; then
        : >"$ACME_TERM_ONCE"
        kill -TERM "$BASHPID"
    fi
}
'''
        result = self.shell(self._download_stub() + body + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.acme_script.exists())
        self.assertFalse(list(self.acme_dir.glob("acme_cf_token.sh.tmp.*")) if self.acme_dir.exists() else [])
        self.assertFalse(list(self.state_dir.glob("acme-menu.*")))

    def test_external_final_created_before_ln_is_preserved(self) -> None:
        candidate = self._candidate()
        self._payload(candidate)
        external = b"external concurrent writer\n"
        self.env["ACME_EXTERNAL_ONCE"] = str(self.root / "acme-external-once")
        body = r'''
ln() {
    local target="${!#}"
    if [[ $target == "$ACME_MENU_SCRIPT" && ! -e "$ACME_EXTERNAL_ONCE" ]]; then
        printf 'external concurrent writer\n' >"$target"
        : >"$ACME_EXTERNAL_ONCE"
    fi
    command ln "$@"
}
'''
        result = self.shell(self._download_stub() + body + "ensure_acme_menu_locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.acme_script.read_bytes(), external)
        self.assertFalse(list(self.acme_dir.glob("acme_cf_token.sh.tmp.*")))
        self.assertFalse(list(self.state_dir.glob("acme-menu.*")))

    def test_open_releases_manager_lock_and_fd_before_external_menu(self) -> None:
        candidate = self._candidate(menu_lock_check=True)
        self._payload(candidate)
        result = self.shell(self._download_stub() + "open_acme_menu")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ACME_MENU_OK", result.stdout)

    def test_cli_acme_arguments_and_menu_seventeen_route(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
main acme extra >/dev/null 2>&1 || printf 'INVALID\n'
open_acme_menu() { printf 'ACME_CLI\n'; }
main acme
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ACME_CLI", result.stdout)
        self.assertIn("INVALID", result.stdout)

    @unittest.skipUnless(pty is not None, "PTY support is required for menu routing")
    def test_main_menu_seventeen_opens_downloaded_menu(self) -> None:
        self._payload(self._candidate(menu_lock_check=True))
        command = (
            'source "$1"; '
            'download_https() { cp -- "$ACME_FIXTURE" "$2"; }; '
            'menu'
        )
        env = self.env.copy()
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", "-c", command, "acme-menu", str(fixture.SCRIPT)], env)
        os.write(fd, b"17\n\n0\n")
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
            try:
                os.close(fd)
            except OSError:
                pass
            self.fail("ACME menu PTY did not exit")
        output = b"".join(chunks).decode("utf-8", errors="replace")
        try:
            os.close(fd)
        except OSError:
            pass
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
        self.assertIn("ACME_MENU_OK", output)


if __name__ == "__main__":
    unittest.main()
