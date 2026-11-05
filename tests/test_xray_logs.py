#!/usr/bin/env python3
"""Regression coverage for the Xray log viewer and live-follow menu."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import unittest

try:
    import pty
    import select
    import termios
except ImportError:  # pragma: no cover - Windows has no POSIX PTY.
    fcntl = pty = select = termios = None

import test_xray_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"),
                     "bash and jq are required")
class XrayLogsTest(fixture.XrayFixture):
    """The log viewer must use journalctl without mutating manager state."""

    def setUp(self) -> None:
        super().setUp()
        self.args_file = self.root / "journalctl-args.log"
        self.journalctl = self.bin / "journalctl"
        self.journalctl.write_text(r'''#!/usr/bin/env bash
set -u
{
    printf 'BEGIN\n'
    printf '%s\n' "$@"
    printf 'END\n'
} >> "$XR_LOG_ARGS"
if [[ " $* " == *" -f "* ]]; then
    printf '%s\n' "$$" > "$XR_LOG_FOLLOW_PID"
    trap 'rm -f -- "$XR_LOG_FOLLOW_PID"; exit 0' INT TERM
    printf 'follow-start\n'
    if [[ ${XR_LOG_MODE:-oneshot} == block ]]; then
        sleep 0.25
        printf 'follow-live\n'
        while :; do sleep 1; done
    fi
else
    printf 'finite-log\n'
fi
''', encoding="utf-8")
        self.journalctl.chmod(0o755)
        self.follow_pid = self.root / "journalctl-follow.pid"
        self.env.update(XR_LOG_ARGS=str(self.args_file), XR_LOG_MODE="oneshot",
                        XR_LOG_FOLLOW_PID=str(self.follow_pid))

    def _invocations(self) -> list[list[str]]:
        lines = self.args_file.read_text(encoding="utf-8").splitlines()
        result: list[list[str]] = []
        current: list[str] | None = None
        for line in lines:
            if line == "BEGIN":
                self.assertIsNone(current)
                current = []
            elif line == "END":
                self.assertIsNotNone(current)
                result.append(current or [])
                current = None
            elif current is not None:
                current.append(line)
        self.assertIsNone(current)
        return result

    def test_finite_and_follow_logs_keep_journalctl_arguments(self) -> None:
        before = self.config.read_bytes(), self.mock_state.read_bytes()
        fallback = self.shell("logs_menu", check=True)
        finite = self.shell("show_logs", check=True)
        followed = self.shell("show_logs --follow", check=True)
        self.assertEqual(fallback.stdout, "finite-log\n")
        self.assertEqual(finite.stdout, "finite-log\n")
        self.assertTrue(followed.stdout.endswith("follow-start\n"), followed.stdout)
        calls = self._invocations()
        self.assertEqual(calls[0], ["-u", "xray.service", "-n100", "--no-pager", "-o", "cat"])
        self.assertEqual(calls[1], ["-u", "xray.service", "-n100", "--no-pager", "-o", "cat"])
        self.assertEqual(calls[2], ["-u", "xray.service", "-n100", "-f", "--no-pager", "-o", "cat"])
        self.assertEqual((self.config.read_bytes(), self.mock_state.read_bytes()), before)

    @unittest.skipIf(os.name == "nt" or pty is None, "live-follow menu requires a POSIX PTY")
    def test_logs_menu_ctrl_c_returns_to_menu_and_then_exits(self) -> None:
        before = self.config.read_bytes(), self.mock_state.read_bytes()
        self.env["XR_LOG_MODE"] = "block"
        pid, master = pty.fork()
        if pid == 0:
            os.execve("/bin/bash", ["bash", "-c", 'source "$1"; logs_menu',
                                     "xray-logs-test", str(fixture.SCRIPT)], self.env)
        output = bytearray()

        def read_until(predicate, *, offset: int = 0, timeout: float = 12.0) -> None:
            deadline = time.monotonic() + timeout
            while not predicate(bytes(output)[offset:]):
                self.assertLess(time.monotonic(), deadline,
                                "logs menu stalled: " + output.decode(errors="replace"))
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        self.fail(f"PTY closed before expected log menu state: {error}")

        try:
            log_prompt = "请选择日志操作 [0]: ".encode()
            read_until(lambda data: log_prompt in data)
            follow_offset = len(output)
            os.write(master, b"2\n")
            read_until(lambda data: b"follow-live" in data, offset=follow_offset)
            self.assertTrue(self.follow_pid.exists())
            follower_pid = int(self.follow_pid.read_text(encoding="utf-8").strip())
            returned_offset = len(output)
            os.write(master, b"\x03")
            read_until(lambda data: log_prompt in data, offset=returned_offset)
            os.write(master, b"0\n")
            deadline = time.monotonic() + 8
            while True:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    self.assertTrue(os.WIFEXITED(status), output.decode(errors="replace"))
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    break
                self.assertLess(time.monotonic(), deadline, "logs menu did not exit")
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        pass  # EIO is the normal PTY EOF after the final 0 exits the menu.
            calls = self._invocations()
            self.assertGreaterEqual(len(calls), 1)
            self.assertEqual(calls[0], ["-u", "xray.service", "-n100", "-f", "--no-pager", "-o", "cat"])
            self.assertIn(b"follow-live", output)
            self.assertFalse(self.follow_pid.exists())
            with self.assertRaises(OSError):
                os.kill(follower_pid, 0)
            self.assertNotIn("[错误]", output.decode(errors="replace"))
            self.assertEqual((self.config.read_bytes(), self.mock_state.read_bytes()), before)
        finally:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == 0:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(master)

    @unittest.skipIf(os.name == "nt" or pty is None, "menu requires a POSIX PTY")
    def test_main_menu_option_seven_opens_log_menu_without_service_writes(self) -> None:
        before = self.config.read_bytes(), self.mock_state.read_bytes()
        pid, master = pty.fork()
        if pid == 0:
            os.execve("/bin/bash", ["bash", "-c", 'source "$1"; menu',
                                     "xray-main-test", str(fixture.SCRIPT)], self.env)
        output = bytearray()

        def wait_for(token: bytes, *, offset: int = 0) -> None:
            deadline = time.monotonic() + 10
            while token not in output[offset:]:
                self.assertLess(time.monotonic(), deadline, "main menu stalled")
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        self.fail(f"PTY closed before expected prompt: {error}")

        try:
            main_prompt = "请输入选项 [0-21]: ".encode()
            log_prompt = "请选择日志操作 [0]: ".encode()
            wait_for(main_prompt)
            os.write(master, b"7\n")
            wait_for(log_prompt)
            menu_after_choice_offset = len(output)
            os.write(master, b"1\n")
            wait_for(b"finite-log", offset=menu_after_choice_offset)
            wait_for(log_prompt, offset=menu_after_choice_offset)
            os.write(master, b"0\n")
            main_return_offset = len(output)
            wait_for(main_prompt, offset=main_return_offset)
            os.write(master, b"0\n")
            deadline = time.monotonic() + 8
            while True:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    break
                self.assertLess(time.monotonic(), deadline, "main menu did not exit")
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        pass  # EIO is the normal PTY EOF after the final 0 exits the menu.
            self.assertIn(b"finite-log", output)
            self.assertEqual((self.config.read_bytes(), self.mock_state.read_bytes()), before)
            self.assertEqual([call for call in self.calls()
                              if any(action in call for action in
                                     ("start", "stop", "restart", "enable", "disable"))], [])
        finally:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == 0:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(master)


if __name__ == "__main__":
    unittest.main()
