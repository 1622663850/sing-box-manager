#!/usr/bin/env python3
"""Focused coverage for day-to-day management operations.

The larger protocol suite lives in ``test_manager.py``.  These tests exercise
the management transaction boundaries and output files without repeating the
protocol matrix.
"""

from __future__ import annotations

import json
import base64
from pathlib import Path
import shutil
import subprocess
import time
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class ManagementTest(unittest.TestCase):
    """Reuse the isolated fixture, but do not inherit the protocol tests."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def calls(self, log: Path) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, log)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def inbound(self, tag: str = "test-ws", port: int = 18443) -> dict:
        return fixture.ManagerTest.inbound(self, tag, port)

    def _write_nodes(self, *, second: bool = True) -> tuple[dict, dict | None]:
        first = self.inbound("first", 18443)
        nodes = [first]
        other = self.inbound("second", 18444) if second else None
        if other is not None:
            nodes.append(other)
        config = dict(self.original)
        config["inbounds"] = nodes
        fixture.dump(self.config, config)
        for node, server in ((first, "first.example"), (other, "second.example")):
            if node is None:
                continue
            metadata = self.state_dir / "clients" / f"{node['tag']}.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            fixture.dump(metadata, {"server": server, "public_key": "", "inbound": node})
            metadata.chmod(0o600)
        default = self.state_dir / "default-server"
        default.write_text("global.example\n", encoding="utf-8")
        default.chmod(0o600)
        return first, other

    def _backup_path(self) -> Path:
        result = self.shell("create_backup", check=True)
        paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip().startswith("/")]
        self.assertEqual(len(paths), 1, result.stdout + result.stderr)
        self.assertTrue(paths[0].is_file())
        return paths[0]

    def test_create_backup_is_complete_private_and_recoverable(self) -> None:
        first, second = self._write_nodes()
        backup = self._backup_path()
        value = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(value["format"], "sing-box-manager-backup-v1")
        self.assertEqual(value["config"]["inbounds"], [first, second])
        self.assertEqual(set(value["clients"]), {"first", "second"})
        self.assertEqual(value["default_server"], "global.example")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("password", json.dumps(value))

    def test_restore_backup_replaces_config_metadata_and_default_atomically(self) -> None:
        self._write_nodes()
        backup = self._backup_path()
        fixture.dump(self.config, self.original)
        for path in (self.state_dir / "clients" / "first.json", self.state_dir / "clients" / "second.json"):
            path.unlink()
        (self.state_dir / "default-server").unlink()
        self.state(active=True)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(backup))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.config.read_text())["inbounds"][0]["tag"], "first")
        self.assertTrue((self.state_dir / "clients" / "second.json").is_file())
        self.assertEqual((self.state_dir / "default-server").read_text(), "global.example\n")
        self.assertTrue(self.state()["active"])

    def test_restore_restart_failure_restores_all_previous_bytes(self) -> None:
        self._write_nodes()
        backup = self._backup_path()
        before_config = self.config.read_bytes()
        before_clients = {
            path.name: path.read_bytes()
            for path in (self.state_dir / "clients").glob("*.json")
        }
        before_default = (self.state_dir / "default-server").read_bytes()
        changed = self.inbound("changed", 18445)
        fixture.dump(self.config, {**self.original, "inbounds": [changed]})
        for path in (self.state_dir / "clients").glob("*.json"):
            path.unlink()
        fixture.dump(self.state_dir / "clients" / "changed.json",
                     {"server": "changed.example", "public_key": "", "inbound": changed})
        (self.state_dir / "default-server").write_text("changed.example\n", encoding="utf-8")
        changed_config = self.config.read_bytes()
        changed_clients = {path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}
        changed_default = (self.state_dir / "default-server").read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(backup))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), changed_config)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}, changed_clients)
        self.assertEqual((self.state_dir / "default-server").read_bytes(), changed_default)
        self.assertNotEqual(before_config, changed_config)
        self.assertNotEqual(before_clients, changed_clients)
        self.assertNotEqual(before_default, changed_default)
        self.assertTrue(self.state()["active"])

    def test_restore_rejects_invalid_format_and_path_traversal_without_writes(self) -> None:
        self._write_nodes()
        before = self.config.read_bytes()
        bad = self.root / "bad-backup.json"
        fixture.dump(bad, {"format": "sing-box-manager-backup-v1", "config": self.original,
                   "clients": {"../escape": {"server": "x"}}, "default_server": None})
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.root / "escape.json").exists())

    def test_restore_rejects_symlink_bundle_and_newline_metadata_key(self) -> None:
        self._write_nodes(second=False)
        backup = self._backup_path()
        link = self.root / "bundle-link.json"
        link.symlink_to(backup)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(link))
        self.assertNotEqual(result.returncode, 0)

        value = json.loads(backup.read_text(encoding="utf-8"))
        value["clients"] = {"bad\nkey": value["clients"]["first"]}
        malformed = self.root / "newline-key.json"
        fixture.dump(malformed, value)
        result = self.shell('confirm() { return 0; }; restore_backup "$1"', str(malformed))
        self.assertNotEqual(result.returncode, 0)

    def test_backup_rejects_symlinked_metadata(self) -> None:
        self._write_nodes(second=False)
        target = self.root / "protected.json"
        target.write_text("protected\n", encoding="utf-8")
        metadata = self.state_dir / "clients" / "first.json"
        metadata.unlink()
        metadata.symlink_to(target)
        result = self.shell("create_backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "protected\n")

    def test_default_server_write_failure_preserves_config_metadata_and_address(self) -> None:
        self._write_nodes()
        before_config = self.config.read_bytes()
        before_clients = {path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}
        default = self.state_dir / "default-server"
        before_default = default.read_bytes()
        result = self.shell('atomic_copy() { return 1; }; set_default_server new.example')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}, before_clients)
        self.assertEqual(default.read_bytes(), before_default)

    def test_apply_bundle_default_write_failure_restores_replaced_clients_and_state(self) -> None:
        self._write_nodes()
        backup = self._backup_path()
        value = json.loads(backup.read_text(encoding="utf-8"))
        value["config"]["inbounds"][0]["listen_port"] = 18445
        value["clients"]["first"]["inbound"]["listen_port"] = 18445
        value["default_server"] = "new.example"
        bundle = self.root / "changed-bundle.json"
        fixture.dump(bundle, value)
        before_config = self.config.read_bytes()
        before_clients = {path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}
        before_default = (self.state_dir / "default-server").read_bytes()
        self.state(active=True)
        marker = self.root / "fail-once"
        body = r'''
real_atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    if install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"; then return 0; fi
    rm -f -- "$tmp"
    return 1
}
atomic_copy() {
    if [[ $2 == "$DEFAULT_SERVER_FILE" && ! -e "$FAIL_ONCE" ]]; then
        : >"$FAIL_ONCE"
        return 1
    fi
    real_atomic_copy "$@"
}
apply_bundle "$1"
'''
        self.env["FAIL_ONCE"] = str(marker)
        result = self.shell(body, str(bundle))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}, before_clients)
        self.assertEqual((self.state_dir / "default-server").read_bytes(), before_default)
        self.assertTrue(self.state()["active"])

    def test_apply_bundle_term_after_clients_rename_restores_directory_without_nesting(self) -> None:
        self._write_nodes()
        backup = self._backup_path()
        value = json.loads(backup.read_text(encoding="utf-8"))
        value["config"]["inbounds"][0]["listen_port"] = 18445
        value["clients"]["first"]["inbound"]["listen_port"] = 18445
        value["default_server"] = "new.example"
        bundle = self.root / "rename-signal-bundle.json"
        fixture.dump(bundle, value)
        before_config = self.config.read_bytes()
        before_clients = {path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}
        before_default = (self.state_dir / "default-server").read_bytes()
        marker = self.root / "mv-signal-once"
        self.env["MV_ONCE"] = str(marker)
        body = r'''
mv() {
    command mv "$@" || return
    [[ ${1:-} != -- ]] || shift
    if [[ ${2:-} == "$CLIENT_DIR" && ${1:-} == */restore.*/clients && ! -e "$MV_ONCE" ]]; then
        : >"$MV_ONCE"
        kill -TERM "$BASHPID"
    fi
}
apply_bundle "$1"
'''
        self.state(active=True)
        result = self.shell(body, str(bundle))
        self.assertTrue(marker.exists(), "rename signal fault was not injected")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}, before_clients)
        self.assertEqual((self.state_dir / "default-server").read_bytes(), before_default)
        clients = self.state_dir / "clients"
        self.assertFalse((clients / "clients-before").exists())
        self.assertFalse((clients / "clients").exists())
        self.assertTrue(self.state()["active"])

    def test_delete_last_node_leaves_valid_empty_configuration(self) -> None:
        self._write_nodes(second=False)
        result = self.shell('confirm() { return 0; }; delete_inbound first')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.config.read_text())["inbounds"], [])
        self.assertEqual(list((self.state_dir / "clients").glob("*.json")), [])

    def test_add_managed_inbound_restart_failure_restores_old_data(self) -> None:
        self._write_nodes(second=False)
        inbound = self.root / "new.json"
        node = self.inbound("new", 18445)
        fixture.dump(inbound, node)
        before_config = self.config.read_bytes()
        before_clients = {path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}
        before_default = (self.state_dir / "default-server").read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell('add_managed_inbound "$1" "$2" ""', str(inbound), "new.example")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state_dir / "clients").glob("*.json")}, before_clients)
        self.assertEqual((self.state_dir / "default-server").read_bytes(), before_default)
        self.assertFalse((self.state_dir / "clients" / "new.json").exists())
        self.assertTrue(self.state()["active"])

    def test_delete_removes_node_metadata_and_keeps_other_nodes(self) -> None:
        self._write_nodes()
        self.state(active=True)
        result = self.shell('confirm() { return 0; }; delete_inbound "$1"', "first")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads(self.config.read_text())
        self.assertEqual([item["tag"] for item in config["inbounds"]], ["second"])
        self.assertFalse((self.state_dir / "clients" / "first.json").exists())
        self.assertTrue((self.state_dir / "clients" / "second.json").is_file())
        self.assertTrue(self.state()["active"])

    def test_delete_cancel_and_referenced_node_leave_everything_unchanged(self) -> None:
        first, _ = self._write_nodes(second=False)
        config = json.loads(self.config.read_text())
        config["route"] = {"rules": [{"inbound": [first["tag"]]}], "final": "direct"}
        fixture.dump(self.config, config)
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 0; }; delete_inbound "$1"', "first")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        config.pop("route")
        fixture.dump(self.config, config)
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 1; }; delete_inbound "$1"', "first")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_export_outputs_all_requested_formats_without_overwriting(self) -> None:
        self._write_nodes()
        for fmt, suffix in (("links", "txt"), ("base64", "txt"), ("json", "json")):
            path = self.root / f"clients-{fmt}.{suffix}"
            result = self.shell('export_clients "$1" "$2"', fmt, str(path), check=True)
            self.assertTrue(path.is_file(), result.stdout + result.stderr)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text(encoding="utf-8")
            if fmt == "base64":
                decoded = base64.b64decode(text).decode("utf-8")
                self.assertIn("first", decoded)
                self.assertIn("second", decoded)
            else:
                self.assertIn("first", text)
                self.assertIn("second", text)
            if fmt == "json":
                self.assertEqual(len(json.loads(text)["outbounds"]), 2)
        existing = self.root / "existing.txt"
        existing.write_text("keep\n", encoding="utf-8")
        result = self.shell('export_clients links "$1"', str(existing))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(), "keep\n")

    def test_export_fails_as_a_whole_when_one_node_cannot_be_exported(self) -> None:
        self._write_nodes()
        metadata = self.state_dir / "clients" / "second.json"
        fixture.dump(metadata, {"server": "", "public_key": "", "inbound": self.inbound("second", 18444)})
        (self.state_dir / "default-server").unlink()
        output = self.root / "partial.txt"
        result = self.shell('export_clients links "$1"', str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_diagnose_is_read_only_and_does_not_print_credentials(self) -> None:
        self._write_nodes()
        config = json.loads(self.config.read_text())
        config["inbounds"][0]["users"][0]["password"] = "<REDACTED_SECRET>"
        fixture.dump(self.config, config)
        result = self.shell("diagnose")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("<REDACTED_SECRET>", result.stdout + result.stderr)
        self.assertFalse(any(action in call for call in self.calls(self.system_log) for action in ("start", "stop", "restart")))

    def test_valid_host_accepts_real_ipv4_ipv6_and_dns_but_rejects_malformed_values(self) -> None:
        for value in ("example.com", "a-b.example", "192.0.2.1", "2001:db8::1", "::ffff:192.0.2.1"):
            self.shell('valid_host "$1"', value, check=True)
        long_label = "a" * 64 + ".example.com"
        for value in ("::::", "1::2::3", ":1::2", "1::2:", "999.999.999.999", "a.-b.example", "-bad.example", "example..com", long_label):
            with self.subTest(value=value):
                self.assertNotEqual(self.shell('valid_host "$1"', value).returncode, 0)

    def test_add_managed_inbound_rolls_back_when_metadata_cannot_be_written(self) -> None:
        inbound = self.root / "new.json"
        node = self.inbound("new", 18445)
        fixture.dump(inbound, node)
        blocked = self.state_dir / "clients" / "new.json"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.mkdir()
        before = self.config.read_bytes()
        result = self.shell('add_managed_inbound "$1" "$2" ""', str(inbound), "new.example")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(blocked.is_dir())

    def test_commit_config_term_during_restart_restores_original_configuration(self) -> None:
        candidate = self.root / "candidate.json"
        changed = dict(self.original)
        changed["log"] = {"level": "debug", "timestamp": True}
        fixture.dump(candidate, changed)
        term_state = self.root / "term-restart-state"
        term_systemctl = self.bin / "systemctl"
        term_systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "set -u\n"
            "if [[ ${1:-} == is-active ]]; then exit 0; fi\n"
            "if [[ ${1:-} == restart ]]; then\n"
            "  if [[ ! -e $TERM_STATE ]]; then : >$TERM_STATE; kill -TERM $PPID; exit 143; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ ${1:-} == stop ]]; then exit 0; fi\n"
            "if [[ ${1:-} == show ]]; then [[ ${2:-} == --value ]] && exit 0; printf '\\n'; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        term_systemctl.chmod(0o755)
        self.env["TERM_STATE"] = str(term_state)
        self.state(active=True)
        result = self.shell('commit_config "$1"', str(candidate))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_bytes)
        self.assertTrue(self.state()["active"])
        self.assertFalse(list(self.config.parent.glob(".candidate.*")))

    def test_cli_management_commands_route_and_reject_bad_arguments(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }
require_systemd() { return 0; }
safe_paths() { return 0; }
is_managed() { return 0; }
with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
delete_inbound() { printf 'DELETE:%s\n' "$*"; }
create_backup() { printf 'BACKUP\n'; }
list_backups() { printf 'BACKUPS\n'; }
restore_backup() { printf 'RESTORE:%s\n' "$*"; }
export_clients() { printf 'EXPORT:%s\n' "$*"; }
diagnose() { printf 'DIAGNOSE\n'; }
main delete alpha
main backup
main backups
main restore /tmp/bundle.json
main export json /tmp/out.json
main diagnose
main export links >/dev/null 2>&1 || printf 'INVALID\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LOCK:delete_inbound alpha", result.stdout)
        self.assertIn("LOCK:create_backup", result.stdout)
        self.assertIn("BACKUPS", result.stdout)
        self.assertIn("LOCK:restore_backup /tmp/bundle.json", result.stdout)
        self.assertIn("LOCK:export_clients json /tmp/out.json", result.stdout)
        self.assertIn("LOCK:diagnose", result.stdout)
        self.assertIn("INVALID", result.stdout)

    def test_management_menu_routes_all_choices_through_expected_lock(self) -> None:
        body = r'''
prompt() {
    case "$1" in
        choice) printf -v choice '%s' "$MG_CHOICE" ;;
        format) printf -v format '%s' "$MG_FORMAT" ;;
        file) printf -v file '%s' "$MG_FILE" ;;
    esac
}
with_lock() { printf 'LOCK:%s\n' "$*"; "$@"; }
create_backup() { printf 'BACKUP\n'; }
list_backups() { printf 'BACKUPS\n'; }
restore_backup() { printf 'RESTORE:%s\n' "$*"; }
export_clients() { printf 'EXPORT:%s\n' "$*"; }
diagnose() { printf 'DIAGNOSE\n'; }
MG_CHOICE=1 management_menu
MG_CHOICE=2 management_menu
MG_CHOICE=3 MG_FILE=/tmp/bundle.json management_menu
MG_CHOICE=4 MG_FORMAT=base64 MG_FILE=/tmp/out.txt management_menu
MG_CHOICE=5 management_menu
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LOCK:create_backup", result.stdout)
        self.assertIn("BACKUPS", result.stdout)
        self.assertIn("LOCK:restore_backup /tmp/bundle.json", result.stdout)
        self.assertIn("LOCK:export_clients base64 /tmp/out.txt", result.stdout)
        self.assertIn("LOCK:diagnose", result.stdout)

    def test_with_lock_rejects_second_mutation_until_first_releases(self) -> None:
        first = subprocess.Popen(
            ["bash", "-c", 'source "$1"; shift; with_lock sleep 2', "lock-test", str(fixture.SCRIPT)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        )
        time.sleep(0.25)
        second = self.shell("with_lock true")
        self.assertNotEqual(second.returncode, 0)
        first.wait(timeout=5)
        self.assertEqual(self.shell("with_lock true").returncode, 0)


if __name__ == "__main__":
    unittest.main()
