#!/usr/bin/env python3
"""Write-path regression tests for preserving JSONC source text."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_jsonc as jf
import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class JsoncWriteTest(unittest.TestCase):
    """Use the existing isolated fixture without inheriting another test class."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def shell_bytes(self, body: str, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                "bash", "-c",
                'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body,
                "test-manager", str(fixture.SCRIPT), *args,
            ],
            env=self.env,
            capture_output=True,
            timeout=20,
        )

    def calls(self, log: Path) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, log)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def inbound(self, tag: str = "first", port: int = 18443) -> dict:
        return fixture.ManagerTest.inbound(self, tag, port)

    def _write_preserve_fixture(self) -> tuple[str, dict, dict, str]:
        first = self.inbound("first", 18443)
        second = self.inbound("second", 18444)
        second["transport"]["path"] = "/second"
        old_uuid = first["users"][0]["uuid"]
        escaped_literal = json.dumps('quote " // /* keep */ \\', ensure_ascii=False)
        raw = (
            "{\n"
            "  // 顶层中文注释，必须保留\n"
            "  /* block 注释\n"
            "     不能被格式化重写 */\n"
            '  "x-url": "https://example.com/a//b",\n'
            f'  "x-escaped": {escaped_literal},\n'
            '  "log": {"level": "info", "timestamp": true},\n'
            '  "inbounds": [\n'
            "    {\n"
            '      "type": "vless", // first type\n'
            '      "tag": "first", // first tag\n'
            '      "listen": "127.0.0.1",\n'
            '      /* first port comment */\n'
            '      "listen_port": 18443,\n'
            '      "users": [{"uuid": "' + old_uuid + '"}], // old credential: ' + old_uuid + "\n"
            '      "transport": {"type": "ws", "path": "/vless"}\n'
            "    },\n"
            "    {\n"
            '      // untouched second node\n'
            '      "type": "vless",\n'
            '      "tag": "second",\n'
            '      "listen": "127.0.0.1",\n'
            '      "listen_port": 18444,\n'
            '      "users": [{"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737"}],\n'
            '      "transport": {"type": "ws", "path": "/second"}\n'
            "    }\n"
            "  ],\n"
            '  "outbounds": [{"type": "direct", "tag": "direct"}],\n'
            '  "route": {"final": "direct"} // inline close comment\n'
            "}\n"
        )
        self.config.write_text(raw, encoding="utf-8")
        clients = self.state_dir / "clients"
        fixture.dump(clients / "first.json", {"server": "example.com", "public_key": "", "inbound": first})
        fixture.dump(clients / "second.json", {"server": "example.com", "public_key": "", "inbound": second})
        (clients / "first.json").chmod(0o600)
        (clients / "second.json").chmod(0o600)
        return raw, first, second, old_uuid

    def test_edit_keeps_inline_comments_and_layout_while_changing_only_requested_fields(self) -> None:
        """Old write paths rewrite the whole file; this is the first red regression."""
        raw, first, second, old_uuid = self._write_preserve_fixture()
        second_metadata = (self.state_dir / "clients" / "second.json").read_bytes()
        changed = dict(first)
        changed["tag"] = "renamed"
        changed["listen_port"] = 18445
        changed["users"] = [{"uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]
        changed_file = self.root / "changed.json"
        fixture.dump(changed_file, changed)
        fixture.dump(self.root / "first-old.json", first)
        expected = raw.replace('"tag": "first"', '"tag": "renamed"', 1)
        expected = expected.replace('"listen_port": 18443', '"listen_port": 18445', 1)
        expected = expected.replace(old_uuid, changed["users"][0]["uuid"], 1)
        result = self.shell(
            'replace_inbound_file "$1" "$2" example.com "" "$3"',
            "first", str(changed_file), str(self.root / "first-old.json"),
        )
        # The expected snapshot is the old inbound object, not the full config.
        # Keep it separate so the test also exercises the optimistic snapshot gate.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.config.read_text(encoding="utf-8")
        self.assertEqual(output, expected)
        self.assertIn("// first type", output)
        self.assertIn("/* first port comment */", output)
        self.assertIn("// untouched second node", output)
        self.assertIn(old_uuid, output.split("// old credential:", 1)[1])
        self.assertIn(changed["users"][0]["uuid"], output)
        surviving = self.shell("config_jq -c --arg tag \"$1\" '.inbounds[] | select(.tag==$tag)'", "second", check=True)
        self.assertEqual(json.loads(surviving.stdout), second)
        self.assertEqual((self.state_dir / "clients" / "second.json").read_bytes(), second_metadata)

    def test_preserve_helper_handles_keys_arrays_line_endings_and_unicode_spelling(self) -> None:
        old = self.root / "old-crlf.jsonc"
        candidate = self.root / "candidate.json"
        old_raw = (
            '{\r\n'
            '  "name": "caf\\u00e9", // spelling must remain\r\n'
            '  "remove": true,\r\n'
            '  "numbers": [1, 2, 3],\r\n'
            '  "object": {"keep": "x", "drop": 1}, // close\r\n'
            '  "tail": "value"\r\n'
            '}'
        )
        old.write_bytes(old_raw.encode("utf-8"))
        fixture.dump(candidate, {
            "name": "café",
            "numbers": [9, 2],
            "object": {"keep": "x", "added": 2},
            "tail": "value",
        })
        result = self.shell_bytes('preserve_config_source "$1" "$2"', str(old), str(candidate))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output_bytes = result.stdout
        output = output_bytes.decode("utf-8")
        self.assertIn('"name": "caf\\u00e9"', output)
        self.assertIn("// spelling must remain", output)
        self.assertIn(b"\r\n", output_bytes)
        self.assertNotIn(b"\n", output_bytes.replace(b"\r\n", b""))
        self.assertFalse(output_bytes.endswith(b"\n"))
        self.assertNotIn('"remove"', output)
        self.assertNotIn('"drop"', output)
        preserved = self.root / "preserved.jsonc"
        preserved.write_bytes(output_bytes)
        parsed = self.shell('json_query "$1" .', str(preserved))
        self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)
        parsed_value = json.loads(parsed.stdout)
        self.assertEqual(parsed_value["numbers"], [9, 2])
        self.assertEqual(parsed_value["object"]["added"], 2)

    def test_add_and_delete_keep_other_nodes_source_comments(self) -> None:
        raw, first, second, _ = self._write_preserve_fixture()
        added = self.inbound("added", 18445)
        added_file = self.root / "added.json"
        fixture.dump(added_file, added)
        result = self.shell('add_inbound_file "$1"', str(added_file))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.config.read_text(encoding="utf-8")
        self.assertIn("// untouched second node", output)
        self.assertIn("// 顶层中文注释", output)
        self.assertEqual(self.shell("config_jq -r --arg tag \"$1\" '.inbounds[] | select(.tag==$tag) | .tag'", "added", check=True).stdout.strip(), "added")

        result = self.shell('confirm() { return 0; }; delete_inbound "$1"', "first")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.config.read_text(encoding="utf-8")
        self.assertIn("// untouched second node", output)
        remaining = self.shell("config_jq -c --arg tag \"$1\" '.inbounds[] | select(.tag==$tag)'", "second", check=True)
        self.assertEqual(json.loads(remaining.stdout), second)
        self.assertNotIn("// first type", output)
        self.assertEqual(second["tag"], "second")
        self.assertNotEqual(raw, output)

    def test_preserve_failure_rollback_restores_original_bytes(self) -> None:
        raw, first, _, _ = self._write_preserve_fixture()
        changed = dict(first)
        changed["listen_port"] = 18445
        changed_file = self.root / "changed.json"
        fixture.dump(changed_file, changed)
        fixture.dump(self.root / "first-old.json", first)
        self.state(active=True, restart_failures=1)
        result = self.shell(
            'replace_inbound_file "$1" "$2" example.com "" "$3"',
            "first", str(changed_file), str(self.root / "first-old.json"),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))

    def test_bundle_backup_restore_carries_source_and_rejects_mismatch_but_accepts_old_bundle(self) -> None:
        raw, _, _, _ = self._write_preserve_fixture()
        backup_result = self.shell("create_backup")
        self.assertEqual(backup_result.returncode, 0, backup_result.stdout + backup_result.stderr)
        backup = Path(backup_result.stdout.strip())
        value = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(value.get("config_source"), raw)

        fixture.dump(self.config, self.original)
        self.state(active=False)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(backup))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))

        old_bundle_value = dict(value)
        old_bundle_value.pop("config_source", None)
        old_bundle = self.root / "old-bundle.json"
        fixture.dump(old_bundle, old_bundle_value)
        fixture.dump(self.config, self.original)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(old_bundle))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.shell('config_jq -r ".inbounds[0].tag"').stdout.strip(), "first")

        forged = dict(value)
        forged["config_source"] = "{}\n"
        forged_file = self.root / "forged-bundle.json"
        fixture.dump(forged_file, forged)
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(forged_file))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_explicit_import_replaces_source_with_passed_comments(self) -> None:
        self._write_preserve_fixture()
        imported = self.root / "imported-commented.jsonc"
        imported_raw = '{\n  // imported source\n  "log": {"level": "warn", "timestamp": true},\n  "inbounds": [],\n  "outbounds": [{"type":"direct","tag":"direct"}]\n}\n'
        imported.write_text(imported_raw, encoding="utf-8")
        self.state(active=False)
        result = self.shell('import_config "$1"', str(imported))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), imported_raw.encode("utf-8"))
        self.assertNotIn("顶层中文注释", self.config.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
