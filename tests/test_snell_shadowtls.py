#!/usr/bin/env python3
"""Focused tests for the managed Snell v5 plus ShadowTLS v3 group."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class SnellShadowTlsTest(unittest.TestCase):
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

    def group(self, tag: str = "stls", port: int = 25701) -> dict:
        result = self.shell(
            'generate_snell_shadowtls "$@"', tag, str(port), "snell-psk", "shadow-password", "origin.example",
            "443", "sni.example", check=True,
        )
        return json.loads(result.stdout)

    def _write_group(self, group: dict, name: str = "group.json") -> Path:
        path = self.root / name
        fixture.dump(path, group)
        return path

    def _metadata(self, group: dict, **extra: object) -> Path:
        tag = group["backend"]["tag"]
        path = self.state_dir / "clients" / (tag + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"server": "example.com", "public_key": "", "inbound": group["backend"], "shadowtls_group": group}
        value.update(extra)
        fixture.dump(path, value)
        return path

    def test_generate_validate_and_reject_unknown_group_fields(self) -> None:
        group = self.group()
        backend = group["backend"]
        front = group["front"]
        self.assertEqual(group["format"], "sing-box-manager-snell-shadowtls-v1")
        self.assertEqual(backend["type"], "snell")
        self.assertEqual(backend["version"], 5)
        self.assertEqual(backend["listen"], "127.0.0.1")
        self.assertEqual(backend["listen_port"], 0)
        self.assertEqual(backend["obfs_mode"], "none")
        self.assertEqual(front["type"], "shadowtls")
        self.assertEqual(front["version"], 3)
        self.assertEqual(front["users"][0]["name"], "default")
        self.assertEqual(front["detour"], "stls")
        self.assertTrue(front["strict_mode"])
        path = self._write_group(group)
        self.shell('validate_snell_shadowtls_group "$1"', str(path), check=True)
        unknown = copy.deepcopy(group)
        unknown["unexpected"] = True
        bad = self._write_group(unknown, "unknown-group.json")
        self.assertNotEqual(self.shell('validate_snell_shadowtls_group "$1"', str(bad)).returncode, 0)

    def test_clients_require_complete_group_and_keep_detour_dependency(self) -> None:
        group = self.group("client-stls", 25702)
        path = self._write_group(group, "client-group.json")
        result = self.shell('generate_snell_shadowtls_clients "$1" "$2" "$3"', str(path), "example.com", "client", check=True)
        clients = json.loads(result.stdout)
        self.assertEqual([item["type"] for item in clients["outbounds"]], ["snell", "shadowtls"])
        self.assertEqual(clients["outbounds"][0]["version"], 4)
        self.assertEqual(clients["outbounds"][0]["detour"], "client:shadowtls")
        self.assertEqual(clients["outbounds"][1]["version"], 3)
        self.assertEqual(clients["route"], {"final": "client"})
        broken = copy.deepcopy(group)
        broken["front"]["detour"] = "wrong"
        broken_path = self._write_group(broken, "broken-client-group.json")
        self.assertNotEqual(self.shell('generate_snell_shadowtls_clients "$1" "$2" "$3"', str(broken_path), "example.com", "client").returncode, 0)

    def test_add_group_saves_pair_metadata_and_starts_stopped_service(self) -> None:
        group = self.group("add-stls", 25703)
        path = self._write_group(group, "add-group.json")
        result = self.shell('confirm() { return 0; }; add_snell_shadowtls_locked "$1" "$2"', str(path), "example.com", check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual([node["tag"] for node in config["inbounds"]], ["add-stls", "add-stls-stls"])
        metadata = json.loads((self.state_dir / "clients/add-stls.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["shadowtls_group"], group)
        self.assertTrue(any("start" in call for call in self.calls()))

    def test_save_preserves_comments_and_unrelated_metadata_and_delete_reference_protection(self) -> None:
        group = self.group("save-stls", 25704)
        path = self._write_group(group, "save-group.json")
        self.shell('confirm() { return 0; }; add_snell_shadowtls_locked "$1" "$2"', str(path), "example.com", check=True)
        self.config.write_text(self.config.read_text(encoding="utf-8").replace("{\n", "{\n  // keep pair comment\n", 1), encoding="utf-8")
        metadata_path = self.state_dir / "clients/save-stls.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["outbound_route"] = {"format": "sing-box-manager-outbound-route-v1", "rule": {"inbound": ["save-stls"], "action": "route", "outbound": "proxy"}}
        fixture.dump(metadata_path, metadata)
        saved = copy.deepcopy(group)
        saved["sni"] = "new-sni.example"
        saved_path = self._write_group(saved, "saved-group.json")
        self.shell('confirm() { return 0; }; save_snell_shadowtls_locked "$1" "$2" "$3"', "save-stls", str(saved_path), "example.com", check=True)
        self.assertIn("// keep pair comment", self.config.read_text(encoding="utf-8"))
        metadata_after = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata_after["outbound_route"], metadata["outbound_route"])

        config = json.loads(self.shell("config_jq .", check=True).stdout)
        config.setdefault("route", {})["final"] = "save-stls-stls"
        fixture.dump(self.config, config)
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 0; }; delete_snell_shadowtls_locked "$1"', "save-stls")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        config["route"].pop("final")
        fixture.dump(self.config, config)
        self.shell('confirm() { return 0; }; delete_snell_shadowtls_locked "$1"', "save-stls", check=True)
        self.assertNotIn("save-stls", [node["tag"] for node in json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"]])
        self.assertFalse(metadata_path.exists())

    def test_read_group_rejects_external_change_and_client_export_is_pair(self) -> None:
        group = self.group("read-stls", 25705)
        path = self._write_group(group, "read-group.json")
        self.shell('confirm() { return 0; }; add_snell_shadowtls_locked "$1" "$2"', str(path), "example.com", check=True)
        read = json.loads(self.shell('read_snell_shadowtls_group "$1"', "read-stls", check=True).stdout)
        self.assertEqual(read, group)
        current = json.loads(self.shell("config_jq .", check=True).stdout)
        current["inbounds"][0]["psk"] = "external"
        fixture.dump(self.config, current)
        self.assertNotEqual(self.shell('read_snell_shadowtls_group "$1"', "read-stls").returncode, 0)
        # Restore the pair and exercise both JSON and link-only output modes.
        self._write_config_pair(group)
        full = self.shell('set -o pipefail; show_snell_shadowtls_client "$1" "$2" json-only | jq -s .', "read-stls", "example.com", check=True)
        exported = json.loads(full.stdout)
        self.assertEqual(len(exported), 2)
        link = self.shell('show_snell_shadowtls_client "$1" "$2" link-only', "read-stls", "example.com", check=True).stdout
        self.assertIn("snell", link)
        self.assertIn("shadow-tls-version=3", link)
        surge = self.shell('snell_shadowtls_surge "$1" "$2" "$3"', str(path), "example.com", "read", check=True).stdout
        self.assertIn("shadow-tls-version=3", surge)
        self.assertIn("snell", surge)

    def _write_config_pair(self, group: dict) -> None:
        fixture.dump(self.config, {**self.original, "inbounds": [group["backend"], group["front"]], "outbounds": [{"type": "direct", "tag": "direct"}]})


if __name__ == "__main__":
    unittest.main()
