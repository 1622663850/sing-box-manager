#!/usr/bin/env python3
"""Focused tests for starting sing-box after adding a node while stopped."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class AddStartTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def tls_material(self, common_name: str = "example.com") -> tuple[Path, Path]:
        return fixture.ManagerTest.tls_material(self, common_name)

    def calls(self) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, self.system_log)

    def inbound_file(self, tag: str = "added-node", port: int = 28501) -> Path:
        path = self.root / (tag + ".json")
        fixture.dump(path, self.inbound(tag, port))
        return path

    def inbound(self, tag: str, port: int) -> dict:
        return fixture.ManagerTest.inbound(self, tag, port)

    def _assert_started_without_enable(self) -> None:
        calls = self.calls()
        self.assertTrue(any("start" in call for call in calls), calls)
        self.assertFalse(any("restart" in call for call in calls), calls)
        self.assertFalse(any("enable" in call for call in calls), calls)
        self.assertTrue(self.state()["active"])

    def test_add_managed_inbound_starts_stopped_service_and_saves_metadata(self) -> None:
        path = self.inbound_file()
        result = self.shell(
            'wait_healthy(){ service_is_active; }; add_managed_inbound "$1" "example.com" ""',
            str(path), check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_started_without_enable()
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["inbounds"], [self.inbound("added-node", 28501)])
        metadata = json.loads((self.state_dir / "clients/added-node.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["inbound"], saved["inbounds"][0])

    def test_add_single_inbound_file_starts_stopped_service(self) -> None:
        path = self.inbound_file("single-file", 28502)
        result = self.shell(
            'wait_healthy(){ service_is_active; }; add_inbound_file "$1"',
            str(path), check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_started_without_enable()
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]["tag"], "single-file")

    def test_active_service_restarts_after_add_and_enabled_flag_is_preserved(self) -> None:
        self.state(active=True, enabled=True)
        path = self.inbound_file("active-added", 28503)
        result = self.shell(
            'wait_healthy(){ service_is_active; }; add_managed_inbound "$1" "example.com" ""',
            str(path), check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertTrue(any("restart" in call for call in calls), calls)
        self.assertFalse(any("start" in call for call in calls), calls)
        self.assertFalse(any("enable" in call for call in calls), calls)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])

    def test_start_failure_rolls_back_config_metadata_and_service(self) -> None:
        path = self.inbound_file("start-fails", 28504)
        before_config = self.config.read_bytes()
        self.state(start_failures=1)
        result = self.shell('add_managed_inbound "$1" "example.com" ""', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse((self.state_dir / "clients/start-fails.json").exists())
        self.assertFalse(self.state()["active"])

    def test_start_health_failure_rolls_back_partial_active_service(self) -> None:
        path = self.inbound_file("health-fails", 28505)
        before_config = self.config.read_bytes()
        self.state(start_unhealthy_once=True)
        result = self.shell('add_managed_inbound "$1" "example.com" ""', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse((self.state_dir / "clients/health-fails.json").exists())
        self.assertFalse(self.state()["active"])

    def test_conflicting_add_does_not_start_stopped_service(self) -> None:
        existing = self.inbound("existing", 28506)
        fixture.dump(self.config, {**self.original, "inbounds": [existing]})
        path = self.inbound_file("other", 28506)
        result = self.shell('add_managed_inbound "$1" "example.com" ""', str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("start" in call for call in self.calls()))
        self.assertFalse(self.state()["active"])

    def test_outer_apply_bundle_term_after_success_rolls_back_started_hy2_and_record(self) -> None:
        certificate, private_key = self.tls_material()
        node = fixture.ManagerTest.generate_inbound(
            self, "hysteria2", "hy-password", tls_server_name="example.com",
            certificate=certificate, key=private_key,
        ) | {"tag": "term-hy2", "listen_port": 28507, "ignore_client_bandwidth": True}
        inbound_path = self.root / "term-hy2.json"
        fixture.dump(inbound_path, node)
        record = json.dumps({
            "format": "sing-box-manager-quic-bbr-v1",
            "type": "hysteria2",
            "settings": {},
        }, separators=(",", ":"))
        before_config = self.config.read_bytes()
        result = self.shell(
            r'''
            eval "$(declare -f commit_config | sed '1s/^commit_config/real_commit_config/')"
            commit_config() {
                real_commit_config "$@"
                local result=$?
                [[ $result -eq 0 ]] && kill -TERM "$BASHPID"
                return "$result"
            }
            add_managed_inbound "$1" "example.com" "" "$2"
            ''',
            str(inbound_path), record,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse((self.state_dir / "clients/term-hy2.json").exists())
        self.assertFalse(self.state()["active"])

    def test_malformed_single_inbound_is_rejected_without_start(self) -> None:
        malformed = self.root / "malformed-inbound.json"
        malformed.write_text('{"type":"vless","tag":"broken"\n', encoding="utf-8")
        before_config = self.config.read_bytes()
        result = self.shell('add_inbound_file "$1"', str(malformed))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse(any("start" in call for call in self.calls()))
        self.assertFalse(self.state()["active"])


if __name__ == "__main__":
    unittest.main()
