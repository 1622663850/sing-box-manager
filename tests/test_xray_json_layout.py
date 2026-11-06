#!/usr/bin/env python3
"""Regression checks for persisted Xray JSON/JSONC layout.

The tests use the reusable Xray fixture and drive the manager's normal write
functions.  They assert both semantic validity and stable pretty formatting.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import test_xray_manager as fixture
import test_xray_platform as platform_fixture
import test_xray_split as split_fixture


UUID2 = fixture.UUID2
PUBLIC_KEY = fixture.PUBLIC_KEY


class XrayJsonFormatterTest(unittest.TestCase):
    def test_jsonc_comments_string_tokens_and_crlf_are_preserved(self) -> None:
        source = fixture.SCRIPT.read_text(encoding="utf-8")
        code = source.split("<<'PY_JSONC_EDITOR'\n", 1)[1].split("\nPY_JSONC_EDITOR", 1)[0]
        namespace = {"__name":"xray_json_formatter_test"}
        exec(compile(code, str(fixture.SCRIPT), "exec"), namespace)
        comment = "/* first\r\n\r\n second */"
        original = '// leading\r\n{\r\n\r\n\t"text":"https://example.com/#value\\n\\n\\\"quote\\\"",\r\n"nested": {"empty":[]}, '+comment+'\r\n"number":1e3\r\n}\r\n\r\n'
        formatted = namespace["format_source"](original)
        self.assertEqual(namespace["Parser"](formatted).parse().value,
                         namespace["Parser"](original).parse().value)
        self.assertIn(comment, formatted)
        self.assertIn('\r\n  "text":', formatted)
        self.assertIn('"number": 1e3', formatted)
        self.assertEqual(formatted, namespace["format_source"](formatted))
        self.assertFalse(formatted.endswith('\r\n\r\n'))


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and fixture.SCRIPT.is_file(),
                     "bash, jq, and xray-manager.sh are required")
class XrayJsonLayoutTest(fixture.XrayFixture):
    """Every successful persisted configuration write stays readable and stable."""

    def assert_layout(self, path: Path | None = None) -> None:
        path = path or self.config
        raw = path.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"), "JSON file must end with one newline")
        self.assertNotIn("\n\n", raw, "JSON file contains a blank line")
        self.assertFalse(any(line.rstrip(" \t") != line for line in raw.splitlines()),
                         "JSON file contains trailing whitespace")
        semantic = self.shell('json_query "$1" .', str(path), check=True).stdout
        self.assertTrue(semantic.strip().startswith("{"), "persisted JSON must be an object")
        formatted = self.shell('format_config_source "$1"', str(path), check=True).stdout
        self.assertEqual(raw, formatted, "formatting is not idempotent")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.assertEqual(raw, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def assert_metadata_layout(self) -> None:
        for path in self.clients_dir.glob("*.json"):
            raw = path.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertNotIn("\n\n", raw)
            self.assertFalse(any(line.rstrip(" \t") != line for line in raw.splitlines()))
            self.assertEqual(raw, json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n")

    def assert_pretty_file(self, path: Path) -> None:
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(raw, json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n")
        self.assertNotIn("\n\n", raw)

    def test_setup_creates_pretty_multiline_base_config(self) -> None:
        self.config.unlink()
        self.shell("setup_panel_locked", check=True)
        self.assert_layout()
        raw = self.config.read_text(encoding="utf-8")
        self.assertIn("\n  \"log\":", raw)
        self.assertIn("\n  \"inbounds\": []", raw)

    def test_node_and_user_add_delete_round_trip_has_no_blank_lines(self) -> None:
        for suffix, port in (("a", 18570), ("b", 18575)):
            node = self.write_node("layout-node-" + suffix, port)
            self.shell('add_managed_inbound "$1" "$2" "$3"', str(self.candidate(node)), "example.com", "", check=True)
            self.assert_layout()
            self.assert_metadata_layout()

            add = r'''prompt() { if [[ $1 == name ]]; then printf -v "$1" user-two; fi; }
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_USER_UUID"; }
confirm() { return 0; }
change_inbound_user add "$XR_TEST_LAYOUT_TAG"'''
            self.env.update(XR_TEST_USER_UUID=UUID2, XR_TEST_LAYOUT_TAG=node["tag"])
            self.shell(add, check=True)
            self.assert_layout()
            self.assert_metadata_layout()

            set_mss = r'''prompt() {
    if [[ $1 == mss ]]; then printf -v "$1" '%s' "$XR_TEST_MSS"
    elif [[ $2 == *"选择用户"* ]]; then printf -v "$1" 1
    else printf -v "$1" '%s' "${3:-}"; fi
}
prompt_secret() { printf -v "$1" '%s' "${3:-}"; }
confirm() { return 0; }
edit_wizard_locked "$XR_TEST_LAYOUT_TAG"'''
            self.env.update(XR_TEST_LAYOUT_TAG=node["tag"], XR_TEST_MSS="1300")
            self.shell(set_mss, check=True)
            value = json.loads(self.config.read_text(encoding="utf-8"))
            edited = next(n for n in value["inbounds"] if n["tag"] == node["tag"])
            self.assertEqual(edited["streamSettings"]["sockopt"]["tcpMaxSeg"], 1300)
            self.assert_layout()

            self.env["XR_TEST_MSS"] = ""
            self.shell(set_mss, check=True)
            value = json.loads(self.config.read_text(encoding="utf-8"))
            edited = next(n for n in value["inbounds"] if n["tag"] == node["tag"])
            self.assertNotIn("sockopt", edited["streamSettings"])
            self.assert_layout()

            delete_user = r'''prompt() { printf -v "$1" 2; }
confirm() { return 0; }
change_inbound_user delete "$XR_TEST_LAYOUT_TAG"'''
            self.shell(delete_user, check=True)
            self.assert_layout()
            self.assert_metadata_layout()

            self.shell('confirm() { return 0; }; delete_inbound "$1"', node["tag"], check=True)
            self.assert_layout()
            self.assert_metadata_layout()

    def test_jsonc_import_format_backup_restore_and_compact_replace(self) -> None:
        node = self.write_node("jsonc-layout", 18571)
        self.write_config([node])
        jsonc = "{\n\t// keep layout comment\n\t\"log\": {\"loglevel\": \"warning\"},\n\t\"inbounds\": [" + json.dumps(node, ensure_ascii=False) + "],\n\t\"outbounds\": [{\"protocol\": \"freedom\", \"tag\": \"direct\", \"settings\": {}}],\n\t\"routing\": {\"rules\": []}\n}\n"
        self.config.write_text(jsonc, encoding="utf-8")
        self.shell("format_config_locked", check=True)
        self.assert_layout()
        formatted_once = self.config.read_bytes()
        self.shell("format_config_locked", check=True)
        self.assertEqual(self.config.read_bytes(), formatted_once)
        self.assertIn("keep layout comment", self.config.read_text(encoding="utf-8"))

        import_jsonc = self.root / "import-crlf.jsonc"
        import_jsonc.write_bytes(("{\r\n  // import comment\r\n  \"log\": {\"loglevel\": \"info\"},\r\n  \"inbounds\": [" + json.dumps(node, ensure_ascii=False) + "],\r\n"
                                 "  \"outbounds\": [{\"protocol\": \"freedom\", \"tag\": \"direct\", \"settings\": {}}],\r\n"
                                 "  \"routing\": {\"rules\": []},\r\n  \"escaped\": \"quote \\\"x\\\"\\\\y\"\r\n}\r\n").encode("utf-8"))
        self.shell('import_config "$1"', str(import_jsonc), check=True)
        self.assert_layout()
        self.assertIn("import comment", self.config.read_text(encoding="utf-8"))
        self.assertIn(b'\r\n', self.config.read_bytes())
        self.assertEqual(json.loads(self.shell('config_jq .',check=True).stdout)['escaped'], 'quote "x"\\y')

        compact = self.root / "compact-import.json"
        compact.write_text(json.dumps({"log": {"loglevel": "info"}, "inbounds": [node],
                                       "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {}}],
                                       "routing": {"rules": []}}, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
        self.shell('import_config "$1"', str(compact), check=True)
        self.assert_layout()

        backup = self.shell("create_backup", check=True)
        bundle = self.backup_path(backup)
        self.assert_pretty_file(bundle)
        compact_before_backup = b'{"log":{"loglevel":"warning"},"inbounds":[],"outbounds":[{"protocol":"freedom","tag":"direct","settings":{}}],"routing":{"rules":[]}}\n'
        self.config.write_bytes(compact_before_backup)
        compact_bundle = self.backup_path(self.shell("create_backup", check=True))
        self.assert_pretty_file(compact_bundle)
        self.shell('confirm() { return 0; }; restore_backup "$1"', str(compact_bundle), check=True)
        self.assert_layout()
        self.config.write_text("{\"inbounds\":[],\"outbounds\":[] ,\"routing\":{\"rules\":[]}}\n", encoding="utf-8")
        self.shell('confirm() { return 0; }; restore_backup "$1"', str(bundle), check=True)
        self.assert_layout()

    def test_exports_and_xhttp_metadata_do_not_create_unformatted_json(self) -> None:
        node = self.write_node("split-layout", 18576)
        node["streamSettings"] = split_fixture.upload_outbound()["streamSettings"]
        node["streamSettings"]["tlsSettings"] = {"serverName":"upload.example","certificates":[
            {"certificateFile":"/fixture/cert.pem","keyFile":"/fixture/key.pem"}]}
        self.write_config([node])
        self.write_metadata(node, server="example.com")
        self.config.write_text('\n\n' + json.dumps(json.loads(self.config.read_text())) + '\n\n',encoding='utf-8')
        self.state(active=True)
        config_before = self.config.read_bytes()
        calls_before = self.calls()
        split_path = self.root / "download.json"
        split_fixture.write_json(split_path, split_fixture.download_settings(path="/xhttp"))
        self.shell('set_xhttp_split_locked "$1" "$2"', node["tag"], str(split_path), check=True)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse(any(call[0] in {'start','stop','restart','enable','disable'} for call in self.calls()[len(calls_before):]))
        self.assert_metadata_layout()
        export = self.root / "exports.json"
        self.shell('exports json "$1"', str(export), check=True)
        self.assert_pretty_file(export)
        self.shell('set_xhttp_split_locked "$1" -',node["tag"],check=True)
        self.assert_metadata_layout()
        self.assertEqual(self.config.read_bytes(),config_before)
        self.assertFalse(any(call[0] in {'start','stop','restart','enable','disable'} for call in self.calls()[len(calls_before):]))

    def test_outbound_and_route_writes_keep_arrays_pretty(self) -> None:
        node = self.write_node("route-layout", 18572)
        self.write_config([node], outbounds=[{"protocol": "freedom", "tag": "direct", "settings": {}},
                                             {"protocol": "freedom", "tag": "proxy", "settings": {}}],
                          routing={"domainStrategy": "AsIs", "rules": []})
        outbound_file = self.root / "outbound-compact.json"
        outbound_file.write_text(json.dumps([{"protocol": "freedom", "tag": "extra", "settings": {}}], separators=(",", ":")), encoding="utf-8")
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(outbound_file), check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; bind_outbound_locked "$1" "$2"', node["tag"], "proxy", check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; bind_user_outbound_locked "$1" 1 "$2"', node["tag"], "extra", check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; bind_user_outbound_locked "$1" 1 -', node["tag"], check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; bind_outbound_locked "$1" -', node["tag"], check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; delete_outbound_locked "$1"', "extra", check=True)
        self.assert_layout()

    def test_ip_preference_and_quic_bbr_writes_keep_format_and_rollback_bytes(self) -> None:
        node = self.write_node("ip-layout", 18573)
        self.write_config([node], outbounds=[{"protocol": "freedom", "tag": "direct", "settings": {}}],
                          routing={"domainStrategy": "AsIs", "rules": []})
        self.env["XR_MOCK_CURL_MODE"] = "success"
        self.shell('confirm() { return 0; }; configure_outbound_preference_locked prefer_ipv4', check=True)
        self.assert_layout()
        self.config.write_text('\n\n' + json.dumps(json.loads(self.config.read_text())) + '\n\n',encoding='utf-8')
        before = self.config.read_bytes()
        self.state(active=True, restart_failures=1)
        failed = self.shell('confirm() { return 0; }; configure_outbound_preference_locked prefer_ipv6')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        invalid = self.root / "invalid.json"
        invalid.write_text('{"inbounds":',encoding='utf-8')
        self.assertNotEqual(self.shell('import_config "$1"',str(invalid)).returncode,0)
        self.assertEqual(self.config.read_bytes(),before)

        self.config.write_text(json.dumps(self.original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        hy2 = self.write_hy2("hy2-layout", 18574)
        self.write_config([hy2])
        self.write_metadata(hy2, server="hy2.example", public_key=PUBLIC_KEY)
        self.shell('confirm() { return 0; }; quic_bbr_change_locked enable "$1"', hy2["tag"], check=True)
        self.assert_layout()
        self.shell('confirm() { return 0; }; quic_bbr_change_locked disable "$1"', hy2["tag"], check=True)
        self.assert_layout()

    def test_bbr_runtime_and_purge_recovery_records_are_pretty_json(self) -> None:
        platform_fixture.XrayPlatformTest._install_bbr_stubs(self)
        self.shell('confirm() { return 0; }; enable_bbr_locked',check=True)
        baseline = self.shell('bbr_restore_backup',check=True).stdout.strip()
        self.assert_pretty_file(Path(baseline) / 'runtime.json')
        self.shell('purge_bbr_settings',check=True)
        self.assert_pretty_file(self.state_dir / 'purge-bbr-restored.json')


if __name__ == "__main__":
    unittest.main()
