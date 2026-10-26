#!/usr/bin/env python3
"""Offline tests for formatting JSONC without changing its meaning."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class ConfigFormatTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def shell_bytes(self, body: str, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body, "format-test", str(fixture.SCRIPT), *args],
            env=self.env, capture_output=True, timeout=20,
        )

    def _raw(self, newline: str = "\n", trailing: bool = True) -> str:
        text = (
            "{\n"
            "  // line comment with trailing spaces   \n"
            "  /* multi-line block comment\n"
            "     remains in the formatted source */\n"
            '  "empty": [],\n'
            '  "num": -0, /* before sci */\n'
            '  "sci": 1e+3,\n'
            '  "url": "https://example.com/a//b",\n'
            '  "escaped": "quote \\\" // /* keep */ \\\\",\n'
            '  "array": [1, 2, 3] /* array close */,\n'
            '  "obj": {"x": 1 /* object close */}\n'
            "}"
        )
        result = text.replace("\n", newline)
        return result + (newline if trailing else "")

    def _write_metadata(self) -> bytes:
        path = self.state_dir / "clients/node.json"
        fixture.dump(path, {"server": "example.com", "inbound": {"tag": "node"}})
        path.chmod(0o600)
        return path.read_bytes()

    def test_format_source_preserves_comments_strings_tokens_and_crlf(self) -> None:
        source = self.root / "compressed.jsonc"
        raw = self._raw("\r\n", trailing=False).encode("utf-8")
        source.write_bytes(raw)
        result = self.shell_bytes('format_config_source "$1"', str(source))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout
        self.assertIn(b"\r\n", output)
        self.assertNotIn(b"\n", output.replace(b"\r\n", b""))
        self.assertIn(b"https://example.com/a//b", output)
        self.assertIn(b"-0", output)
        self.assertIn(b"1e+3", output)
        self.assertIn(b"/* before sci */", output)
        self.assertTrue(output.endswith(b"\r\n"))
        formatted = self.root / "formatted.jsonc"
        formatted.write_bytes(output)
        parsed = self.shell('json_query "$1" .', str(formatted))
        self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)
        value = json.loads(parsed.stdout)
        self.assertEqual(value["empty"], [])
        self.assertEqual(value["array"], [1, 2, 3])
        second = self.shell_bytes('format_config_source "$1"', str(formatted))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(second.stdout, output)

    def test_invalid_jsonc_and_empty_arrays_are_handled(self) -> None:
        source = self.root / "empty.jsonc"
        source.write_text('{\n  /* empty */\n  "inbounds": [],\n  "outbounds": []\n}\n', encoding="utf-8")
        result = self.shell('format_config_source "$1"', str(source), check=True)
        self.assertIn("inbounds", result.stdout)
        invalid = self.root / "invalid.jsonc"
        for raw in ('{"a": 1,}', '[1,]', '{"a": /* unclosed'):
            with self.subTest(raw=raw):
                invalid.write_text(raw, encoding="utf-8")
                result = self.shell('format_config_source "$1"', str(invalid))
                self.assertNotEqual(result.returncode, 0)

    def test_new_nodes_are_written_as_indented_multiline_objects(self) -> None:
        self.config.write_text(self.shell("base_config", check=True).stdout, encoding="utf-8")
        node = fixture.ManagerTest.inbound(self, "pretty-node", 18443)
        node_file = self.root / "pretty-node.json"
        fixture.dump(node_file, node)
        self.shell('add_inbound_file "$1"', str(node_file), check=True)
        raw = self.config.read_text(encoding="utf-8")
        self.assertIn('    {\n      "type": "vless",', raw)
        self.assertIn('      "users": [\n        {\n          "uuid": ', raw)
        self.assertEqual(json.loads(self.shell("config_jq '.inbounds[0]'", check=True).stdout), node)

    def test_repeated_format_is_noop_and_validation_failure_never_writes(self) -> None:
        self.config.write_text(self._raw(), encoding="utf-8")
        self.shell('confirm() { return 0; }; format_config_locked', check=True)
        before = (self.config.read_bytes(), self.config.stat().st_mtime_ns)
        backups = sorted((self.state_dir / "backups").glob("format-*.json"))
        self.shell('confirm() { return 99; }; format_config_locked', check=True)
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        self.assertEqual(sorted((self.state_dir / "backups").glob("format-*.json")), backups)
        self.config.write_text(self._raw(), encoding="utf-8")
        original = self.config.read_bytes()
        result = self.shell('validate_config() { return 1; }; confirm() { return 0; }; format_config_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted((self.state_dir / "backups").glob("format-*.json")), backups)

    def test_locked_format_backs_up_preserves_semantics_and_does_not_restart(self) -> None:
        raw = self._raw()
        self.config.write_text(raw, encoding="utf-8")
        self.config.chmod(0o640)
        metadata = self._write_metadata()
        default = self.state_dir / "default-server"
        default.write_text("example.com\n", encoding="utf-8")
        before_mode = self.config.stat().st_mode & 0o777
        self.state(active=True)
        result = self.shell('confirm() { return 0; }; format_config_locked')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.stat().st_mode & 0o777, before_mode)
        self.assertIn("/* before sci */", self.config.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.shell("config_jq -c .array", check=True).stdout), [1, 2, 3])
        self.assertEqual((self.state_dir / "clients/node.json").read_bytes(), metadata)
        self.assertEqual(default.read_text(), "example.com\n")
        calls = fixture.ManagerTest.calls(self, self.system_log)
        self.assertFalse(any(any(action in call for action in ("restart", "start", "stop")) for call in calls))
        backups = list((self.state_dir / "backups").glob("format-*.json"))
        self.assertTrue(backups)
        self.assertTrue(any(path.read_bytes() == raw.encode("utf-8") for path in backups))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))

    def test_cancel_and_concurrent_change_do_not_write(self) -> None:
        raw = self._raw()
        self.config.write_text(raw, encoding="utf-8")
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 1; }; format_config_locked')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        external = b'{"external":true}\n'
        body = r'''
eval "$(declare -f format_config_source | sed '1s/^format_config_source/real_format_config_source/')"
format_config_source() { real_format_config_source "$@"; printf '%s' "$EXTERNAL_BYTES" >"$CONFIG_FILE"; }
'''
        self.env["EXTERNAL_BYTES"] = external.decode()
        result = self.shell(body + 'confirm() { return 0; }; format_config_locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), external)

    def test_atomic_replace_failure_and_term_restore_original_bytes(self) -> None:
        raw = self._raw()
        self.config.write_text(raw, encoding="utf-8")
        for mode in ("fail", "term"):
            with self.subTest(mode=mode):
                self.config.write_text(raw, encoding="utf-8")
                if mode == "fail":
                    hook = 'mv() { command mv "$@"; if [[ ${*: -1} == "$CONFIG_FILE" ]]; then return 1; fi; }'
                else:
                    hook = 'mv() { command mv "$@"; if [[ ${*: -1} == "$CONFIG_FILE" && ! -e "$FORMAT_TERM_ONCE" ]]; then : >"$FORMAT_TERM_ONCE"; kill -TERM "$BASHPID"; fi; }'
                    self.env["FORMAT_TERM_ONCE"] = str(self.root / "format-term-once")
                result = self.shell(hook + '; confirm() { return 0; }; format_config_locked')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))

    def test_directory_created_before_publish_is_not_modified_by_rollback(self) -> None:
        original = self._raw().encode("utf-8")
        self.config.write_bytes(original)
        result = self.shell(r'''
mv() {
    if [[ ${*: -1} == "$CONFIG_FILE" ]]; then
        command rm -f -- "$CONFIG_FILE"
        command mkdir -- "$CONFIG_FILE"
    fi
    command mv "$@"
}
confirm() { return 0; }; format_config_locked
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.config.is_dir())
        self.assertEqual(list(self.config.iterdir()), [])
        backups = list((self.state_dir / "backups").glob("format-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertFalse(list(self.config.parent.glob(".format-*")))

    def test_cli_and_show_config_menu_route_format_operation(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
format_config_locked() { printf 'FORMATTED\n'; }
main format-config
main format-config extra >/dev/null 2>&1 || printf 'INVALID\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LOCK:format_config_locked", result.stdout)
        self.assertIn("FORMATTED", result.stdout)
        self.assertIn("INVALID", result.stdout)
        result = self.shell('require_core(){ return 0; }; prompt(){ printf -v "$1" 9; }; with_lock(){ printf "MENU_LOCK:%s\\n" "$*"; "$@"; }; format_config_locked(){ printf MENU_FORMAT; }; show_config')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MENU_LOCK:format_config_locked", result.stdout)


if __name__ == "__main__":
    unittest.main()
