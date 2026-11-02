#!/usr/bin/env python3
"""Focused tests for sing-box Snell v5/v6 inbounds and clients."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class SnellTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def snell(self, protocol: str = "snell", psk: str = "snell-secret", extra: str = "none", tag: str = "snell", port: int = 25601) -> dict:
        result = self.shell(
            'generate_inbound "$@"', protocol, tag, str(port), psk, extra, "", "", "", "", "", "", "",
            check=True,
        )
        return json.loads(result.stdout)

    def _write_config(self, node: dict, *, comments: bool = False) -> None:
        value = {**self.original, "inbounds": [node]}
        if comments:
            self.config.write_text(
                '{\n  // keep Snell comment\n  "log":{"level":"info"},\n'
                '  "inbounds":[' + json.dumps(node, ensure_ascii=False) + '],\n'
                '  "outbounds":[{"type":"direct","tag":"direct"}]\n}\n',
                encoding="utf-8",
            )
        else:
            fixture.dump(self.config, value)

    def _metadata(self, node: dict, **extra: object) -> Path:
        path = self.state_dir / "clients" / (node["tag"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"server": "example.com", "public_key": "", "inbound": node}
        value.update(extra)
        fixture.dump(path, value)
        return path

    def test_generate_v5_v6_modes_password_boundaries_and_client_versions(self) -> None:
        cases = (
            ("snell", "snell-secret", "none", 5, 4),
            ("snell", "snell-secret", "http", 5, 4),
            ("snell-v6", "abcdefghijkl", "default", 6, 6),
            ("snell-v6", "abcdefghijkl", "unshaped", 6, 6),
            ("snell-v6", "abcdefghijkl", "unsafe-raw", 6, 6),
        )
        for index, (protocol, psk, mode, inbound_version, outbound_version) in enumerate(cases):
            with self.subTest(protocol=protocol, mode=mode):
                node = self.snell(protocol, psk, mode, f"snell-{index}", 25601 + index)
                self.assertEqual(node["type"], "snell")
                self.assertEqual(node["version"], inbound_version)
                self.assertEqual(node["psk"], psk)
                if inbound_version == 5:
                    self.assertEqual(node.get("obfs_mode", "none"), mode)
                else:
                    self.assertEqual(node.get("mode", "default"), mode)
                self.assertNotIn("users", node)
                source = self.root / (protocol + mode + ".json")
                fixture.dump(source, node)
                client = json.loads(self.shell('generate_client "$1" "example.com"', str(source), check=True).stdout)
                self.assertEqual(client["type"], "snell")
                self.assertEqual(client["version"], outbound_version)
                self.assertEqual(client["psk"], psk)
                if fixture.REAL_CORE:
                    full = {**self.original, "inbounds": [node]}
                    full_path = self.root / (protocol + mode + "-full.json")
                    fixture.dump(full_path, full)
                    checked = subprocess.run([fixture.REAL_CORE, "check", "-c", str(full_path)], capture_output=True, text=True, timeout=20)
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

        for protocol, psk in (("snell", ""), ("snell-v6", "short"), ("snell-v6", "abcdefghijk")):
            result = self.shell(
                'generate_inbound "$@"', protocol, "bad", "25620", psk, "default", "", "", "", "", "", "", "",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")

    def test_snell_client_links_are_surge_lines_and_complex_psk_is_rejected(self) -> None:
        for protocol, mode, version in (("snell", "http", 4), ("snell-v6", "unshaped", 6)):
            node = self.snell(protocol, "snell-secret", mode, protocol + "-link", 25630 + version)
            source = self.root / (protocol + "-link.json")
            fixture.dump(source, node)
            outbound = json.loads(self.shell('generate_client "$1" "example.com"', str(source), check=True).stdout)
            outbound_path = self.root / (protocol + "-outbound.json")
            fixture.dump(outbound_path, outbound)
            link = self.shell('client_link "$1" "snell-name"', str(outbound_path), check=True).stdout
            self.assertIn("snell", link)
            self.assertIn("example.com", link)
            self.assertIn(f"version={version}", link)
            self.assertIn("psk=snell-secret", link)
            self.assertIn("obfs=http" if version == 4 else "mode=unshaped", link)

        complex_node = self.snell("snell", "comma,password", "none", "complex-link", 25640)
        source = self.root / "complex-snell.json"
        fixture.dump(source, complex_node)
        outbound = self.shell('generate_client "$1" "example.com"', str(source), check=True)
        outbound_path = self.root / "complex-snell-outbound.json"
        outbound_path.write_text(outbound.stdout, encoding="utf-8")
        result = self.shell('client_link "$1" "complex"', str(outbound_path))
        self.assertNotEqual(result.returncode, 0)

    def test_json_export_supports_snell_but_uri_batches_do_not_mix_surge_lines(self) -> None:
        node = self.snell("snell", "snell-secret", "none", "export-snell")
        self._write_config(node)
        self._metadata(node)
        target = self.root / "outbounds-export.json"
        self.shell('export_clients json "$1"', str(target), check=True)
        value = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(value["outbounds"][0]["type"], "snell")
        self.assertEqual(value["outbounds"][0]["version"], 4)
        for mode in ("links", "base64"):
            target = self.root / (mode + "-export.txt")
            result = self.shell('export_clients "$1" "$2"', mode, str(target))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertIn("Snell", result.stdout + result.stderr)

    def test_snell_psk_utf8_bounds_and_mode_validation(self) -> None:
        for psk in ("汉"*4, "a"*255):
            self.assertEqual(self.snell("snell-v6", psk, "default")["psk"], psk)
        for psk in ("汉"*3, "a"*256):
            result = self.shell('generate_inbound snell-v6 invalid 25680 "$1" default', psk)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        for protocol, mode in (("snell", "tls"), ("snell-v6", "http")):
            result = self.shell('generate_inbound "$1" invalid 25680 abcdefghijkl "$2"', protocol, mode)
            self.assertNotEqual(result.returncode, 0)
        node = self.snell()
        path = self.root / "null-psk.json"
        node["psk"] = "hidden\x00invalid"
        fixture.dump(path, node)
        self.assertNotEqual(self.shell('validate_snell_template "$1"', str(path)).returncode, 0)

    def test_snell_users_and_unknown_fields_are_rejected(self) -> None:
        node = self.snell()
        node["users"] = [{"psk": "unexpected"}]
        node["tag"] = "snell-users"
        path = self.root / "snell-users.json"
        fixture.dump(path, node)
        self.assertNotEqual(self.shell('validate_snell_template "$1"', str(path)).returncode, 0)
        result = self.shell('generate_client "$1" "example.com"', str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        unknown = self.snell(tag="snell-unknown")
        unknown["unknown_field"] = True
        path = self.root / "snell-unknown.json"
        fixture.dump(path, unknown)
        self.assertNotEqual(self.shell('validate_snell_template "$1"', str(path)).returncode, 0)

    def test_snell_add_menu_cancel_and_edit_preserve_jsonc_metadata_and_rollback(self) -> None:
        menu = self.shell('require_core(){ return 0; }; prompt(){ printf -v "$1" 0; }; add_wizard_locked', check=True)
        self.assertIn("Snell", menu.stdout)
        self.assertRegex(menu.stdout, r"19\.\s*Snell")
        self.assertRegex(menu.stdout, r"20\.\s*Snell")
        node = self.snell("snell-v6", "abcdefghijkl", "unshaped", "edit-snell", 25651)
        metadata = self._metadata(node, outbound_route={"format": "sing-box-manager-outbound-route-v1", "rule": {"inbound": ["edit-snell"], "action": "route", "outbound": "proxy"}})
        self._write_config(node, comments=True)
        before_config = self.config.read_bytes()
        body = r'''
            prompt() {
                case "$1" in
                    tag|port|extra|snell_mode) printf -v "$1" '%s' "${3-}" ;;
                    *) printf -v "$1" '%s' "${3-}" ;;
                esac
            }
            prompt_secret() { printf -v "$1" '%s' "${TEST_EDIT_PSK:-${3-}}"; }
            confirm() { return 0; }
            edit_wizard_locked edit-snell
        '''
        self.shell(body, check=True)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.env["TEST_EDIT_PSK"] = "abcdefghijklmnop"
        self.shell(body, check=True)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(saved["psk"], "abcdefghijklmnop")
        self.assertEqual(saved["mode"], "unshaped")
        self.assertIn("// keep Snell comment", self.config.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["outbound_route"], {
            "format": "sing-box-manager-outbound-route-v1", "rule": {"inbound": ["edit-snell"], "action": "route", "outbound": "proxy"},
        })
        self.assertNotEqual(before_config, self.config.read_bytes())

        self.state(active=True, restart_failures=1)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        self.env["TEST_EDIT_PSK"] = "qrstuvwxyzabcdef"
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])

    def test_snell_is_tcp_for_port_conflict_and_cli_menu_route(self) -> None:
        snell = self.snell(tag="snell-tcp", port=25661)
        cert, key = fixture.ManagerTest.tls_material(self)
        hy2 = fixture.ManagerTest.generate_inbound(self, "hysteria2", "hy-password", tls_server_name="example.com", certificate=cert, key=key) | {"tag": "hy2-udp", "listen_port": 25661}
        snell_path = self.root / "snell-tcp.json"
        fixture.dump(snell_path, snell)
        fixture.dump(self.config, {**self.original, "inbounds": [hy2]})
        self.assertEqual(self.shell('check_inbound_conflicts "$1" "" "$2"', str(snell_path), str(self.config), check=True).returncode, 0)

        body = r'''
            SB_ROOT_PREFIX=""
            require_root(){ return 0; }; require_systemd(){ return 0; }; safe_paths(){ return 0; }; is_managed(){ return 0; }
            users_menu(){ printf 'USERS\n'; }
            main users snell-tcp
        '''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0)
        self.assertIn("USERS", result.stdout)


if __name__ == "__main__":
    unittest.main()
