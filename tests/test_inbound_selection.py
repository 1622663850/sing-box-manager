#!/usr/bin/env python3
"""Inbound selection and index/name safety tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class InboundSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def inbound(self, tag: str, port: int) -> dict:
        return fixture.ManagerTest.inbound(self, tag, port)

    def _write_nodes(self, comments: bool = False) -> list[dict]:
        nodes = [self.inbound("ws-first", 18443), self.inbound("0", 18444), self.inbound("2", 18445)]
        value = {**self.original, "inbounds": nodes}
        if comments:
            self.config.write_text('{\n  // first comment\n  "inbounds": ' + json.dumps(nodes) + ',\n  "outbounds":[{"type":"direct","tag":"direct"}]\n}\n', encoding="utf-8")
        else:
            fixture.dump(self.config, value)
        clients = self.state_dir / "clients"
        clients.mkdir(parents=True, exist_ok=True)
        for node in nodes:
            fixture.dump(clients / f"{node['tag']}.json", {"server":"example.com", "inbound":node})
            (clients / f"{node['tag']}.json").chmod(0o600)
        return nodes

    def test_index_name_numeric_alias_cancel_and_empty_mapping(self) -> None:
        self._write_nodes()
        for selection, expected in (("1", "ws-first"), ("ws-first", "ws-first"), ("name:2", "2"), ("name:0", "0")):
            result = self.shell('TEST_SELECTION="$1"; prompt(){ printf -v "$1" "$TEST_SELECTION"; }; select_inbound selected edit; printf "\\nPICKED:%s\\n" "$selected"', selection)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(result.stdout.rstrip().endswith("PICKED:" + expected))
        result = self.shell('picked=stale; prompt(){ printf -v "$1" 0; }; select_inbound picked edit; printf "\\nPICKED:%s\\n" "$picked"')
        self.assertTrue(result.stdout.rstrip().endswith("PICKED:"))
        fixture.dump(self.config, {**self.original, "inbounds": []})
        result = self.shell('picked=stale; select_inbound picked edit; printf "\\nPICKED:%s\\n" "$picked"')
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("PICKED:"))

    def test_invalid_duplicate_and_injection_inputs_retry(self) -> None:
        self._write_nodes()
        marker = self.root / "selection-injection"
        self.env["INJECT_MARKER"] = str(marker)
        body = 'selection_step=0; prompt(){ if [[ "$1" == inbound_selection ]]; then selection_step=$((selection_step+1)); case $selection_step in 1) printf -v "$1" 999;; 2) printf -v "$1" \'$(touch "$INJECT_MARKER")\';; *) printf -v "$1" 2;; esac; fi; }; select_inbound selected edit; printf "%s" "$selected"'
        result = self.shell(body)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout[-1:], "0")
        self.assertFalse(marker.exists())
        value = json.loads(self.shell('config_jq .').stdout)
        value["inbounds"].append(self.inbound("ws-first", 18446))
        fixture.dump(self.config, value)
        result = self.shell('selection_step=0; prompt(){ selection_step=$((selection_step+1)); if [[ $selection_step == 1 ]]; then printf -v "$1" ws-first; else printf -v "$1" 0; fi; }; select_inbound picked edit; printf "\\nPICKED:%s\\n" "$picked"')
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("PICKED:"))
        self.assertIn("重复", result.stdout + result.stderr)

    def test_concurrent_reorder_and_credentials_are_protected(self) -> None:
        self._write_nodes()
        body = 'prompt(){ printf -v "$1" 1; printf "%s" "$(jq \' .inbounds |= reverse\' "$CONFIG_FILE")" >"$CONFIG_FILE"; }; select_inbound selected edit'
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        value = json.loads(self.shell('config_jq .').stdout)
        value["inbounds"][0]["users"][0]["uuid"] = "SECRET_UUID"
        fixture.dump(self.config, value)
        result = self.shell('prompt(){ printf -v "$1" 0; }; select_inbound selected edit')
        self.assertNotIn("SECRET_UUID", result.stdout)
        self.assertNotIn("password", result.stdout.lower())

    def test_edit_by_index_preserves_other_nodes_and_comments(self) -> None:
        nodes = self._write_nodes(comments=True)
        body = 'prompt(){ case "$1" in inbound_selection) printf -v "$1" 1;; tag) printf -v "$1" ws-first-updated;; port) printf -v "$1" 18453;; *) printf -v "$1" "%s" "${3:-}";; esac; }; prompt_secret(){ printf -v "$1" "%s" "${3:-}"; }; confirm(){ return 0; }; edit_wizard_locked'
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(self.shell('config_jq .').stdout)
        self.assertEqual([item["tag"] for item in value["inbounds"]], ["ws-first-updated", "0", "2"])
        self.assertEqual(value["inbounds"][1]["listen_port"], nodes[1]["listen_port"])
        self.assertIn("first comment", self.config.read_text(encoding="utf-8"))

    def test_delete_by_index_keeps_other_nodes_and_reference_protection(self) -> None:
        self._write_nodes(comments=True)
        result = self.shell('prompt(){ printf -v "$1" 1; }; confirm(){ return 0; }; delete_inbound')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([item["tag"] for item in json.loads(self.shell('config_jq .').stdout)["inbounds"]], ["0", "2"])
        self.assertTrue((self.state_dir / "clients/0.json").is_file())
        self.assertTrue(list((self.state_dir / "backups").glob("bundle-*.json")))
        self._write_nodes()
        value = json.loads(self.shell('config_jq .').stdout); value["route"] = {"rules":[{"inbound":["ws-first"]}],"final":"direct"}; fixture.dump(self.config, value)
        before = self.config.read_bytes()
        result = self.shell('prompt(){ printf -v "$1" 1; }; confirm(){ return 0; }; delete_inbound')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_cli_numeric_argument_stays_a_name_and_viewer_uses_index(self) -> None:
        self._write_nodes()
        result = self.shell('SB_ROOT_PREFIX=""; require_root(){ return 0; }; require_systemd(){ return 0; }; is_managed(){ return 0; }; confirm(){ return 0; }; main delete 2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([item["tag"] for item in json.loads(self.shell('config_jq .').stdout)["inbounds"]], ["ws-first", "0"])
        result = self.shell('require_core(){ return 0; }; prompt(){ if [[ "$1" == choice ]]; then printf -v "$1" 3; else printf -v "$1" 2; fi; }; show_client(){ printf "CLIENT:%s" "$1"; }; show_config')
        self.assertEqual(result.returncode, 0)
        self.assertIn("CLIENT:0", result.stdout)

    def test_selection_table_sanitizes_control_characters(self) -> None:
        node = self.inbound("\x1b[31mred", 18443)
        fixture.dump(self.config, {**self.original, "inbounds": [node]})
        result = self.shell('prompt(){ printf -v "$1" 0; }; select_inbound picked edit')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("\x1b", result.stdout)


if __name__ == "__main__":
    unittest.main()
