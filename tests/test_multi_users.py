#!/usr/bin/env python3
"""Focused tests for multi-user inbounds and per-user client export."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import unquote, urlsplit

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class MultiUserTest(unittest.TestCase):
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

    def tls_material(self, common_name: str = "example.com") -> tuple[Path, Path]:
        return fixture.ManagerTest.tls_material(self, common_name)

    def vless(self, tag: str = "multi-vless", port: int = 25401) -> dict:
        node = fixture.ManagerTest.generate_inbound(
            self, "vless-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
        ) | {"tag": tag, "listen_port": port}
        node["users"] = [
            {"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737", "name": "alice", "flow": ""},
            {"uuid": "0325ba41-4946-4ad5-b6b3-476eb50c37cb", "name": "bob", "flow": ""},
        ]
        return node

    def vmess(self, tag: str = "multi-vmess", port: int = 25402) -> dict:
        node = fixture.ManagerTest.generate_inbound(
            self, "vmess-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
        ) | {"tag": tag, "listen_port": port}
        node["users"] = [
            {"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737", "alterId": 0, "name": "alice"},
            {"uuid": "0325ba41-4946-4ad5-b6b3-476eb50c37cb", "alterId": 0, "name": "bob"},
        ]
        return node

    def hysteria2(self, tag: str = "multi-hy2", port: int = 25403) -> dict:
        certificate, private_key = self.tls_material()
        node = fixture.ManagerTest.generate_inbound(
            self, "hysteria2", "hy-password", tls_server_name="example.com",
            certificate=certificate, key=private_key,
        ) | {"tag": tag, "listen_port": port}
        node["users"] = [
            {"password": "hy-alice", "name": "alice"},
            {"password": "hy-bob", "name": "bob"},
        ]
        return node

    def tuic(self, tag: str = "multi-tuic", port: int = 25404) -> dict:
        certificate, private_key = self.tls_material()
        node = fixture.ManagerTest.generate_inbound(
            self, "tuic", "b7efdc96-87aa-4910-81b4-374b2eecc737",
            tls_server_name="example.com", certificate=certificate, key=private_key,
            tuic_password="tuic-password",
        ) | {"tag": tag, "listen_port": port}
        node["users"] = [
            {"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737", "password": "tuic-alice", "name": "alice"},
            {"uuid": "0325ba41-4946-4ad5-b6b3-476eb50c37cb", "password": "tuic-bob", "name": "bob"},
        ]
        return node

    def anytls(self, tag: str = "multi-anytls", port: int = 25405) -> dict:
        certificate, private_key = self.tls_material()
        node = fixture.ManagerTest.generate_inbound(
            self, "anytls", "anytls-password", tls_server_name="example.com",
            certificate=certificate, key=private_key,
        ) | {"tag": tag, "listen_port": port}
        node["users"] = [
            {"password": "any-alice", "name": "alice"},
            {"password": "any-bob", "name": "bob"},
        ]
        return node

    def _write_config(self, nodes: list[dict], *, comments: bool = False) -> None:
        if comments:
            self.config.write_text(
                '{\n  // keep multi-user comment\n  "log":{"level":"info"},\n'
                '  "inbounds":[' + ",".join(json.dumps(node, ensure_ascii=False) for node in nodes) + '],\n'
                '  "outbounds":[{"type":"direct","tag":"direct"}]\n}\n',
                encoding="utf-8",
            )
        else:
            fixture.dump(self.config, {**self.original, "inbounds": nodes})

    def _metadata(self, node: dict, *, public_key: str = "", record: dict | None = None) -> Path:
        path = self.state_dir / "clients" / (node["tag"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"server": "example.com", "public_key": public_key, "inbound": node}
        if record is not None:
            value["quic_bbr_restore"] = record
        fixture.dump(path, value)
        return path

    def test_validate_multi_users_all_supported_protocols_and_reject_duplicates(self) -> None:
        nodes = [self.vless(), self.vmess(), self.hysteria2(), self.tuic(), self.anytls()]
        for node in nodes:
            with self.subTest(protocol=node["type"]):
                path = self.root / (node["tag"] + ".json")
                fixture.dump(path, node)
                result = self.shell('validate_inbound_users "$1"', str(path), check=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        duplicate_uuid = copy.deepcopy(nodes[0])
        duplicate_uuid["users"][1]["uuid"] = duplicate_uuid["users"][0]["uuid"].upper()
        duplicate_name = copy.deepcopy(nodes[0])
        duplicate_name["users"][1]["name"] = duplicate_name["users"][0]["name"]
        inconsistent_flow = copy.deepcopy(nodes[0])
        inconsistent_flow["users"][0]["flow"] = "flow-a"
        inconsistent_flow["users"][1]["flow"] = "flow-b"
        duplicate_password = copy.deepcopy(nodes[2])
        duplicate_password["users"][1]["password"] = duplicate_password["users"][0]["password"]
        for name, invalid in (("uuid", duplicate_uuid), ("name", duplicate_name), ("flow", inconsistent_flow), ("password", duplicate_password)):
            path = self.root / ("invalid-" + name + ".json")
            fixture.dump(path, invalid)
            self.assertNotEqual(self.shell('validate_inbound_users "$1"', str(path)).returncode, 0)
        ss = fixture.ManagerTest.generate_inbound(self, "shadowsocks", "8JCsPssfgS8tiRwiMlhARg==")
        ss["users"] = [{"password": "one"}, {"password": "two"}]
        path = self.root / "invalid-ss.json"
        fixture.dump(path, ss)
        self.assertNotEqual(self.shell('validate_inbound_users "$1"', str(path)).returncode, 0)
        if fixture.REAL_CORE:
            reality = fixture.ManagerTest.generate_inbound(
                self, "vless-reality", "b7efdc96-87aa-4910-81b4-374b2eecc737",
                "example.com", "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
            ) | {"tag": "multi-reality", "listen_port": 25406}
            reality["users"].append({"uuid": "0325ba41-4946-4ad5-b6b3-476eb50c37cb", "flow": "xtls-rprx-vision", "name": "bob"})
            full = self.root / "multi-core-check.json"
            fixture.dump(full, {**self.original, "inbounds": [reality, self.vmess()]})
            checked = subprocess.run([fixture.REAL_CORE, "check", "-c", str(full)], capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_generate_client_requires_user_index_and_exports_selected_user(self) -> None:
        node = self.vless()
        path = self.root / "vless-multi.json"
        fixture.dump(path, node)
        missing = self.shell('generate_client "$1" "example.com"', str(path))
        self.assertNotEqual(missing.returncode, 0)
        for index, uuid in ((1, node["users"][0]["uuid"]), (2, node["users"][1]["uuid"])):
            result = self.shell('generate_client "$1" "example.com" "" "$2"', str(path), str(index), check=True)
            self.assertEqual(json.loads(result.stdout)["uuid"], uuid)
        for node in (self.hysteria2(), self.tuic(), self.anytls()):
            path = self.root / (node["tag"] + ".json")
            fixture.dump(path, node)
            result = self.shell('generate_client "$1" "example.com" "" 2', str(path), check=True)
            outbound = json.loads(result.stdout)
            self.assertEqual(outbound["server"], "example.com")
            self.assertEqual(outbound["password"], node["users"][1]["password"])

    def test_show_and_batch_export_include_all_users_without_printing_auth_in_list(self) -> None:
        node = self.vless("export-multi", 25411)
        self._write_config([node])
        self._metadata(node)
        selected = self.shell('show_client "$1" "example.com" json-only 2', node["tag"], check=True)
        self.assertEqual(json.loads(selected.stdout)["uuid"], node["users"][1]["uuid"])
        all_users = self.shell('show_client "$1" "example.com" json-only all', node["tag"], check=True)
        self.assertIn(node["users"][0]["uuid"], all_users.stdout)
        self.assertIn(node["users"][1]["uuid"], all_users.stdout)
        target = self.root / "multi-links.txt"
        self.shell('export_clients links "$1"', str(target), check=True)
        links = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(links), 2)
        self.assertTrue(all(urlsplit(link).scheme == "vless" for link in links))
        self.assertTrue(any("user1" in unquote(link) or "alice" in unquote(link) for link in links))
        self.assertTrue(any("user2" in unquote(link) or "bob" in unquote(link) for link in links))
        json_target = self.root / "multi-outbounds.json"
        self.shell('export_clients json "$1"', str(json_target), check=True)
        outbounds = json.loads(json_target.read_text(encoding="utf-8"))["outbounds"]
        self.assertEqual([item["tag"] for item in outbounds], ["export-multi:user1:alice", "export-multi:user2:bob"])
        # Node names cannot contain ':', so user labels cannot collide with a single-user node name.
        single = self.vless("export-multi-user1-alice", 25412)
        single["users"] = single["users"][:1]
        self._write_config([node, single])
        self._metadata(single)
        unique_target = self.root / "multi-unique-outbounds.json"
        self.shell('export_clients json "$1"', str(unique_target), check=True)
        combined = json.loads(unique_target.read_text(encoding="utf-8"))["outbounds"]
        self.assertEqual(len(combined), 3)
        self.assertEqual(len({item["tag"] for item in combined}), 3)

    def test_edit_selected_user_preserves_other_users_jsonc_metadata_and_public_key(self) -> None:
        node = self.vless("edit-multi", 25421)
        metadata = self._metadata(node, public_key="public-key-placeholder")
        self._write_config([node], comments=True)
        before_other = copy.deepcopy(node["users"][0])
        body = r'''
            prompt() {
                case "$1" in
                    inbound_user_selection) printf -v "$1" 2 ;;
                    tag|port) printf -v "$1" '%s' "${3-}" ;;
                    *) printf -v "$1" '%s' "${3-}" ;;
                esac
            }
            prompt_secret() { printf -v "$1" 5c8f4b2c-1f32-4a4a-9d6d-0f5a2d9d0c3e; }
            confirm() { return 0; }
            edit_wizard_locked edit-multi
        '''
        result = self.shell(body, check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(saved["users"][0], before_other)
        self.assertEqual(saved["users"][1]["uuid"], "5c8f4b2c-1f32-4a4a-9d6d-0f5a2d9d0c3e")
        self.assertIn("// keep multi-user comment", self.config.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["public_key"], "public-key-placeholder")

    def test_edit_multi_hy2_preserves_shared_tls_obfs_and_quic_record(self) -> None:
        node = self.hysteria2("edit-hy2-multi", 25425)
        node["users"] = [
            {"password": "hy-alice", "name": "alice"},
            {"password": "hy-bob", "name": "bob"},
        ]
        node["ignore_client_bandwidth"] = True
        record = {"format": "sing-box-manager-quic-bbr-v1", "type": "hysteria2", "settings": {}}
        metadata = self._metadata(node, record=record)
        self._write_config([node], comments=True)
        original_shared = {key: copy.deepcopy(node[key]) for key in ("tls", "ignore_client_bandwidth")}
        certificate, private_key = self.tls_material()
        body = r'''
            prompt() {
                case "$1" in
                    inbound_user_selection) printf -v "$1" 2 ;;
                    quic_bbr_choice) printf -v "$1" 1 ;;
                    tag|port) printf -v "$1" '%s' "${3-}" ;;
                    tls_name|server) printf -v "$1" example.com ;;
                    *) printf -v "$1" '%s' "${3-}" ;;
                esac
            }
            prompt_secret() { printf -v "$1" edited-hy-password; }
            prompt_hysteria2_obfs() { printf -v "$1" '%s' "${2-}"; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
            confirm() { return 0; }
            edit_wizard_locked edit-hy2-multi
        '''
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "edit-hy2-multi", str(fixture.SCRIPT)],
            env=dict(self.env, TEST_CERTIFICATE=str(certificate), TEST_PRIVATE_KEY=str(private_key)),
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(saved["users"][0], node["users"][0])
        self.assertEqual(saved["users"][1]["password"], "edited-hy-password")
        self.assertEqual(saved["tls"], original_shared["tls"])
        self.assertTrue(saved["ignore_client_bandwidth"])
        self.assertIn("// keep multi-user comment", self.config.read_text(encoding="utf-8"))
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata_value["quic_bbr_restore"], record)

    def test_add_and_delete_user_preserve_other_users_and_reject_last_delete(self) -> None:
        node = self.vless("user-mutate", 25431)
        metadata = self._metadata(node)
        self._write_config([node])
        body = r'''
            prompt() {
                case "$1" in
                    user_name) printf -v "$1" charlie ;;
                    *) printf -v "$1" '%s' "${3-}" ;;
                esac
            }
            prompt_secret() { printf -v "$1" 5c8f4b2c-1f32-4a4a-9d6d-0f5a2d9d0c3e; }
            confirm() { return 0; }
            add_inbound_user_locked user-mutate
        '''
        self.shell(body, check=True)
        added = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(len(added["users"]), 3)
        self.assertEqual(added["users"][2]["name"], "charlie")
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], added)

        delete_body = r'''
            prompt() { printf -v "$1" 2; }
            confirm() { return 0; }
            delete_inbound_user_locked user-mutate
        '''
        self.shell(delete_body, check=True)
        after_delete = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(len(after_delete["users"]), 2)
        self.assertEqual(after_delete["users"][0]["name"], "alice")
        self.assertEqual(after_delete["users"][1]["name"], "charlie")
        single = copy.deepcopy(after_delete)
        single["users"] = [single["users"][0]]
        fixture.dump(self.config, {**self.original, "inbounds": [single]})
        result = self.shell('confirm() { return 0; }; delete_inbound_user_locked "$1"', "user-mutate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]["users"]), 1)

    def test_multi_user_cli_routes(self) -> None:
        body = r'''
            SB_ROOT_PREFIX=""
            require_root() { return 0; }
            require_systemd() { return 0; }
            safe_paths() { return 0; }
            is_managed() { return 0; }
            users_menu() { printf 'USERS_MENU\n'; }
            add_inbound_user() { printf 'USER_ADD:%s\n' "$*"; }
            delete_inbound_user() { printf 'USER_DELETE:%s\n' "$*"; }
            main users multi
            main user-add multi
            main user-delete multi
        '''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("USERS_MENU", result.stdout)
        self.assertIn("USER_ADD:multi", result.stdout)
        self.assertIn("USER_DELETE:multi", result.stdout)

    def test_add_user_restart_failure_rolls_back_and_cancel_is_noop(self) -> None:
        node = self.vless("add-user-rollback", 25441)
        self._write_config([node])
        metadata = self._metadata(node)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        self.state(active=True, restart_failures=1)
        body = r'''
            prompt() { if [[ $1 == user_name ]]; then printf -v "$1" charlie; else printf -v "$1" '%s' "${3-}"; fi; }
            prompt_secret() { printf -v "$1" 5c8f4b2c-1f32-4a4a-9d6d-0f5a2d9d0c3e; }
            confirm() { return 0; }
            add_inbound_user_locked add-user-rollback
        '''
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])
        canceled = self.shell(body.replace("return 0", "return 1"))
        self.assertEqual(canceled.returncode, 0, canceled.stdout + canceled.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_add_user_confirmation_config_cas_rejects_external_port_change(self) -> None:
        node = self.vless("add-user-cas", 25451)
        fixture.dump(self.config, {**self.original, "inbounds": [node]})
        metadata = self._metadata(node)
        before_metadata = metadata.read_bytes()
        body = r'''
            prompt() { if [[ $1 == user_name ]]; then printf -v "$1" charlie; else printf -v "$1" '%s' "${3-}"; fi; }
            prompt_secret() { printf -v "$1" 5c8f4b2c-1f32-4a4a-9d6d-0f5a2d9d0c3e; }
            confirm() {
                python3 -c 'import json,sys; p=sys.argv[1]; v=json.load(open(p)); v["inbounds"][0]["listen_port"] += 1; json.dump(v,open(p,"w"))' "$CONFIG_FILE"
                return 0
            }
            add_inbound_user_locked add-user-cas
        '''
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        changed = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(changed["inbounds"][0]["listen_port"], node["listen_port"] + 1)
        self.assertEqual(changed["inbounds"][0]["users"], node["users"])
        self.assertEqual(metadata.read_bytes(), before_metadata)


if __name__ == "__main__":
    unittest.main()
