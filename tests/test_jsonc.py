#!/usr/bin/env python3
"""Regression coverage for comment-aware sing-box configuration reads."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class JsoncTest(unittest.TestCase):
    """Reuse the isolated fixture without inheriting the main test class."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def calls(self, log: Path) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, log)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def inbound(self, tag: str = "commented", port: int = 18443) -> dict:
        return fixture.ManagerTest.inbound(self, tag, port)

    def _backup_path(self) -> Path:
        result = self.shell("create_backup", check=True)
        backup = Path(result.stdout.strip())
        self.assertTrue(backup.is_file())
        return backup

    def _write_commented_config(self, *, tag: str = "commented", port: int = 18443) -> tuple[str, dict]:
        node = self.inbound(tag, port)
        raw = (
            '{\n'
            '  // UTF-8 顶层注释\n'
            '  /* block comment\n'
            '     跨行注释 */\n'
            '  "log": {"level": "info", "timestamp": true},\n'
            f'  "inbounds": [{json.dumps(node, ensure_ascii=False)}],\n'
            '  "outbounds": [{"type": "direct", "tag": "direct"}],\n'
            '  "route": {"final": "direct"} // 文件末尾行注释\n'
            '}\n'
        )
        self.config.write_text(raw, encoding="utf-8")
        metadata = self.state_dir / "clients" / f"{tag}.json"
        fixture.dump(metadata, {"server": "example.com", "public_key": "", "inbound": node})
        metadata.chmod(0o600)
        return raw, node

    def test_restart_start_and_enable_accept_comments_without_rewriting_config(self) -> None:
        raw, _ = self._write_commented_config()
        self.state(active=True)
        result = self.shell("service_action restart")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

        raw, _ = self._write_commented_config()
        self.state(active=False)
        result = self.shell("service_action start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

        raw, _ = self._write_commented_config()
        self.state(active=False, enabled=False)
        result = self.shell("service_action enable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

    def test_comment_aware_list_client_backup_export_and_diagnose(self) -> None:
        raw, _ = self._write_commented_config()
        result = self.shell("show_config_list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("commented", result.stdout)

        result = self.shell('show_client "$1"', "commented")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vless://", result.stdout)

        result = self.shell("create_backup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backup_paths = [Path(line) for line in result.stdout.splitlines() if line.startswith("/")]
        self.assertEqual(len(backup_paths), 1)
        self.assertEqual(json.loads(backup_paths[0].read_text())["config"]["inbounds"][0]["tag"], "commented")

        exported = self.root / "comments-links.txt"
        result = self.shell('export_clients links "$1"', str(exported))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vless://", exported.read_text(encoding="utf-8"))

        self.state(active=True)
        result = self.shell("ss() { printf 'LISTEN\\n'; }; diagnose")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)

    def test_comment_aware_full_import_preserves_raw_bytes_and_single_inbound_imports(self) -> None:
        raw, node = self._write_commented_config()
        full = self.root / "full-commented.json"
        full.write_text(raw, encoding="utf-8")
        self.state(active=False)
        result = self.shell('import_config "$1"', str(full))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))

        added = self.inbound("added", 18444)
        single = self.root / "single-commented.json"
        single.write_text(
            '/* 单入站注释 */\n' + json.dumps(added, ensure_ascii=False) + '\n',
            encoding="utf-8",
        )
        result = self.shell('add_inbound_file "$1"', str(single))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.shell('config_jq -r ".inbounds | length"')
        self.assertEqual(result.stdout.strip(), "2")
        self.assertEqual(node["tag"], "commented")

    def test_edit_noop_keeps_comments_and_failed_change_restores_raw_backup(self) -> None:
        raw, node = self._write_commented_config()
        same = self.root / "same.json"
        fixture.dump(same, node)
        result = self.shell(
            'replace_inbound_file "$1" "$2" example.com "" "$3"',
            "commented", str(same), str(same),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))

        changed = dict(node)
        changed["listen_port"] = 18445
        changed_file = self.root / "changed.json"
        fixture.dump(changed_file, changed)
        self.state(active=True, restart_failures=1)
        result = self.shell(
            'replace_inbound_file "$1" "$2" example.com "" "$3"',
            "commented", str(changed_file), str(same),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))
        backups = sorted((self.state_dir / "backups").glob("edit-*/config.json"))
        self.assertTrue(backups)
        self.assertEqual(backups[-1].read_bytes(), raw.encode("utf-8"))

    def test_delete_add_and_restore_accept_commented_config_semantics(self) -> None:
        raw, node = self._write_commented_config()
        initial_backup = self._backup_path()
        initial_metadata = (self.state_dir / "clients" / "commented.json").read_bytes()
        self.state(active=False)
        result = self.shell('confirm() { return 0; }; delete_inbound "$1"', "commented")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.shell('config_jq -r ".inbounds | length"')
        self.assertEqual(result.stdout.strip(), "0")

        added = self.inbound("added", 18444)
        single = self.root / "added.json"
        fixture.dump(single, added)
        result = self.shell('add_managed_inbound "$1" example.com ""', str(single))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.shell('config_jq -r ".inbounds[0].tag"')
        self.assertEqual(result.stdout.strip(), "added")

        # Restoring the original bundle must recover the original node set and
        # metadata after both delete and add mutations.
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(initial_backup))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.shell('config_jq -r ".inbounds[0].tag"').stdout.strip(), "commented")
        self.assertEqual(list((self.state_dir / "clients").glob("*.json")), [self.state_dir / "clients" / "commented.json"])
        self.assertEqual((self.state_dir / "clients" / "commented.json").read_bytes(), initial_metadata)
        self.assertFalse((self.state_dir / "clients" / "added.json").exists())

    def test_noop_restore_of_commented_config_preserves_raw_bytes_without_restart(self) -> None:
        raw, _ = self._write_commented_config()
        backup = self._backup_path()
        self.state(active=True)
        before_calls = len(self.calls(self.system_log))
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(backup))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)[before_calls:]))

    def test_jsonc_preserves_urls_escapes_utf8_comments_and_rejects_invalid_input(self) -> None:
        values = self.root / "strings.jsonc"
        escaped = 'quote " // /* keep */ \\'
        values.write_text(
            '{\n'
            '  // 普通注释\n'
            '  "url": ' + json.dumps("https://example.com/a//b", ensure_ascii=False) + ',\n'
            '  /* UTF-8 block 注释\n'
            '     第二行 */\n'
            '  "escaped": ' + json.dumps(escaped, ensure_ascii=False) + ' // EOF 注释\n'
            '}\n',
            encoding="utf-8",
        )
        result = self.shell('json_query "$1" -r ".url"', str(values))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "https://example.com/a//b")
        result = self.shell('json_query "$1" -r ".escaped"', str(values))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.rstrip("\n"), escaped)

        password = "// /* */"
        tail = "ends-with-backslash\\"
        quoted = 'say "ok"'
        lexical = self.root / "password-lexical.jsonc"
        lexical.write_text(
            "{\n"
            '  "password": ' + json.dumps(password) + ",\n"
            '  "tail": ' + json.dumps(tail) + ",\n"
            '  "quoted": ' + json.dumps(quoted) + "\n"
            "}\n",
            encoding="utf-8",
        )
        for key, expected in (("password", password), ("tail", tail), ("quoted", quoted)):
            with self.subTest(key=key):
                result = self.shell('json_query "$1" -r ".' + key + '"', str(lexical))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.rstrip("\n"), expected)

        for name, content in {
            "unclosed-block.jsonc": '{"a": 1 /* 未闭合\n',
            "syntax-error.jsonc": '{"a": }\n',
            "trailing-comma.jsonc": '{"a": 1,}\n',
        }.items():
            path = self.root / name
            path.write_text(content, encoding="utf-8")
            with self.subTest(name=name):
                result = self.shell('json_query "$1" -r ".a"', str(path))
                self.assertNotEqual(result.returncode, 0)

        before = self.config.read_bytes()
        invalid = self.root / "invalid-commented.jsonc"
        invalid.write_text('{\n  // comment\n  "inbounds": [}\n', encoding="utf-8")
        result = self.shell('commit_config "$1"', str(invalid))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))

    def test_unclosed_block_after_valid_json_is_not_masked_by_jq(self) -> None:
        path = self.root / "valid-then-unclosed.jsonc"
        path.write_text('{"a": 1} /* 未闭合', encoding="utf-8")
        result = self.shell('json_query "$1" -r ".a"', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_comment_separator_cannot_join_two_json_numbers(self) -> None:
        path = self.root / "number-comment-separator.jsonc"
        path.write_text('{"a":1/*x*/2}', encoding="utf-8")
        result = self.shell('json_query "$1" -r ".a"', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(
        fixture.REAL_CORE and Path(fixture.REAL_CORE).is_file(),
        "SB_TEST_CORE is required for the real-core JSONC check",
    )
    def test_real_core_checks_original_commented_config_without_mutating_source(self) -> None:
        raw, _ = self._write_commented_config()
        official = subprocess.run(
            [fixture.REAL_CORE, "check", "-D", str(self.state_dir), "-c", str(self.config)],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(official.returncode, 0, official.stdout + official.stderr)
        result = self.shell('CORE_BIN="$1"; validate_config "$2"', fixture.REAL_CORE, str(self.config))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), raw.encode("utf-8"))
        self.assertEqual(list(self.state_dir.glob(".check.*")), [])


if __name__ == "__main__":
    unittest.main()
