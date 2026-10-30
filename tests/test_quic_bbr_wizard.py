#!/usr/bin/env python3
"""Focused tests for enabling QUIC BBR from add/edit wizards."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class QuicBbrWizardTest(unittest.TestCase):
    """Reuse the fixture without inheriting the complete manager test class."""

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

    def hysteria2(self, tag: str, port: int, *, bbr: bool = False, record: dict | None = None) -> dict:
        certificate, private_key = self.tls_material()
        node = fixture.ManagerTest.generate_inbound(
            self, "hysteria2", "hy-password", tls_server_name="example.com",
            certificate=certificate, key=private_key,
        ) | {"tag": tag, "listen_port": port}
        if bbr:
            node["ignore_client_bandwidth"] = True
        if record is not None:
            node["up_mbps"] = 0
        return node

    def tuic(self, tag: str, port: int, congestion: str = "cubic") -> dict:
        certificate, private_key = self.tls_material()
        return fixture.ManagerTest.generate_inbound(
            self, "tuic", "b7efdc96-87aa-4910-81b4-374b2eecc737",
            tls_server_name="example.com", certificate=certificate, key=private_key,
            tuic_password="tuic-password",
        ) | {"tag": tag, "listen_port": port, "congestion_control": congestion}

    def _write_config(self, nodes: list[dict]) -> None:
        self.config.write_text(
            '{\n'
            '  // wizard QUIC BBR comment\n'
            '  "log": {"level": "info"},\n'
            '  "inbounds": [' + ",".join(json.dumps(node, ensure_ascii=False) for node in nodes) + '],\n'
            '  "outbounds": [{"type": "direct", "tag": "direct"}]\n'
            '}\n',
            encoding="utf-8",
        )

    def _write_metadata(self, node: dict, record: dict | None = None) -> Path:
        path = self.state_dir / "clients" / (node["tag"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"server": "example.com", "public_key": "", "inbound": node}
        if record is not None:
            value["quic_bbr_restore"] = record
        fixture.dump(path, value)
        return path

    def _assert_export(self, node: dict, expected_congestion: str | None = None) -> None:
        source = self.root / (node["tag"] + "-export.json")
        fixture.dump(source, node)
        exported = self.shell('generate_client "$1" "example.com"', str(source), check=True)
        outbound = json.loads(exported.stdout)
        self.assertEqual(outbound["server"], "example.com")
        if node["type"] == "hysteria2":
            self.assertNotIn("up_mbps", outbound)
            self.assertNotIn("down_mbps", outbound)
            self.assertNotIn("congestion_control", outbound)
        else:
            expected_wire_congestion = expected_congestion or "cubic"
            self.assertEqual(outbound["congestion_control"], expected_wire_congestion)
        outbound_path = self.root / (node["tag"] + "-export-outbound.json")
        outbound_path.write_text(exported.stdout, encoding="utf-8")
        link = self.shell('client_link "$1" "$2"', str(outbound_path), node["tag"], check=True).stdout.strip()
        parsed = urlsplit(link)
        query = parse_qs(parsed.query)
        self.assertEqual(query["sni"], ["example.com"])
        auth = parsed.password if node["type"] == "tuic" else parsed.username
        expected_auth = node["users"][0]["password"]
        self.assertEqual(unquote(auth or ""), expected_auth)
        if node["type"] == "tuic":
            self.assertEqual(query["congestion_control"], [expected_congestion or "cubic"])
        else:
            self.assertNotIn("congestion_control", query)
            self.assertNotIn("up_mbps", query)
            self.assertNotIn("down_mbps", query)
            if node.get("obfs"):
                self.assertEqual(query["obfs"], ["salamander"])
                self.assertEqual(query["obfs-password"], [node["obfs"]["password"]])
        if fixture.REAL_CORE:
            checked = subprocess.run(
                [fixture.REAL_CORE, "check", "-c", str(self.config)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def _wizard_core(self) -> Path:
        core = self.root / "wizard-core"
        core.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == generate && $2 == uuid ]]; then printf '%s\\n' 'b7efdc96-87aa-4910-81b4-374b2eecc737'; exit 0; fi\n"
            "if [[ $1 == generate && $2 == rand ]]; then printf '%s\\n' wizard-password; exit 0; fi\n",
            encoding="utf-8",
        )
        core.chmod(0o755)
        return core

    def test_add_wizard_defaults_to_bbr_and_option_two_keeps_automatic_mode(self) -> None:
        core = self._wizard_core()
        certificate, private_key = self.tls_material()
        prompt_log = self.root / "add-prompts.log"
        cases = (
            ("hysteria2", "7", "", True, {"settings": {}}),
            ("hysteria2-off", "7", "2", False, None),
            ("tuic", "8", "", True, {"settings": {"congestion_control": "cubic"}}),
            ("tuic-off", "8", "2", False, None),
        )
        for index, (protocol, choice, quic_choice, enabled, record_fragment) in enumerate(cases):
            with self.subTest(protocol=protocol, quic_choice=quic_choice):
                self.config.write_bytes(self.original_bytes)
                tag = "wizard-" + protocol
                env = dict(
                    self.env,
                    TEST_QUIC_CHOICE=quic_choice,
                    TEST_TAG=tag,
                    TEST_CERTIFICATE=str(certificate),
                    TEST_PRIVATE_KEY=str(private_key),
                    TEST_WIZARD_CORE=str(core),
                    TEST_PROMPT_LOG=str(prompt_log),
                )
                prompt_log.write_text("", encoding="utf-8")
                body = r'''
                    require_core() { return 0; }
                    prompt() {
                        local variable=$1
                        printf '%s|%s\n' "$variable" "${3-}" >> "$TEST_PROMPT_LOG"
                        case "$variable" in
                            choice) printf -v "$variable" '%s' "$TEST_CHOICE" ;;
                            tag) printf -v "$variable" '%s' "$TEST_TAG" ;;
                            port) printf -v "$variable" '%s' "$((25000 + TEST_INDEX))" ;;
                            obfs_choice) printf -v "$variable" 1 ;;
                            quic_bbr_choice) printf -v "$variable" '%s' "${TEST_QUIC_CHOICE:-${3-1}}" ;;
                            tls_name) printf -v "$variable" example.com ;;
                            certificate) printf -v "$variable" '%s' "$TEST_CERTIFICATE" ;;
                            tls_key) printf -v "$variable" '%s' "$TEST_PRIVATE_KEY" ;;
                            server) printf -v "$variable" example.com ;;
                            *) printf -v "$variable" '%s' '' ;;
                        esac
                    }
                    prompt_hysteria2_obfs() { printf -v "$1" ''; }
                    prompt_secret() { printf -v "$1" wizard-password; }
                    prompt_tls_certificate_files() {
                        printf -v certificate '%s' "$TEST_CERTIFICATE"
                        printf -v tls_key '%s' "$TEST_PRIVATE_KEY"
                    }
                    CORE_BIN="$TEST_WIZARD_CORE"
                    add_wizard_locked
                '''
                env["TEST_CHOICE"] = choice
                env["TEST_INDEX"] = str(index)
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; ' + body, "add-quic-wizard", str(fixture.SCRIPT)],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
                metadata = json.loads((self.state_dir / "clients" / (tag + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["type"], "hysteria2" if choice == "7" else "tuic")
                if choice == "7":
                    self.assertEqual(saved.get("ignore_client_bandwidth", False), enabled)
                    self.assertNotIn("up_mbps", saved)
                    self.assertNotIn("down_mbps", saved)
                else:
                    self.assertEqual(saved["congestion_control"], "bbr" if enabled else "cubic")
                if record_fragment is None:
                    self.assertNotIn("quic_bbr_restore", metadata)
                else:
                    self.assertEqual(metadata["quic_bbr_restore"]["settings"], record_fragment["settings"])
                self._assert_export(saved, "bbr" if saved["type"] == "tuic" and enabled else "cubic" if saved["type"] == "tuic" else None)
                if enabled:
                    self.shell('confirm() { return 0; }; disable_quic_bbr_locked "$1"', tag, check=True)
                    restored = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
                    restored_metadata = json.loads((self.state_dir / "clients" / (tag + ".json")).read_text(encoding="utf-8"))
                    if restored["type"] == "hysteria2":
                        self.assertNotIn("ignore_client_bandwidth", restored)
                    else:
                        self.assertEqual(restored["congestion_control"], "cubic")
                    self.assertNotIn("quic_bbr_restore", restored_metadata)
                prompt_lines = prompt_log.read_text(encoding="utf-8").splitlines()
                quic_lines = [line for line in prompt_lines if line.startswith("quic_bbr_choice|")]
                self.assertTrue(quic_lines)
                self.assertEqual(quic_lines[0].split("|", 1)[1], "1")

    def test_add_wizard_quic_bbr_cancel_leaves_config_and_metadata_untouched(self) -> None:
        core = self._wizard_core()
        certificate, private_key = self.tls_material()
        body = r'''
            require_core() { return 0; }
            prompt() {
                case "$1" in
                    choice) printf -v "$1" 7 ;;
                    tag) printf -v "$1" cancel-hy2 ;;
                    port) printf -v "$1" 25101 ;;
                    obfs_choice) printf -v "$1" 1 ;;
                    quic_bbr_choice) printf -v "$1" 0 ;;
                    tls_name) printf -v "$1" example.com ;;
                    certificate) printf -v "$1" '%s' "$TEST_CERTIFICATE" ;;
                    tls_key) printf -v "$1" '%s' "$TEST_PRIVATE_KEY" ;;
                    server) printf -v "$1" example.com ;;
                    *) printf -v "$1" '' ;;
                esac
            }
            prompt_hysteria2_obfs() { printf -v "$1" ''; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
            CORE_BIN="$TEST_WIZARD_CORE"
            add_wizard_locked
        '''
        env = dict(self.env, TEST_CERTIFICATE=str(certificate), TEST_PRIVATE_KEY=str(private_key), TEST_WIZARD_CORE=str(core))
        before = self.config.read_bytes()
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "add-quic-cancel", str(fixture.SCRIPT)],
            env=env, text=True, capture_output=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state_dir / "clients/cancel-hy2.json").exists())

    def test_add_wizard_restart_failure_rolls_back_record_config_metadata_and_service(self) -> None:
        core = self._wizard_core()
        certificate, private_key = self.tls_material()
        body = r'''
            require_core() { return 0; }
            prompt() {
                case "$1" in
                    choice) printf -v "$1" 7 ;;
                    tag) printf -v "$1" wizard-restart-fail ;;
                    port) printf -v "$1" 25111 ;;
                    obfs_choice) printf -v "$1" 1 ;;
                    quic_bbr_choice) printf -v "$1" 1 ;;
                    tls_name) printf -v "$1" example.com ;;
                    certificate) printf -v "$1" '%s' "$TEST_CERTIFICATE" ;;
                    tls_key) printf -v "$1" '%s' "$TEST_PRIVATE_KEY" ;;
                    server) printf -v "$1" example.com ;;
                    *) printf -v "$1" '' ;;
                esac
            }
            prompt_hysteria2_obfs() { printf -v "$1" ''; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
            CORE_BIN="$TEST_WIZARD_CORE"
            add_wizard_locked
        '''
        self._write_config([])
        before_config = self.config.read_bytes()
        self.state(active=True, restart_failures=1)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "add-quic-restart-fail", str(fixture.SCRIPT)],
            env=dict(self.env, TEST_CERTIFICATE=str(certificate), TEST_PRIVATE_KEY=str(private_key), TEST_WIZARD_CORE=str(core)),
            text=True, capture_output=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertFalse((self.state_dir / "clients/wizard-restart-fail.json").exists())
        self.assertTrue(self.state()["active"])

    def test_edit_wizard_quic_bbr_enable_disable_capture_and_preserve_record(self) -> None:
        certificate, private_key = self.tls_material()
        record_hy2 = {"format": "sing-box-manager-quic-bbr-v1", "type": "hysteria2", "settings": {"up_mbps": 6}}
        record_tuic = {"format": "sing-box-manager-quic-bbr-v1", "type": "tuic", "settings": {"congestion_control": "new_reno"}}
        cases = (
            ("capture-hy2", self.hysteria2("capture-hy2", 25201), None, "", {"up_mbps": 4}, True),
            ("repeat-hy2", self.hysteria2("repeat-hy2", 25200, bbr=True), record_hy2, "", {"up_mbps": 6}, True),
            ("close-hy2", self.hysteria2("close-hy2", 25202, bbr=True), record_hy2, "2", {"up_mbps": 6}, False),
            ("close-tuic", self.tuic("close-tuic", 25203, "bbr"), record_tuic, "2", {"congestion_control": "new_reno"}, False),
            ("capture-tuic", self.tuic("capture-tuic", 25204, "new_reno"), None, "", {"congestion_control": "new_reno"}, True),
            ("capture-tuic-missing", self.tuic("capture-tuic-missing", 25205, "cubic"), None, "", {}, True),
            ("capture-tuic-null", self.tuic("capture-tuic-null", 25207, "cubic") | {"congestion_control": None}, None, "", {"congestion_control": None}, True),
            ("close-tuic-null", self.tuic("close-tuic-null", 25206, "bbr"), {"format": "sing-box-manager-quic-bbr-v1", "type": "tuic", "settings": {"congestion_control": None}}, "2", {"congestion_control": None}, False),
        )
        for tag, node, record, quic_choice, expected_settings, enabling in cases:
            with self.subTest(tag=tag):
                if tag == "capture-hy2":
                    node["up_mbps"] = 4
                if tag == "capture-tuic-missing":
                    node.pop("congestion_control", None)
                self._write_config([node])
                metadata_path = self._write_metadata(node, record)
                env = dict(
                    self.env,
                    TEST_QUIC_CHOICE=quic_choice,
                    TEST_CERTIFICATE=str(certificate),
                    TEST_PRIVATE_KEY=str(private_key),
                    TEST_CREDENTIAL="edited-hy-password" if node["type"] == "hysteria2" else node["users"][0]["uuid"],
                    TEST_PROMPT_LOG=str(self.root / (tag + "-prompts.log")),
                )
                body = r'''
                    SECRET_CALLS=0
                    prompt() {
                        local variable=$1
                        printf '%s|%s\n' "$variable" "${3-}" >> "$TEST_PROMPT_LOG"
                        case "$variable" in
                            quic_bbr_choice) printf -v "$variable" '%s' "${TEST_QUIC_CHOICE:-${3-1}}" ;;
                            tag|port) printf -v "$variable" '%s' "${3-}" ;;
                            tls_name|server) printf -v "$variable" example.com ;;
                            *) printf -v "$variable" '%s' "${3-}" ;;
                        esac
                    }
                    prompt_secret() {
                        SECRET_CALLS=$((SECRET_CALLS + 1))
                        if [[ $SECRET_CALLS == 1 ]]; then printf -v "$1" '%s' "$TEST_CREDENTIAL"; else printf -v "$1" '%s' "${3-}"; fi
                    }
                    prompt_hysteria2_obfs() { printf -v "$1" '%s' "${2-}"; }
                    prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
                    confirm() { return 0; }
                    edit_wizard_locked "$1"
                '''
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; shift; ' + body, "edit-quic-wizard", str(fixture.SCRIPT), tag],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                quic_lines = [
                    line for line in Path(env["TEST_PROMPT_LOG"]).read_text(encoding="utf-8").splitlines()
                    if line.startswith("quic_bbr_choice|")
                ]
                self.assertTrue(quic_lines)
                self.assertEqual(quic_lines[0].split("|", 1)[1], "1")
                saved = json.loads(self.shell("config_jq .", check=True).stdout)["inbounds"][0]
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if enabling:
                    if node["type"] == "hysteria2":
                        self.assertTrue(saved["ignore_client_bandwidth"])
                        self.assertNotIn("up_mbps", saved)
                    else:
                        self.assertEqual(saved["congestion_control"], "bbr")
                    self.assertEqual(metadata["quic_bbr_restore"]["settings"], expected_settings)
                else:
                    if node["type"] == "hysteria2":
                        self.assertEqual(saved["up_mbps"], expected_settings["up_mbps"])
                        self.assertNotIn("ignore_client_bandwidth", saved)
                    else:
                        self.assertEqual(saved["congestion_control"], expected_settings["congestion_control"])
                    self.assertNotIn("quic_bbr_restore", metadata)
                self._assert_export(saved, saved.get("congestion_control") if saved["type"] == "tuic" else None)

    def test_edit_confirmation_metadata_change_is_not_overwritten(self) -> None:
        node = self.hysteria2("wizard-meta-race", 25341)
        node["up_mbps"] = 4
        self._write_config([node])
        metadata = self._write_metadata(node)
        before = self.config.read_bytes()
        self.env["TEST_META_PATH"] = str(metadata)
        body = r'''
            prompt() { printf -v "$1" '%s' "${3-}"; }
            prompt_secret() { printf -v "$1" '%s' "${3-}"; }
            prompt_hysteria2_obfs() { printf -v "$1" '%s' "${2-}"; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$2"; printf -v tls_key '%s' "$3"; }
            confirm() {
                python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["external_change"]=True; open(p,"w").write(json.dumps(d))' "$TEST_META_PATH"
                return 0
            }
            edit_wizard_locked wizard-meta-race
        '''
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        current = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertTrue(current["external_change"])
        self.assertNotIn("quic_bbr_restore", current)

    def test_edit_wizard_quic_bbr_cancel_keeps_jsonc_and_metadata(self) -> None:
        node = self.hysteria2("cancel-edit-quic", 25301)
        node["up_mbps"] = 5
        self._write_config([node])
        metadata_path = self._write_metadata(node)
        before_config = self.config.read_bytes()
        before_metadata = metadata_path.read_bytes()
        body = r'''
            prompt() { if [[ $1 == quic_bbr_choice ]]; then printf -v "$1" 0; else printf -v "$1" '%s' "${3-}"; fi; }
            prompt_secret() { printf -v "$1" '%s' "${3-}"; }
            prompt_hysteria2_obfs() { printf -v "$1" '%s' "${2-}"; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
            edit_wizard_locked cancel-edit-quic
        '''
        cert, key = self.tls_material()
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "cancel-edit-quic", str(fixture.SCRIPT)],
            env=dict(self.env, TEST_CERTIFICATE=str(cert), TEST_PRIVATE_KEY=str(key)),
            text=True, capture_output=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata_path.read_bytes(), before_metadata)

    def test_edit_quic_bbr_close_without_record_refuses_to_guess_original(self) -> None:
        node = self.hysteria2("unknown-original-bbr", 25311, bbr=True)
        self._write_config([node])
        metadata_path = self._write_metadata(node)
        before_config = self.config.read_bytes()
        before_metadata = metadata_path.read_bytes()
        body = r'''
            prompt() { if [[ $1 == quic_bbr_choice ]]; then printf -v "$1" 2; else printf -v "$1" '%s' "${3-}"; fi; }
            prompt_secret() { printf -v "$1" '%s' "${3-}"; }
            prompt_hysteria2_obfs() { printf -v "$1" '%s' "${2-}"; }
            prompt_tls_certificate_files() { printf -v certificate '%s' "$TEST_CERTIFICATE"; printf -v tls_key '%s' "$TEST_PRIVATE_KEY"; }
            edit_wizard_locked unknown-original-bbr
        '''
        cert, key = self.tls_material()
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "unknown-original-bbr", str(fixture.SCRIPT)],
            env=dict(self.env, TEST_CERTIFICATE=str(cert), TEST_PRIVATE_KEY=str(key)),
            text=True, capture_output=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata_path.read_bytes(), before_metadata)
