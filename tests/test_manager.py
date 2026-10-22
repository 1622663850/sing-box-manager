#!/usr/bin/env python3
"""Offline regression tests; never installs packages or touches host services.

Run with: python3 -m unittest discover -s tests -v
Set SB_TEST_CORE to a trusted sing-box executable to additionally check generated
configurations with the real core. Everything runs beneath a TemporaryDirectory.
"""

from __future__ import annotations

import json
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "sing-box-manager.sh"
REAL_CORE = os.environ.get("SB_TEST_CORE", "")


MOCK_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["SB_MOCK_STATE_FILE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(os.environ["SB_MOCK_SYSTEMCTL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
actions = {"is-active", "is-enabled", "start", "stop", "restart", "enable",
           "disable", "daemon-reload", "reset-failed", "status", "show"}
action = next((arg for arg in args if arg in actions), "")
quiet = "--quiet" in args or "-q" in args
if action == "is-active":
    if not quiet: print("active" if state["active"] else "inactive")
    sys.exit(0 if state["active"] else 3)
if action == "is-enabled":
    if not quiet: print("enabled" if state["enabled"] else "disabled")
    sys.exit(0 if state["enabled"] else 1)
if action == "restart" and state.get("restart_failures", 0):
    state["restart_failures"] -= 1
    state["active"] = False
    state_path.write_text(json.dumps(state))
    print("simulated service restart failure", file=sys.stderr)
    sys.exit(1)
if action == "daemon-reload" and state.get("reload_failures", 0):
    state["reload_failures"] -= 1
    state_path.write_text(json.dumps(state))
    print("simulated systemd reload failure", file=sys.stderr)
    sys.exit(1)
if action in {"start", "restart"}: state["active"] = True
if action == "stop": state["active"] = False
if action == "enable":
    state["enabled"] = True
    if "--now" in args: state["active"] = True
if action == "disable":
    state["enabled"] = False
    if "--now" in args: state["active"] = False
if action == "show":
    if "--value" in args: print("")
    else: print("FragmentPath=")
state_path.write_text(json.dumps(state))
sys.exit(0)
'''


MOCK_CORE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["SB_MOCK_CORE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args and args[0] == "check":
    try:
        position = args.index("-c") if "-c" in args else args.index("--config")
        with open(args[position + 1], encoding="utf-8") as file:
            content = json.load(file)
        assert isinstance(content, dict)
        assert not content.get("_test_invalid")
        assert isinstance(content.get("inbounds", []), list)
    except (AssertionError, OSError, ValueError, IndexError) as error:
        print("mock core: invalid config", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if args == ["version"]:
    print("sing-box version 1.14.3")
    sys.exit(0)
print(json.dumps(args, ensure_ascii=False))
sys.exit(23 if args and args[0] == "return-23" else 0)
'''


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class ManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sing-box-manager-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "mock-bin"
        self.bin.mkdir()
        self.core = self.root / "usr/local/lib/sing-box-manager/sing-box"
        self.core.parent.mkdir(parents=True)
        self.config = self.root / "etc/sing-box/config.json"
        self.config.parent.mkdir(parents=True)
        self.state_dir = self.root / "var/lib/sing-box-manager"
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "backups").mkdir()
        self.unit = self.root / "etc/systemd/system/sing-box.service"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("# sing-box-manager\n[Service]\nType=simple\n", encoding="utf-8")
        self.mock_state = self.root / "service-state.json"
        self.system_log = self.root / "systemctl.jsonl"
        self.core_log = self.root / "core.jsonl"
        dump(self.mock_state, {"active": False, "enabled": False, "restart_failures": 0})
        self.original = {
            "log": {"level": "info", "timestamp": True},
            "inbounds": [],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        }
        dump(self.config, self.original)
        self.original_bytes = self.config.read_bytes()
        for path, content in ((self.bin / "systemctl", MOCK_SYSTEMCTL), (self.core, MOCK_CORE)):
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        mock_ss = self.bin / "ss"
        mock_ss.write_text('#!/usr/bin/env bash\n[[ ${SB_MOCK_PORT_BUSY:-0} == 1 ]] && printf "LISTEN\\n"\nexit 0\n', encoding="utf-8")
        mock_ss.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "SB_ROOT_PREFIX": str(self.root),
            "SB_MOCK_STATE_FILE": str(self.mock_state),
            "SB_MOCK_SYSTEMCTL_LOG": str(self.system_log),
            "SB_MOCK_CORE_LOG": str(self.core_log),
            "LC_ALL": "C.UTF-8",
        })

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body, "test-manager", str(SCRIPT), *args],
            env=self.env, text=True, capture_output=True, timeout=20,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self, log: Path) -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def state(self, **updates: object) -> dict:
        state = json.loads(self.mock_state.read_text())
        if updates:
            state.update(updates)
            dump(self.mock_state, state)
        return state

    def changed_candidate(self) -> Path:
        candidate = self.root / "candidate with spaces.json"
        value = dict(self.original)
        value["log"] = {"level": "debug", "timestamp": True}
        dump(candidate, value)
        return candidate

    def inbound(self, tag: str = "test-ws", port: int = 18443) -> dict:
        return {
            "type": "vless", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
            "users": [{"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737"}],
            "transport": {"type": "ws", "path": "/vless"},
        }

    def test_source_only_defines_functions_without_running_menu_or_services(self) -> None:
        result = self.shell('printf "%s\\n" "$CORE_BIN" "$CONFIG_FILE"', check=True)
        self.assertEqual(result.stdout.splitlines(), [str(self.core), str(self.config)])
        self.assertFalse(self.system_log.exists())
        self.assertEqual(self.config.read_bytes(), self.original_bytes)

    def test_default_config_is_closed_and_has_direct_outbound(self) -> None:
        result = self.shell("base_config", check=True)
        config = json.loads(result.stdout)
        self.assertEqual(config.get("inbounds"), [])
        self.assertTrue(any(item.get("type") == "direct" for item in config.get("outbounds", [])))

    def test_port_validation_rejects_invalid_and_shell_metacharacters(self) -> None:
        for port in ("1", "443", "65535"):
            with self.subTest(port=port):
                self.shell('valid_port "$1"', port, check=True)
        for port in ("", "0", "65536", "-1", "1.2", " 443", "443 ", "1+1", "$(touch marker)", "18446744073709552059", "99999999999999999999999999"):
            with self.subTest(port=port):
                self.assertNotEqual(self.shell('valid_port "$1"', port).returncode, 0)

    def test_tag_validation_blocks_paths_and_control_characters(self) -> None:
        for tag in ("node1", "vless-ws", "my_node"):
            self.shell('valid_tag "$1"', tag, check=True)
        for tag in ("", "../node", "/etc/passwd", "node\nline", "node;exit"):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.shell('valid_tag "$1"', tag).returncode, 0)

    def test_invalid_config_never_changes_disk_or_restarts_service(self) -> None:
        self.state(active=True)
        candidate = self.root / "bad.json"
        dump(candidate, {"_test_invalid": True, "inbounds": []})
        result = self.shell('commit_config "$1"', str(candidate))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_bytes)
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))
        self.assertTrue(self.state()["active"])

    def test_valid_config_saved_without_starting_stopped_service(self) -> None:
        candidate = self.changed_candidate()
        self.shell('commit_config "$1"', str(candidate), check=True)
        self.assertEqual(json.loads(self.config.read_text()), json.loads(candidate.read_text()))
        calls = self.calls(self.system_log)
        self.assertFalse(any(any(action in call for action in ("start", "restart")) for call in calls))
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_active_service_restarts_after_valid_change(self) -> None:
        self.state(active=True)
        candidate = self.changed_candidate()
        self.shell('commit_config "$1"', str(candidate), check=True)
        self.assertEqual(json.loads(self.config.read_text()), json.loads(candidate.read_text()))
        self.assertTrue(any("restart" in call for call in self.calls(self.system_log)))
        self.assertTrue(self.state()["active"])

    def test_restart_failure_restores_original_config_and_service(self) -> None:
        self.state(active=True, restart_failures=1)
        candidate = self.changed_candidate()
        result = self.shell('commit_config "$1"', str(candidate))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_bytes)
        restarts = [call for call in self.calls(self.system_log) if "restart" in call]
        self.assertGreaterEqual(len(restarts), 2)
        self.assertTrue(self.state()["active"])

    def test_add_single_inbound_preserves_existing_config(self) -> None:
        inbound_path = self.root / "one inbound.json"
        inbound = self.inbound()
        dump(inbound_path, inbound)
        self.shell('add_inbound_file "$1"', str(inbound_path), check=True)
        config = json.loads(self.config.read_text())
        self.assertEqual(config["inbounds"], [inbound])
        self.assertEqual(config["outbounds"], self.original["outbounds"])
        self.assertEqual(config["log"], self.original["log"])

    def test_duplicate_tag_or_port_keeps_original_config(self) -> None:
        existing = dict(self.original)
        existing["inbounds"] = [self.inbound()]
        dump(self.config, existing)
        before = self.config.read_bytes()
        for inbound in (self.inbound(tag="test-ws", port=18444), self.inbound(tag="other", port=18443)):
            with self.subTest(inbound=inbound):
                path = self.root / "duplicate.json"
                dump(path, inbound)
                result = self.shell('add_inbound_file "$1"', str(path))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.config.read_bytes(), before)

    def test_occupied_port_never_modifies_configuration(self) -> None:
        self.env["SB_MOCK_PORT_BUSY"] = "1"
        inbound_path = self.root / "occupied.json"
        dump(inbound_path, self.inbound())
        result = self.shell('add_inbound_file "$1"', str(inbound_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original_bytes)

    def test_symlink_config_is_rejected_without_writing_target(self) -> None:
        protected = self.root / "protected.txt"
        protected.write_text("leave this file alone\n", encoding="utf-8")
        self.config.unlink()
        self.config.symlink_to(protected)
        result = self.shell('commit_config "$1"', str(self.changed_candidate()))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(protected.read_text(), "leave this file alone\n")
        self.assertTrue(self.config.is_symlink())

    def download_fixture(self, digest: str | None = "valid") -> Path:
        archive = self.root / "fixture.tar.gz"
        executable = b'#!/usr/bin/env bash\nprintf "sing-box version 1.14.3\\n"\n'
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("sing-box-1.14.3-linux-amd64/sing-box")
            member.size = len(executable)
            member.mode = 0o755
            tar.addfile(member, io.BytesIO(executable))
            # The downloader must extract only the expected member to stdout.
            unrelated = tarfile.TarInfo("../archive-path-escape.txt")
            unrelated.size = 4
            tar.addfile(unrelated, io.BytesIO(b"oops"))
        if digest == "valid":
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        release = self.root / "release.json"
        dump(release, {
            "tag_name": "v1.14.3", "draft": False, "prerelease": False,
            "assets": [{
                "name": "sing-box-1.14.3-linux-amd64.tar.gz",
                "browser_download_url": "https://github.com/SagerNet/sing-box/releases/download/v1.14.3/sing-box-1.14.3-linux-amd64.tar.gz",
                "digest": digest,
            }],
        })
        self.env["SB_RELEASE_FIXTURE"] = str(release)
        self.env["SB_ARCHIVE_FIXTURE"] = str(archive)
        destination = self.root / "download"
        destination.mkdir(exist_ok=True)
        return destination

    def download_shell(self, destination: Path) -> subprocess.CompletedProcess[str]:
        return self.shell(
            'detect_arch() { printf "amd64\\n"; }; '
            'download_https() { case "$1" in */releases/latest) cp -- "$SB_RELEASE_FIXTURE" "$2";; '
            '*) cp -- "$SB_ARCHIVE_FIXTURE" "$2";; esac; }; download_core "$1"',
            str(destination),
        )

    def test_download_checks_digest_and_extracts_only_expected_binary(self) -> None:
        destination = self.download_fixture()
        result = self.download_shell(destination)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((destination / "sing-box").is_file())
        self.assertFalse((self.root / "archive-path-escape.txt").exists())

    def test_download_rejects_missing_digest_and_checksum_mismatch(self) -> None:
        for digest in (None, "sha256:" + "0" * 64):
            with self.subTest(digest=digest):
                destination = self.download_fixture(digest)
                result = self.download_shell(destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((destination / "sing-box").exists())

    def test_full_import_uses_same_validation_and_preserves_on_failure(self) -> None:
        bad = self.root / "invalid import.json"
        bad.write_text('{"inbounds": [', encoding="utf-8")
        result = self.shell('import_config "$1"', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), self.original_bytes)

    def test_enable_does_not_start_stopped_service(self) -> None:
        self.shell("service_action enable", check=True)
        self.assertTrue(self.state()["enabled"])
        self.assertFalse(self.state()["active"])

    def test_disable_stops_and_disables_service(self) -> None:
        self.state(active=True, enabled=True)
        self.shell("service_action disable", check=True)
        self.assertFalse(self.state()["enabled"])
        self.assertFalse(self.state()["active"])

    def test_stop_and_disable_work_even_when_config_and_core_are_missing(self) -> None:
        self.config.unlink()
        self.core.unlink()
        for action in ("stop", "disable"):
            with self.subTest(action=action):
                self.state(active=True, enabled=True)
                self.shell('service_action "$1"', action, check=True)
                self.assertFalse(self.state()["active"])
                self.assertEqual(self.state()["enabled"], action == "stop")

    def test_logs_remain_available_when_config_and_core_are_missing(self) -> None:
        self.config.unlink()
        self.core.unlink()
        result = self.shell('journalctl() { printf "saved service logs\\n"; }; show_logs', check=True)
        self.assertIn("saved service logs", result.stdout)

    def test_managed_uninstall_works_when_config_and_core_are_missing(self) -> None:
        self.config.unlink()
        self.core.unlink()
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.state(active=True, enabled=True)
        self.shell('confirm() { return 0; }; uninstall_core', check=True)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        self.assertTrue(marker.exists())

    def test_unmanaged_main_start_refuses_without_creating_state_directory(self) -> None:
        shutil.rmtree(self.state_dir)
        result = self.shell(
            'SB_ROOT_PREFIX=""; require_root() { return 0; }; '
            'require_systemd() { return 0; }; main start'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(any("start" in call for call in self.calls(self.system_log)))

    def test_first_menu_initializes_panel_before_showing_menu(self) -> None:
        result = self.shell(
            'SB_ROOT_PREFIX=""; require_root() { return 0; }; require_systemd() { return 0; }; '
            'setup_panel() { printf "setup-called\\n"; printf "sing-box-manager-v1\\n" > "$MANAGED_MARKER"; }; '
            'menu() { printf "menu-called\\n"; }; main menu',
            check=True,
        )
        lines = result.stdout.splitlines()
        self.assertIn("setup-called", lines)
        self.assertIn("menu-called", lines)
        self.assertLess(lines.index("setup-called"), lines.index("menu-called"))

    def upgrade_fixture(self) -> Path:
        upgrade = self.root / "upgrade-core"
        upgrade.write_text(MOCK_CORE + "\n# distinct upgraded core\n", encoding="utf-8")
        upgrade.chmod(0o755)
        self.env["SB_UPGRADE_CORE"] = str(upgrade)
        return upgrade

    def upgrade_shell(self, extra: str = "") -> subprocess.CompletedProcess[str]:
        return self.shell(
            'download_core() { install -m 755 "$SB_UPGRADE_CORE" "$1/sing-box"; }; '
            + extra + ' install_core_locked'
        )

    def test_upgrade_restores_old_core_and_unit_when_new_unit_cannot_be_written(self) -> None:
        self.upgrade_fixture()
        old_core, old_unit = self.core.read_bytes(), self.unit.read_bytes()
        self.state(active=True)
        result = self.upgrade_shell('write_unit() { return 1; };')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.core.read_bytes(), old_core)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertTrue(self.state()["active"])

    def test_upgrade_restores_old_core_and_unit_when_reload_or_restart_fails(self) -> None:
        self.upgrade_fixture()
        old_core, old_unit = self.core.read_bytes(), self.unit.read_bytes()
        for failure in ("reload_failures", "restart_failures"):
            with self.subTest(failure=failure):
                self.state(active=True, reload_failures=0, restart_failures=0)
                self.state(**{failure: 1})
                result = self.upgrade_shell()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.core.read_bytes(), old_core)
                self.assertEqual(self.unit.read_bytes(), old_unit)
                self.assertTrue(self.state()["active"])

    def test_failed_initial_install_leaves_no_core_or_unit(self) -> None:
        self.upgrade_fixture()
        self.core.unlink()
        self.unit.unlink()
        self.config.unlink()
        self.state(active=False, reload_failures=1)
        result = self.upgrade_shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.core.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.state()["active"])

    def test_start_rejects_invalid_configuration_before_systemctl(self) -> None:
        dump(self.config, {"_test_invalid": True})
        result = self.shell("service_action start")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("start" in call for call in self.calls(self.system_log)))

    def test_core_passthrough_preserves_argument_boundaries(self) -> None:
        args = ["show-args", "with spaces", "literal;$HOME", 'quote"back\\slash', ""]
        result = self.shell('core_passthrough "$@"', *args, check=True)
        self.assertEqual(json.loads(result.stdout), args)

    def test_core_passthrough_preserves_exit_status(self) -> None:
        result = self.shell('core_passthrough "$@"', "return-23")
        self.assertEqual(result.returncode, 23)

    def test_generated_ws_configuration_escapes_path(self) -> None:
        path = '/vless/quote"back\\slash?name=a&part=b'
        result = self.shell(
            'generate_inbound "$@"', "vless-ws", "ws-node", "18443",
            "b7efdc96-87aa-4910-81b4-374b2eecc737", path, "", "", check=True,
        )
        inbound = json.loads(result.stdout)
        self.assertEqual(inbound["transport"]["path"], path)
        self.assertEqual(inbound["transport"]["type"], "ws")
        self.assertEqual(inbound["listen_port"], 18443)

    def test_generated_reality_uses_server_side_fields(self) -> None:
        result = self.shell(
            'generate_inbound "$@"', "vless-reality", "reality-node", "18443",
            "b7efdc96-87aa-4910-81b4-374b2eecc737", "www.example.com",
            "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef", check=True,
        )
        inbound = json.loads(result.stdout)
        self.assertTrue(inbound["tls"]["enabled"])
        self.assertEqual(inbound["users"][0]["flow"], "xtls-rprx-vision")
        self.assertEqual(inbound["tls"]["reality"]["short_id"], ["0123456789abcdef"])
        self.assertIn("private_key", inbound["tls"]["reality"])
        self.assertNotIn("public_key", inbound["tls"]["reality"])

    def test_client_export_rejects_unrepresentable_imported_inbounds(self) -> None:
        tls = self.inbound()
        tls["tls"] = {"enabled": True, "certificate_path": "/certificate.pem", "key_path": "/key.pem"}
        grpc = self.inbound()
        grpc["transport"] = {"type": "grpc", "service_name": "example"}
        multiple_users = self.inbound()
        multiple_users["users"].append({"uuid": "0325ba41-4946-4ad5-b6b3-476eb50c37cb"})
        for name, inbound in (("ordinary-tls", tls), ("grpc", grpc), ("multiple-users", multiple_users)):
            with self.subTest(case=name):
                path = self.root / (name + ".json")
                dump(path, inbound)
                result = self.shell('generate_client "$1" "example.com"', str(path))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_client_export_accepts_own_templates_without_losing_tls(self) -> None:
        fields = {
            "vless-tcp": ("b7efdc96-87aa-4910-81b4-374b2eecc737", "", "", ""),
            "vless-ws": ("b7efdc96-87aa-4910-81b4-374b2eecc737", "/vless", "", ""),
            "vless-reality": (
                "b7efdc96-87aa-4910-81b4-374b2eecc737", "www.example.com",
                "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
            ),
            "shadowsocks": ("8JCsPssfgS8tiRwiMlhARg==", "", "", ""),
        }
        for protocol, options in fields.items():
            with self.subTest(protocol=protocol):
                generated = self.shell('generate_inbound "$@"', protocol, "client-test", "18443", *options, check=True)
                inbound = self.root / (protocol + "-client-source.json")
                inbound.write_text(generated.stdout, encoding="utf-8")
                result = self.shell(
                    'generate_client "$1" "example.com" "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"',
                    str(inbound), check=True,
                )
                client = json.loads(result.stdout)
                self.assertEqual(client["server"], "example.com")
                self.assertEqual(client["server_port"], 18443)
                if protocol == "vless-reality":
                    self.assertTrue(client["tls"]["enabled"])
                    self.assertEqual(client["tls"]["reality"]["short_id"], "0123456789abcdef")
                    self.assertNotIn("private_key", client["tls"]["reality"])

    @unittest.skipUnless(REAL_CORE, "set SB_TEST_CORE for real-core validation")
    def test_real_core_accepts_all_generated_inbounds(self) -> None:
        protocols = {
            "vless-tcp": ("b7efdc96-87aa-4910-81b4-374b2eecc737", "", "", ""),
            "vless-ws": ("b7efdc96-87aa-4910-81b4-374b2eecc737", "/vless", "", ""),
            "vless-reality": (
                "b7efdc96-87aa-4910-81b4-374b2eecc737", "www.example.com",
                "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
            ),
            "shadowsocks": ("8JCsPssfgS8tiRwiMlhARg==", "", "", ""),
        }
        for protocol, fields in protocols.items():
            with self.subTest(protocol=protocol):
                result = self.shell('generate_inbound "$@"', protocol, "real-test", "18443", *fields, check=True)
                value = dict(self.original)
                value["inbounds"] = [json.loads(result.stdout)]
                path = self.root / (protocol + ".json")
                dump(path, value)
                checked = subprocess.run(
                    [REAL_CORE, "check", "-c", str(path)], capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)


if __name__ == "__main__":
    unittest.main()
