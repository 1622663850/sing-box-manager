#!/usr/bin/env python3
"""Focused tests for IPv4/IPv6 outbound DNS preference configuration."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture

@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class IpPreferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def calls(self) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, self.system_log)

    def _source(self, value: dict, name: str = "source.jsonc") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _base(self) -> dict:
        return {
            "log": {"level": "info"},
            "dns": {"servers": [
                {"tag": "local", "type": "local"},
                {"tag": "public", "type": "udp", "server": "1.1.1.1"},
            ]},
            "route": {"rules": [{"domain_suffix": ["example.com"], "action": "route", "outbound": "direct"}]},
            "inbounds": [],
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "custom": {"keep": True},
        }

    def _generate(self, value: dict, preference: str) -> dict:
        source = self._source(value, preference + ".jsonc")
        result = self.shell('generate_outbound_preference "$1" "$2"', str(source), preference, check=True)
        return json.loads(result.stdout)

    def test_generate_both_preferences_preserves_config_and_resolver_forms(self) -> None:
        value = self._base()
        value["dns"]["final"] = "public"
        value["route"]["default_domain_resolver"] = "public"
        for preference in ("prefer_ipv6", "prefer_ipv4"):
            with self.subTest(preference=preference):
                result = self._generate(value, preference)
                self.assertEqual(result["dns"]["strategy"], preference)
                self.assertEqual(result["route"]["default_domain_resolver"], {"server": "public", "strategy": preference})
                self.assertEqual(result["route"]["rules"], value["route"]["rules"])
                self.assertEqual(result["custom"], value["custom"])
                if fixture.REAL_CORE:
                    path = self.root / (preference + "-core-check.json")
                    fixture.dump(path, {k:v for k,v in result.items() if k != "custom"})
                    checked = subprocess.run([fixture.REAL_CORE, "check", "-c", str(path)], capture_output=True, text=True, timeout=20)
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

        object_value = self._base()
        object_value["route"]["default_domain_resolver"] = {"server": "public", "disable_cache": True}
        object_result = self._generate(object_value, "prefer_ipv6")
        self.assertEqual(object_result["route"]["default_domain_resolver"], {
            "server": "public", "disable_cache": True, "strategy": "prefer_ipv6",
        })

    def test_generate_chooses_dns_final_then_first_tag_and_adds_local_server(self) -> None:
        value = self._base()
        value["dns"]["final"] = "public"
        result = self._generate(value, "prefer_ipv4")
        self.assertEqual(result["route"]["default_domain_resolver"]["server"], "public")
        no_final = self._base()
        no_final["dns"].pop("final", None)
        result = self._generate(no_final, "prefer_ipv6")
        self.assertEqual(result["route"]["default_domain_resolver"]["server"], "local")
        for servers in (None, []):
            value = self._base()
            if servers is None:
                value["dns"].pop("servers")
            else:
                value["dns"]["servers"] = servers
            result = self._generate(value, "prefer_ipv6")
            self.assertEqual(result["dns"]["servers"], [{"type": "local", "tag": "local"}])
            self.assertEqual(result["route"]["default_domain_resolver"]["server"], "local")

    def test_generate_rejects_bad_direction_and_ambiguous_dns_reference(self) -> None:
        for preference in ("ipv6", "", "prefer_tcp"):
            result = self.shell('generate_outbound_preference "$1" "$2"', str(self._source(self._base())), preference)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
        no_tag = self._base()
        no_tag["dns"]["servers"][0].pop("tag")
        result = self.shell('generate_outbound_preference "$1" "$2"', str(self._source(no_tag)), "prefer_ipv6")
        self.assertNotEqual(result.returncode, 0)
        unknown = self._base()
        unknown["route"]["default_domain_resolver"] = "missing"
        result = self.shell('generate_outbound_preference "$1" "$2"', str(self._source(unknown)), "prefer_ipv6")
        self.assertNotEqual(result.returncode, 0)
        bad = self.root / "bad.jsonc"
        bad.write_text('{"dns": /* broken', encoding="utf-8")
        result = self.shell('generate_outbound_preference "$1" "$2"', str(bad), "prefer_ipv6")
        self.assertNotEqual(result.returncode, 0)

    def test_configure_preserves_jsonc_stopped_state_and_creates_backup(self) -> None:
        self.config.write_text(
            '{\n  // keep IP preference comment\n  "dns": {"servers":[{"tag":"local","type":"local"}]},\n'
            '  "inbounds": [], "outbounds":[{"type":"direct","tag":"direct"}]\n}\n',
            encoding="utf-8",
        )
        before_metadata = (self.state_dir / "clients").exists()
        result = self.shell('confirm() { return 0; }; configure_outbound_preference_locked prefer_ipv6', check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual(saved["dns"]["strategy"], "prefer_ipv6")
        self.assertEqual(saved["route"]["default_domain_resolver"]["server"], "local")
        self.assertIn("// keep IP preference comment", self.config.read_text(encoding="utf-8"))
        self.assertFalse(self.state()["active"])
        self.assertFalse(any(any(action in call for action in ("start", "restart")) for call in self.calls()))
        backups = sorted((self.state_dir / "backups").glob("bundle-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(before_metadata, (self.state_dir / "clients").exists())

    def test_configure_active_restart_failure_and_cancel_rollback(self) -> None:
        self.config.write_text(json.dumps(self._base()) + "\n", encoding="utf-8")
        before = self.config.read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell('confirm() { return 0; }; configure_outbound_preference_locked prefer_ipv4')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(self.state()["active"])
        result = self.shell('confirm() { return 1; }; configure_outbound_preference_locked prefer_ipv6')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_configure_noop_does_not_confirm_backup_or_restart(self) -> None:
        value = self._generate(self._base(), "prefer_ipv6")
        fixture.dump(self.config, value)
        before = self.config.read_bytes()
        marker = self.root / "confirm-marker"
        self.env["IP_CONFIRM_MARKER"] = str(marker)
        result = self.shell(
            'confirm() { printf x > "$IP_CONFIRM_MARKER"; return 99; }; configure_outbound_preference_locked prefer_ipv6',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list((self.state_dir / "backups").glob("bundle-*.json")), [])
        self.assertFalse(any("restart" in call for call in self.calls()))

    def test_main_menu_cancel_and_status_stay_in_submenu(self) -> None:
        from test_menu_navigation import MenuNavigationTest
        before = self.config.read_bytes()
        output = MenuNavigationTest.navigate(self, [
            ("请输入选项", "19\n", None),
            ("请选择出站策略", "1\n", None),
            ("保存出站 IP 优先级？", "n\n", None),
            ("按回车键返回出站设置菜单", "\n", None),
            ("请选择出站策略", "3\n", "========== sing-box 管理面板"),
            ("按回车键返回出站设置菜单", "\n", None),
            ("请选择出站策略", "0\n", "========== sing-box 管理面板"),
            ("请输入选项", "0\n", "按回车键返回"),
        ])
        self.assertIn("DNS 默认解析", output)
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
