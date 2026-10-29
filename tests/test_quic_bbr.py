#!/usr/bin/env python3
"""Focused tests for per-inbound QUIC BBR configuration."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class QuicBbrTest(unittest.TestCase):
    """Reuse one isolated fixture without inheriting the full manager suite."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def calls(self, log: Path) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, log)

    def _metadata(self, node: dict) -> Path:
        return self.state_dir / "clients" / (node["tag"] + ".json")

    def tls_material(self, common_name: str = "example.com") -> tuple[Path, Path]:
        return fixture.ManagerTest.tls_material(self, common_name)

    def hysteria2(self, tag: str = "hy2-quic", port: int = 18443, *, obfs: str = "") -> dict:
        certificate, private_key = self.tls_material()
        return fixture.ManagerTest.generate_inbound(
            self, "hysteria2", "hy-password", tls_server_name="example.com",
            certificate=certificate, key=private_key, obfs_password=obfs,
        ) | {"tag": tag, "listen_port": port}

    def tuic(self, tag: str = "tuic-quic", port: int = 18444) -> dict:
        certificate, private_key = self.tls_material()
        return fixture.ManagerTest.generate_inbound(
            self, "tuic", "b7efdc96-87aa-4910-81b4-374b2eecc737",
            tls_server_name="example.com", certificate=certificate, key=private_key,
            tuic_password="tuic-password",
        ) | {"tag": tag, "listen_port": port}

    def _write_config(self, nodes: list[dict], *, jsonc: bool = False) -> None:
        if not jsonc:
            fixture.dump(self.config, {**self.original, "inbounds": nodes})
            return
        self.config.write_text(
            '{\n'
            '  // preserve QUIC BBR comments\n'
            '  "log": {"level": "info"},\n'
            '  "inbounds": [' + ",".join(json.dumps(node, ensure_ascii=False) for node in nodes) + '],\n'
            '  "outbounds": [{"type": "direct", "tag": "direct"}]\n'
            '}\n',
            encoding="utf-8",
        )

    def _write_metadata(self, node: dict, server: str = "example.com") -> Path:
        path = self.state_dir / "clients" / (node["tag"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture.dump(path, {"server": server, "public_key": "", "inbound": node})
        return path

    def _apply_helper(self, node: dict) -> dict:
        source = self.root / (node["tag"] + "-quic-source.json")
        fixture.dump(source, node)
        result = self.shell('generate_quic_bbr_inbound "$1"', str(source), check=True)
        return json.loads(result.stdout)

    def test_quic_helper_changes_only_supported_fields(self) -> None:
        hy2 = self.hysteria2(obfs="salamander-obfs")
        hy2["up_mbps"] = 12
        hy2["down_mbps"] = 34
        hy2_before = copy.deepcopy(hy2)
        changed_hy2 = self._apply_helper(hy2)
        self.assertNotIn("up_mbps", changed_hy2)
        self.assertNotIn("down_mbps", changed_hy2)
        self.assertTrue(changed_hy2["ignore_client_bandwidth"])
        self.assertEqual(changed_hy2["users"], hy2_before["users"])
        self.assertEqual(changed_hy2["tls"], hy2_before["tls"])
        self.assertEqual(changed_hy2["obfs"], hy2_before["obfs"])
        self.assertEqual(changed_hy2["listen_port"], hy2_before["listen_port"])

        tuic = self.tuic()
        changed_tuic = self._apply_helper(tuic)
        self.assertEqual(changed_tuic["congestion_control"], "bbr")
        self.assertEqual(changed_tuic["users"], tuic["users"])
        self.assertEqual(changed_tuic["tls"], tuic["tls"])

        unsupported = fixture.ManagerTest.generate_inbound(
            self, "vless-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
        ) | {"tag": "vless-quic", "listen_port": 18445}
        source = self.root / "unsupported-quic.json"
        fixture.dump(source, unsupported)
        result = self.shell('generate_quic_bbr_inbound "$1"', str(source))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_tuic_bbr_outbound_and_share_link_keep_congestion_control(self) -> None:
        node = self._apply_helper(self.tuic(tag="tuic-bbr-export"))
        source = self.root / "tuic-bbr-export.json"
        fixture.dump(source, node)
        exported = self.shell('generate_client "$1" "example.com"', str(source), check=True)
        outbound = json.loads(exported.stdout)
        self.assertEqual(outbound["congestion_control"], "bbr")
        outbound_path = self.root / "tuic-bbr-outbound.json"
        outbound_path.write_text(exported.stdout, encoding="utf-8")
        link = self.shell('client_link "$1" "tuic-bbr"', str(outbound_path), check=True).stdout.strip()
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(query["congestion_control"], ["bbr"])

    def test_configure_hy2_rewrites_jsonc_metadata_and_preserves_identity(self) -> None:
        hy2 = self.hysteria2(tag="hy2-configure", port=24001, obfs="salamander-obfs")
        hy2["up_mbps"] = 10
        hy2["down_mbps"] = 20
        other = fixture.ManagerTest.generate_inbound(
            self, "vless-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
        ) | {"tag": "other-node", "listen_port": 24002}
        self._write_config([hy2, other], jsonc=True)
        metadata = self._write_metadata(hy2, "example.com")
        other_metadata = self._write_metadata(other, "other.example")
        before_hy2 = copy.deepcopy(hy2)
        before_other_metadata = other_metadata.read_bytes()
        self.state(active=False)
        result = self.shell(
            'confirm() { return 0; }; configure_quic_bbr_locked "$1"', hy2["tag"], check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)
        saved_hy2, saved_other = saved["inbounds"]
        self.assertNotIn("up_mbps", saved_hy2)
        self.assertNotIn("down_mbps", saved_hy2)
        self.assertTrue(saved_hy2["ignore_client_bandwidth"])
        self.assertEqual(saved_hy2["users"], before_hy2["users"])
        self.assertEqual(saved_hy2["tls"], before_hy2["tls"])
        self.assertEqual(saved_hy2["obfs"], before_hy2["obfs"])
        self.assertEqual(saved_other, other)
        self.assertIn("// preserve QUIC BBR comments", self.config.read_text(encoding="utf-8"))
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata_value["inbound"], saved_hy2)
        self.assertEqual(metadata_value["quic_bbr_restore"], {
            "format": "sing-box-manager-quic-bbr-v1",
            "type": "hysteria2",
            "settings": {"up_mbps": 10, "down_mbps": 20},
        })
        self.assertEqual(other_metadata.read_bytes(), before_other_metadata)
        self.assertFalse(self.state()["active"])
        bundles = sorted((self.state_dir / "backups").glob("bundle-*.json"))
        self.assertEqual(len(bundles), 1)
        bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
        old_backup = next(item for item in bundle["config"]["inbounds"] if item["tag"] == hy2["tag"])
        self.assertEqual(old_backup["up_mbps"], 10)
        self.assertEqual(old_backup["down_mbps"], 20)
        self.assertFalse(any(
            any(action in call for action in ("start", "restart", "stop"))
            for call in self.calls(self.system_log)
        ))

    def test_configure_quic_bbr_selection_by_number_and_cancel_noop(self) -> None:
        hy2 = self.hysteria2(tag="hy2-number", port=24101)
        hy2["up_mbps"] = 11
        tuic = self.tuic(tag="tuic-number", port=24102)
        self._write_config([hy2, tuic])
        metadata = self._write_metadata(hy2)
        self._write_metadata(tuic)
        initial_config = self.config.read_bytes()
        initial_metadata = metadata.read_bytes()
        body = r'''
            prompt() { [[ $1 == inbound_selection ]] && printf -v "$1" 1 || printf -v "$1" '%s' "${3-}"; }
            confirm() { return 0; }
            configure_quic_bbr_locked
        '''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertTrue(first["ignore_client_bandwidth"])
        self.assertNotIn("up_mbps", first)
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], first)

        self.config.write_bytes(initial_config)
        metadata.write_bytes(initial_metadata)
        marker = self.root / "confirm-marker"
        self.env["QUIC_CONFIRM_MARKER"] = str(marker)
        result = self.shell(
            'confirm() { printf x > "$QUIC_CONFIRM_MARKER"; return 1; }; configure_quic_bbr_locked "$1"',
            hy2["tag"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(self.config.read_bytes(), initial_config)
        self.assertEqual(metadata.read_bytes(), initial_metadata)

        marker.unlink()
        result = self.shell(
            'confirm() { return 0; }; configure_quic_bbr_locked "$1"', hy2["tag"], check=True,
        )
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self.shell(
            'confirm() { printf x > "$QUIC_CONFIRM_MARKER"; return 99; }; configure_quic_bbr_locked "$1"',
            hy2["tag"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))

    def test_quic_bbr_status_is_read_only_and_hides_credentials(self) -> None:
        hy2 = self.hysteria2(tag="status-hy2", port=24301, obfs="status-obfs-password")
        tuic = self.tuic(tag="status-tuic", port=24302)
        tuic["congestion_control"] = "bbr"
        self._write_config([hy2, tuic])
        before = self.config.read_bytes()
        result = self.shell("show_quic_bbr_status", check=True)
        self.assertIn("status-hy2", result.stdout)
        self.assertIn("hysteria2", result.stdout)
        self.assertIn("status-tuic", result.stdout)
        self.assertIn("tuic", result.stdout)
        self.assertNotIn("hy-password", result.stdout)
        self.assertNotIn("tuic-password", result.stdout)
        self.assertNotIn("status-obfs-password", result.stdout)
        self.assertEqual(self.config.read_bytes(), before)

    def test_configure_quic_bbr_restart_failure_rolls_back_config_metadata_and_service(self) -> None:
        hy2 = self.hysteria2(tag="hy2-rollback", port=24201)
        hy2["up_mbps"] = 7
        self._write_config([hy2], jsonc=True)
        metadata = self._write_metadata(hy2)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', hy2["tag"])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])

    def test_quic_restore_records_preserve_missing_zero_null_false_and_limit_fields(self) -> None:
        cases = (
            ("hy2-missing", "hysteria2", {}),
            ("hy2-zero-null-false", "hysteria2", {"up_mbps": 0, "down_mbps": None, "ignore_client_bandwidth": False}),
            ("hy2-values", "hysteria2", {"up_mbps": 12, "down_mbps": 34, "ignore_client_bandwidth": True}),
            ("tuic-cubic", "tuic", {"congestion_control": "cubic"}),
            ("tuic-newreno", "tuic", {"congestion_control": "new_reno"}),
            ("tuic-missing", "tuic", {"__delete__": "congestion_control"}),
            ("tuic-null", "tuic", {"congestion_control": None}),
        )
        for tag, protocol, fields in cases:
            with self.subTest(tag=tag):
                node = self.hysteria2(tag=tag, port=24401) if protocol == "hysteria2" else self.tuic(tag=tag, port=24402)
                if "__delete__" in fields:
                    node.pop(fields["__delete__"], None)
                else:
                    node.update(fields)
                original = copy.deepcopy(node)
                self._write_config([node])
                metadata = self._write_metadata(node)
                result = self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', tag, check=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
                record = metadata_value["quic_bbr_restore"]
                expected_settings = {
                    key: original[key]
                    for key in (("up_mbps", "down_mbps", "ignore_client_bandwidth") if protocol == "hysteria2" else ("congestion_control",))
                    if key in original
                }
                self.assertEqual(record["settings"], expected_settings)
                current_path = self.root / (tag + "-current.json")
                fixture.dump(current_path, metadata_value["inbound"])
                record_path = self.root / (tag + "-record.json")
                fixture.dump(record_path, record)
                restored = json.loads(self.shell(
                    'generate_quic_bbr_restore "$1" "$2"', str(current_path), str(record_path), check=True,
                ).stdout)
                for key in expected_settings:
                    self.assertIn(key, restored)
                    self.assertEqual(restored[key], expected_settings[key])
                for key in (("up_mbps", "down_mbps", "ignore_client_bandwidth") if protocol == "hysteria2" else ("congestion_control",)):
                    if key not in expected_settings:
                        self.assertNotIn(key, restored)

    def test_disable_quic_bbr_restores_only_recorded_fields_and_deletes_record(self) -> None:
        node = self.hysteria2(tag="hy2-disable", port=24501, obfs="disable-obfs")
        node["up_mbps"] = 8
        node["down_mbps"] = 9
        self._write_config([node])
        metadata = self._write_metadata(node)
        self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', node["tag"], check=True)
        current = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        current["users"][0]["password"] = "edited-hy-password"
        current["listen_port"] = 24502
        candidate = self.root / "hy2-disable-edited.json"
        fixture.dump(candidate, current)
        expected = self.root / "hy2-disable-before-edit.json"
        fixture.dump(expected, current | {"listen_port": 24501, "users": node["users"]})
        self.shell('replace_inbound_file "$1" "$2" "" "" "$3"', node["tag"], str(candidate), str(expected), check=True)
        result = self.shell('confirm() { return 0; }; disable_quic_bbr_locked "$1"', node["tag"], check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(restored["users"][0]["password"], "edited-hy-password")
        self.assertEqual(restored["listen_port"], 24502)
        self.assertEqual(restored["obfs"], node["obfs"])
        self.assertEqual(restored["tls"], node["tls"])
        self.assertEqual(restored["up_mbps"], 8)
        self.assertEqual(restored["down_mbps"], 9)
        self.assertNotIn("ignore_client_bandwidth", restored)
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertNotIn("quic_bbr_restore", metadata_value)
        self.assertEqual(metadata_value["inbound"], restored)

    def test_restore_helper_rejects_missing_corrupt_external_and_mismatched_records(self) -> None:
        node = self.hysteria2(tag="hy2-record-invalid", port=24601)
        node["up_mbps"] = 5
        current = self._apply_helper(node)
        current_path = self.root / "invalid-current.json"
        fixture.dump(current_path, current)
        valid = {
            "format": "sing-box-manager-quic-bbr-v1",
            "type": "hysteria2",
            "settings": {"up_mbps": 5},
        }
        records = {
            "missing-format": {},
            "wrong-format": {**valid, "format": "other"},
            "wrong-type": {**valid, "type": "tuic"},
            "unknown-setting": {**valid, "settings": {"up_mbps": 5, "unexpected": 1}},
            "external-change": valid,
        }
        changed = dict(current)
        changed["up_mbps"] = 99
        changed_path = self.root / "invalid-external.json"
        fixture.dump(changed_path, changed)
        for name, record in records.items():
            with self.subTest(case=name):
                record_path = self.root / (name + ".json")
                fixture.dump(record_path, record)
                source = changed_path if name == "external-change" else current_path
                result = self.shell('generate_quic_bbr_restore "$1" "$2"', str(source), str(record_path))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "")
        missing = self.root / "missing-record.json"
        result = self.shell('generate_quic_bbr_restore "$1" "$2"', str(current_path), str(missing))
        self.assertNotEqual(result.returncode, 0)
        corrupt = self.root / "corrupt-record.json"
        corrupt.write_bytes(b"{")
        result = self.shell('generate_quic_bbr_restore "$1" "$2"', str(current_path), str(corrupt))
        self.assertNotEqual(result.returncode, 0)

    def test_normal_edit_rename_preserves_record_until_quic_disable(self) -> None:
        node = self.hysteria2(tag="hy2-rename", port=24651)
        node["up_mbps"] = 6
        self._write_config([node])
        old_metadata = self._write_metadata(node)
        self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', node["tag"], check=True)
        current = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        candidate = dict(current)
        candidate["tag"] = "hy2-renamed"
        candidate["listen_port"] = 24652
        candidate_path = self.root / "hy2-renamed.json"
        expected_path = self.root / "hy2-rename-current.json"
        fixture.dump(candidate_path, candidate)
        fixture.dump(expected_path, current)
        self.shell(
            'replace_inbound_file "$1" "$2" "example.com" "" "$3"',
            node["tag"], str(candidate_path), str(expected_path), check=True,
        )
        new_metadata = self.state_dir / "clients/hy2-renamed.json"
        self.assertFalse(old_metadata.exists())
        record = json.loads(new_metadata.read_text(encoding="utf-8"))["quic_bbr_restore"]
        self.assertEqual(record["settings"], {"up_mbps": 6})
        self.shell('confirm() { return 0; }; disable_quic_bbr_locked "$1"', "hy2-renamed", check=True)
        restored = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        self.assertEqual(restored["tag"], "hy2-renamed")
        self.assertEqual(restored["listen_port"], 24652)
        self.assertEqual(restored["up_mbps"], 6)
        self.assertNotIn("ignore_client_bandwidth", restored)
        self.assertNotIn("quic_bbr_restore", json.loads(new_metadata.read_text(encoding="utf-8")))

    def test_replace_metadata_expected_absence_cas_rejects_without_changes(self) -> None:
        node = self.hysteria2(tag="hy2-cas", port=24661)
        node["up_mbps"] = 4
        self._write_config([node])
        metadata = self._write_metadata(node)
        self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', node["tag"], check=True)
        candidate = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
        candidate["listen_port"] = 24662
        candidate_path = self.root / "hy2-cas-candidate.json"
        expected_path = self.root / "hy2-cas-expected.json"
        fixture.dump(candidate_path, candidate)
        fixture.dump(expected_path, json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0])
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self.shell(
            'replace_inbound_file "$1" "$2" "" "" "$3" "" -',
            node["tag"], str(candidate_path), str(expected_path),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_quic_confirmation_metadata_change_is_rejected_without_overwrite(self) -> None:
        node = self.hysteria2(tag="hy2-confirm-cas", port=24666)
        node["up_mbps"] = 4
        self._write_config([node])
        metadata = self._write_metadata(node)
        before_config = self.config.read_bytes()
        self.env["QUIC_METADATA_PATH"] = str(metadata)
        result = self.shell(
            r'''
            confirm() {
                jq '.server="external.example"' "$QUIC_METADATA_PATH" >"$QUIC_METADATA_PATH.tmp" &&
                    mv -- "$QUIC_METADATA_PATH.tmp" "$QUIC_METADATA_PATH"
                return 0
            }
            configure_quic_bbr_locked "$1"
            ''',
            node["tag"],
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["server"], "external.example")
        self.assertNotIn("quic_bbr_restore", json.loads(metadata.read_text(encoding="utf-8")))

    def test_record_write_failure_rolls_back_enable_and_record_delete_failure_rolls_back_disable(self) -> None:
        node = self.hysteria2(tag="hy2-record-fail", port=24671)
        node["up_mbps"] = 3
        self._write_config([node])
        metadata = self._write_metadata(node)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        body = r'''
            eval "$(declare -f atomic_copy | sed '1s/^atomic_copy/real_atomic_copy/')"
            atomic_copy() { if [[ $2 == *"/clients/hy2-record-fail.json" ]]; then return 1; fi; real_atomic_copy "$@"; }
            confirm() { return 0; }
            configure_quic_bbr_locked "$1"
        '''
        result = self.shell(body, node["tag"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

        self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', node["tag"], check=True)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self.shell(body.replace("configure_quic_bbr_locked", "disable_quic_bbr_locked"), node["tag"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_disable_restart_failure_rolls_back_record_and_service(self) -> None:
        node = self.hysteria2(tag="hy2-disable-restart", port=24681)
        node["up_mbps"] = 2
        self._write_config([node])
        metadata = self._write_metadata(node)
        self.shell('confirm() { return 0; }; configure_quic_bbr_locked "$1"', node["tag"], check=True)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell('confirm() { return 0; }; disable_quic_bbr_locked "$1"', node["tag"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])

    def test_record_quic_bbr_adopts_only_matching_backup_and_never_overwrites_record(self) -> None:
        original = self.hysteria2(tag="hy2-adopt", port=24701)
        original["up_mbps"] = 15
        original["down_mbps"] = 25
        self._write_config([original])
        metadata = self._write_metadata(original)
        bundle = Path(self.shell("create_backup", check=True).stdout.strip())
        current = self._apply_helper(original)
        self._write_config([current])
        metadata.write_text(json.dumps({"server": "example.com", "public_key": "", "inbound": current}) + "\n", encoding="utf-8")
        before_config = self.config.read_bytes()
        after_bundle = Path(self.shell("create_backup", check=True).stdout.strip())
        rejected = self.shell('confirm() { return 0; }; record_quic_bbr_locked "$1" "$2"', original["tag"], str(after_bundle))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn("quic_bbr_restore", json.loads(metadata.read_text(encoding="utf-8")))
        result = self.shell('confirm() { return 0; }; record_quic_bbr_locked "$1" "$2"', original["tag"], str(bundle), check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))
        adopted = json.loads(metadata.read_text(encoding="utf-8"))["quic_bbr_restore"]
        self.assertEqual(adopted["settings"], {"up_mbps": 15, "down_mbps": 25})
        adopted_before = json.dumps(adopted, sort_keys=True)

        mismatched = json.loads(bundle.read_text(encoding="utf-8"))
        mismatched["config"]["inbounds"][0]["up_mbps"] = 99
        mismatched["config"]["inbounds"][0]["down_mbps"] = 88
        mismatched["clients"][original["tag"]]["inbound"] = mismatched["config"]["inbounds"][0]
        mismatched.pop("config_source", None)
        bad_bundle = self.root / "bad-adopt-bundle.json"
        fixture.dump(bad_bundle, mismatched)
        self.shell('validate_bundle "$1"', str(bad_bundle), check=True)
        result = self.shell('confirm() { return 0; }; record_quic_bbr_locked "$1" "$2"', original["tag"], str(bad_bundle))
        self.assertNotEqual(result.returncode, 0)
        current_record = json.loads(metadata.read_text(encoding="utf-8"))["quic_bbr_restore"]
        self.assertEqual(json.dumps(current_record, sort_keys=True), adopted_before)

    def test_cli_quic_bbr_and_status_routes(self) -> None:
        body = r'''
            SB_ROOT_PREFIX=""
            require_root() { return 0; }
            require_systemd() { return 0; }
            safe_paths() { return 0; }
            is_managed() { return 0; }
            with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
            enable_bbr() { printf 'TCP_BBR\n'; }
            configure_quic_bbr_locked() { printf 'QUIC_BBR:%s\n' "$*"; }
            show_quic_bbr_status() { printf 'QUIC_STATUS\n'; }
            main bbr
            main quic-bbr hy2-node
            main quic-bbr-status
            main quic-bbr extra extra >/dev/null 2>&1 || printf 'INVALID\n'
        '''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TCP_BBR", result.stdout)
        self.assertIn("LOCK:configure_quic_bbr_locked hy2-node", result.stdout)
        self.assertIn("QUIC_STATUS", result.stdout)
        self.assertIn("INVALID", result.stdout)


if __name__ == "__main__":
    unittest.main()
