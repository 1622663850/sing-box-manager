#!/usr/bin/env python3
"""Exercise real nested terminal menus; credentials remain inside fixtures."""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
import unittest
import pty
import fcntl

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq required")
class MenuNavigationTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def navigate(self, steps: list[tuple[str, str, str | None]]) -> str:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 240, 0, 0))
        process = subprocess.Popen(
            ["bash", "-c", 'source "$1"; menu', "menu-navigation", str(fixture.SCRIPT)],
            env=self.env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        pending = b""
        transcript = b""
        try:
            for expected, answer, forbidden in steps:
                needle = expected.encode()
                deadline = time.monotonic() + 5
                while needle not in pending:
                    if forbidden:
                        self.assertNotIn(forbidden.encode(), pending, "returned to wrong menu")
                    self.assertLess(time.monotonic(), deadline, "expected menu prompt did not appear")
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            part = os.read(master, 65536)
                        except OSError:
                            part = b""
                        self.assertTrue(part, "menu exited before expected prompt")
                        pending += part
                        transcript += part
                position = pending.index(needle)
                if forbidden:
                    self.assertNotIn(forbidden.encode(), pending[:position], "returned to wrong menu")
                pending = pending[position + len(needle):]
                os.write(master, answer.encode())
            self.assertEqual(process.wait(timeout=5), 0)
            return transcript.decode(errors="replace")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            os.close(master)

    def test_missing_address_then_validate_stays_in_config_menu(self) -> None:
        node = {"type":"vless", "tag":"reality-fixture", "listen":"::", "listen_port":49751,
                "users":[{"uuid":"b7efdc96-87aa-4910-81b4-374b2eecc737", "flow":"xtls-rprx-vision"}],
                "tls":{"enabled":True, "server_name":"example.com", "reality":{"enabled":True,
                "handshake":{"server":"example.com","server_port":443},
                "private_key":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "short_id":["0123456789abcdef"]}}}
        value = self.original | {"inbounds":[node]}
        fixture.dump(self.config, value)
        before = self.config.read_bytes()
        output = self.navigate([
            ("请输入选项", "9\n", None), ("请选择 [1]", "1\n", None),
            ("按回车键返回", "\n", None),
            ("请选择 [1]", "4\n", "========== sing-box 管理面板"),
            ("按回车键返回", "\n", None),
            ("请选择 [1]", "0\n", "========== sing-box 管理面板"),
            ("请输入选项", "0\n", "按回车键返回"),
        ])
        self.assertIn("缺少当前配置的公网 IP/域名", output)
        self.assertEqual(self.config.read_bytes(), before)

    def test_maintenance_returns_to_config_before_main(self) -> None:
        self.navigate([
            ("请输入选项", "9\n", None), ("请选择 [1]", "8\n", None),
            ("请选择 [1]", "2\n", None), ("按回车键返回", "\n", None),
            ("请选择 [1]", "0\n", "========== sing-box 管理面板"),
            ("请选择 [1]", "0\n", "请输入选项"),
            ("请输入选项", "0\n", "按回车键返回"),
        ])

    def test_autostart_action_stays_in_submenu_and_keeps_running(self) -> None:
        fixture.ManagerTest.state(self, active=True, enabled=True)
        self.navigate([
            ("请输入选项", "4\n", None), ("请选择自启操作", "2\n", None),
            ("按回车键返回", "\n", None),
            ("请选择自启操作", "0\n", "========== sing-box 管理面板"),
            ("请输入选项", "0\n", "按回车键返回"),
        ])
        state = fixture.ManagerTest.state(self)
        self.assertTrue(state["active"])
        self.assertFalse(state["enabled"])

    def test_invalid_maintenance_choice_stays_at_same_level(self) -> None:
        output = self.navigate([
            ("请输入选项", "13\n", None), ("请选择 [1]", "99\n", None),
            ("按回车键返回", "\n", None),
            ("请选择 [1]", "0\n", "========== sing-box 管理面板"),
            ("请输入选项", "0\n", "按回车键返回"),
        ])
        self.assertIn("无效选项", output)


if __name__ == "__main__":
    unittest.main()
