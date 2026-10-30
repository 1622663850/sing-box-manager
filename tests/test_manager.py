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
import pty
import re
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
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["SB_MOCK_CORE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args and args[0] == "check":
    try:
        position = args.index("-c") if "-c" in args else args.index("--config")
        path = args[position + 1]
        with open(path, encoding="utf-8") as file:
            source = file.read()
        try:
            content = json.loads(source)
        except json.JSONDecodeError:
            # Match comment support of the real core; independent real-core
            # tests also validate the original JSONC bytes directly.
            parsed = subprocess.run(
                ["bash", "-c", 'source "$1"; json_query "$2" .',
                 "mock-core-jsonc", os.environ["SB_MOCK_MANAGER"], path],
                capture_output=True, text=True, timeout=10,
            )
            assert parsed.returncode == 0
            content = json.loads(parsed.stdout)
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
            "SB_MOCK_MANAGER": str(SCRIPT),
            "LC_ALL": "C.UTF-8",
        })

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body, "test-manager", str(SCRIPT), *args],
            env=self.env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=20,
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

    def tls_material(self, common_name: str = "example.com") -> tuple[Path, Path]:
        """Create a short-lived self-signed pair for TLS template tests."""
        if not shutil.which("openssl"):
            self.skipTest("openssl is required for TLS template tests")
        certificate = self.root / "server-cert.pem"
        private_key = self.root / "server-key.pem"
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
                "-subj", f"/CN={common_name}", "-addext", f"subjectAltName=DNS:{common_name}",
            ],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(certificate.is_file())
        self.assertTrue(private_key.is_file())
        return certificate, private_key

    def generate_inbound(
        self,
        protocol: str,
        credential: str,
        extra: str = "",
        private_key: str = "",
        short_id: str = "",
        tls_server_name: str = "",
        certificate: Path | None = None,
        key: Path | None = None,
        tuic_password: str = "",
        obfs_password: str = "",
    ) -> dict:
        """Call the source-only generator with the extended positional seam."""
        args = (
            protocol, "extended-test", "18443", credential, extra, private_key,
            short_id, tls_server_name, str(certificate or ""), str(key or ""),
            tuic_password, obfs_password,
        )
        result = self.shell('generate_inbound "$@"', *args, check=True)
        return json.loads(result.stdout)

    def edit_protocol_inbound(
        self,
        protocol: str,
        tag: str,
        port: int,
        certificate: Path | None = None,
        private_key: Path | None = None,
    ) -> dict:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        if protocol == "vless-reality":
            return self.generate_inbound(
                protocol, uuid, "example.com",
                "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
                "example.com", certificate=certificate, key=private_key,
            ) | {"tag": tag, "listen_port": port}
        if protocol == "vless-grpc-reality":
            return self.generate_inbound(
                protocol, uuid, "EditGrpcReality",
                "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
                "example.com", certificate=certificate, key=private_key,
            ) | {"tag": tag, "listen_port": port}
        if protocol == "shadowsocks":
            return self.generate_inbound(
                protocol, "8JCsPssfgS8tiRwiMlhARg==",
            ) | {"tag": tag, "listen_port": port}
        if protocol == "hysteria2":
            return self.generate_inbound(
                protocol, "edit-hy2-password", tls_server_name="example.com",
                certificate=certificate, key=private_key,
            ) | {"tag": tag, "listen_port": port}
        if protocol == "tuic":
            return self.generate_inbound(
                protocol, uuid, tuic_password="edit-tuic-password",
                tls_server_name="example.com", certificate=certificate, key=private_key,
            ) | {"tag": tag, "listen_port": port}
        if protocol == "anytls":
            return self.generate_inbound(
                protocol, "edit-anytls-password", tls_server_name="example.com",
                certificate=certificate, key=private_key,
            ) | {"tag": tag, "listen_port": port}
        extras = {
            "vless-tcp": "",
            "vless-tcp-tls": "",
            "vless-ws": "/edit-vless",
            "vless-ws-tls": "/edit-vless-tls",
            "vless-grpc-tls": "EditGrpc",
            "vmess-tcp": "",
            "vmess-tcp-tls": "",
            "vmess-ws": "/edit-vmess",
            "vmess-ws-tls": "/edit-vmess-tls",
            "vmess-grpc-tls": "EditVmessGrpc",
        }
        extra = extras[protocol]
        tls = protocol.endswith("-tls")
        return self.generate_inbound(
            protocol, uuid, extra, tls_server_name="example.com" if tls else "",
            certificate=certificate if tls else None, key=private_key if tls else None,
        ) | {"tag": tag, "listen_port": port}

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

    def test_disable_autostart_preserves_running_or_stopped_state_and_files(self) -> None:
        for active in (True, False):
            with self.subTest(active=active):
                self.state(active=active, enabled=True)
                before = (self.config.read_bytes(), self.core.read_bytes(), self.unit.read_bytes())
                self.system_log.unlink(missing_ok=True)
                self.shell("service_action disable-autostart", check=True)
                self.assertFalse(self.state()["enabled"])
                self.assertEqual(self.state()["active"], active)
                self.assertEqual(self.calls(self.system_log), [["disable", "sing-box.service"]])
                self.assertEqual((self.config.read_bytes(), self.core.read_bytes(), self.unit.read_bytes()), before)

    def test_disable_autostart_handles_missing_core_and_propagates_failure(self) -> None:
        self.core.unlink()
        self.config.unlink()
        self.state(active=True, enabled=True)
        self.shell("service_action disable-autostart", check=True)
        self.assertTrue(self.state()["active"])
        self.assertFalse(self.state()["enabled"])
        result = self.shell('systemctl() { return 23; }; service_action disable-autostart')
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("已关闭开机自启", result.stdout)

    def test_autostart_submenu_routes_enable_disable_return_and_invalid(self) -> None:
        body = r'''
prompt() { printf -v "$1" '%s' "$TEST_CHOICE"; }
with_lock() { printf 'LOCK:%s\n' "$*"; }
for TEST_CHOICE in 1 2 0; do autostart_menu || exit; done
TEST_CHOICE=3; autostart_menu >/dev/null 2>&1 || exit 90
printf 'INVALID_REJECTED\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INVALID_REJECTED", result.stdout)
        self.assertEqual(result.stdout.count("LOCK:"), 2)
        self.assertIn("LOCK:service_action enable", result.stdout)
        self.assertIn("LOCK:service_action disable-autostart", result.stdout)

    def test_disable_autostart_cli_dispatch_and_arguments(self) -> None:
        body = r'''
SB_ROOT_PREFIX=""
require_root() { return 0; }; require_systemd() { return 0; }
safe_paths() { return 0; }; is_managed() { return 0; }
with_lock() { printf 'LOCK:%s\n' "$*"; }
main disable-autostart
if main disable-autostart extra >/dev/null 2>&1; then exit 90; fi
printf 'INVALID_REJECTED\n'
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INVALID_REJECTED", result.stdout)
        self.assertEqual(result.stdout.count("LOCK:"), 1)
        self.assertIn("LOCK:service_action disable-autostart", result.stdout)

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

    def test_install_rejects_existing_core_without_downloading_or_replacing(self) -> None:
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        before = self.core.read_bytes()
        result = self.shell(
            'require_systemd() { return 0; }; check_conflicts() { return 0; }; '
            'install_dependencies() { return 0; }; '
            'download_core() { printf "download-called\\n"; return 77; }; install_core'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("download-called", result.stdout)
        self.assertIn("update", result.stdout + result.stderr)
        self.assertEqual(self.core.read_bytes(), before)

    def test_update_requires_a_managed_installed_core_before_downloading(self) -> None:
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        before = self.core.read_bytes()
        self.core.unlink()
        body = (
            'require_systemd() { return 0; }; check_conflicts() { return 0; }; '
            'install_dependencies() { return 0; }; '
            'download_core() { printf "download-called\\n"; return 77; }; update_core'
        )
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("download-called", result.stdout)
        self.assertRegex(result.stdout + result.stderr, r"install|安装")
        self.assertFalse(self.core.exists())
        self.core.write_bytes(before)
        self.core.chmod(0o755)
        marker.unlink()
        result = self.shell(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("download-called", result.stdout)
        self.assertEqual(self.core.read_bytes(), before)

    def test_update_cli_routes_version_through_locked_transaction(self) -> None:
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        body = (
            'SB_ROOT_PREFIX=""; require_root() { return 0; }; require_systemd() { return 0; }; '
            'check_conflicts() { return 0; }; install_dependencies() { return 0; }; '
            'with_lock() { printf "lock-acquired\\n"; "$@"; }; '
            'install_core_locked() { printf "transaction-version:%s\\n" "$1"; }; main update "$@"'
        )
        for args, expected in (((), "latest"), (("1.14.3",), "1.14.3")):
            with self.subTest(version=expected):
                result = self.shell(body, *args, check=True)
                self.assertEqual(result.stdout.count("lock-acquired"), 1)
                self.assertIn("transaction-version:" + expected, result.stdout)

    def test_menu_lists_and_routes_separate_update_option(self) -> None:
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(
                ["bash", "-c", 'source "$1"; show_status() { :; }; '
                 'update_core() { printf "update-called\\n"; }; menu', "test-menu", str(SCRIPT)],
                env=self.env, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            os.close(slave)
            slave = -1
            os.write(master, b"10\n\n0\n")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertRegex(stdout, r"10\.\s*更新\s*sing-box")
            self.assertRegex(stdout, r"11\.\s*修改\s*配置")
            install_lines = [line for line in stdout.splitlines() if re.match(r"\s*1\.", line)]
            self.assertTrue(install_lines)
            self.assertTrue(all("更新" not in line for line in install_lines))
            self.assertEqual(stdout.count("update-called"), 1)
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_add_wizard_lists_all_extended_protocol_routes(self) -> None:
        result = self.shell(
            'require_core() { return 0; }; prompt() { printf -v "$1" "%s" 0; }; add_wizard_locked',
            check=True,
        )
        self.assertIn("1. VLESS + TCP + REALITY + Vision", result.stdout)
        self.assertIn("2. VLESS + WebSocket", result.stdout)
        self.assertIn("3. VLESS TCP", result.stdout)
        self.assertIn("4. Shadowsocks 2022", result.stdout)
        for number, label in (
            (7, "Hysteria 2"), (8, "TUIC"), (9, "AnyTLS"),
            (10, "VLESS + gRPC + TLS"), (11, "VMess + TCP"),
            (12, "VMess + TCP + TLS"), (13, "VMess + WebSocket"),
            (14, "VMess + WebSocket + TLS"), (15, "VMess + gRPC + TLS"),
            (16, "VLESS + gRPC + REALITY"), (17, "VLESS + WebSocket + TLS"),
            (18, "VLESS + TCP + TLS"),
        ):
            with self.subTest(number=number):
                self.assertRegex(result.stdout, rf"{number}\.\s*{re.escape(label)}")

    def test_add_wizard_routes_each_protocol_to_the_generator(self) -> None:
        """Drive each interactive choice and assert the selected template id."""
        wizard_core = self.root / "wizard-core"
        wizard_core.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == generate && $2 == reality-keypair ]]; then\n"
            "  printf '%s\\n' 'PrivateKey: Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw'\n"
            "  printf '%s\\n' 'PublicKey: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n"
            "  exit 0\n"
            "fi\n"
            "case \"$1 $2\" in\n"
            "  'generate uuid') printf '%s\\n' 'b7efdc96-87aa-4910-81b4-374b2eecc737' ;;\n"
            "  'generate rand') printf '%s\\n' 'route-password' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        wizard_core.chmod(0o755)
        openssl_stub = self.bin / "openssl"
        openssl_stub.write_text(
            "#!/usr/bin/env bash\n[[ $1 == rand && $2 == -hex ]] && printf '%s\\n' 0123456789abcdef\n",
            encoding="utf-8",
        )
        openssl_stub.chmod(0o755)
        route_log = self.root / "wizard-routes.log"
        prompt_log = self.root / "wizard-prompts.log"
        expected = {
            1: "vless-reality", 2: "vless-ws", 3: "vless-tcp", 4: "shadowsocks",
            7: "hysteria2", 8: "tuic", 9: "anytls", 10: "vless-grpc-tls",
            11: "vmess-tcp", 12: "vmess-tcp-tls", 13: "vmess-ws",
            14: "vmess-ws-tls", 15: "vmess-grpc-tls", 16: "vless-grpc-reality",
            17: "vless-ws-tls", 18: "vless-tcp-tls",
        }
        for choice, protocol in expected.items():
            with self.subTest(choice=choice, protocol=protocol):
                route_log.write_text("", encoding="utf-8")
                prompt_log.write_text("", encoding="utf-8")
                env = dict(self.env)
                env.update({
                    "TEST_CHOICE": str(choice),
                    "TEST_ROUTE_LOG": str(route_log),
                    "TEST_PROMPT_LOG": str(prompt_log),
                    "TEST_WIZARD_CORE": str(wizard_core),
                })
                body = r'''
                    require_core() { return 0; }
                    prompt() {
                        local variable=$1
                        printf '%s\n' "$variable" >> "$TEST_PROMPT_LOG"
                        case "$variable" in
                            choice) printf -v "$variable" '%s' "$TEST_CHOICE" ;;
                            tag) printf -v "$variable" '%s' "route-$TEST_CHOICE" ;;
                            port) printf -v "$variable" '%s' "$((20000 + TEST_CHOICE))" ;;
                            obfs_choice) printf -v "$variable" '%s' 1 ;;
                            extra)
                                case "$TEST_CHOICE" in
                                    2|13|14|17) printf -v "$variable" '%s' '/route-ws' ;;
                                    10|15|16) printf -v "$variable" '%s' 'RouteGrpc' ;;
                                    *) printf -v "$variable" '%s' '' ;;
                                esac ;;
                            tls_name) printf -v "$variable" '%s' 'example.com' ;;
                            certificate) printf -v "$variable" '%s' '/tmp/route-cert.pem' ;;
                            tls_key) printf -v "$variable" '%s' '/tmp/route-key.pem' ;;
                            server) printf -v "$variable" '%s' '' ;;
                            *) printf -v "$variable" '%s' '' ;;
                        esac
                    }
                    prompt_quic_bbr() { printf -v "$1" 2; }
                    prepare_quic_bbr_wizard() { : >"$4"; cat -- "$1"; }
                    generate_inbound() { printf '%s\n' "$1" >> "$TEST_ROUTE_LOG"; printf '{"type":"direct","tag":"route"}\n'; }
                    add_managed_inbound() { return 0; }
                    atomic_copy() { return 0; }
                    show_client() { return 0; }
                    CORE_BIN="$TEST_WIZARD_CORE"
                    export CORE_BIN
                    add_wizard_locked
                '''
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; ' + body, "wizard-route", str(SCRIPT)],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(route_log.read_text(encoding="utf-8").strip(), protocol)
                prompt_names = prompt_log.read_text(encoding="utf-8").splitlines()
                if protocol == "vless-tcp-tls":
                    self.assertNotIn("extra", prompt_names)
                if protocol == "vless-ws-tls":
                    self.assertIn("extra", prompt_names)

    def test_tls_wizard_persists_config_metadata_and_client_export(self) -> None:
        integration_core = self.root / "integration-core"
        integration_core.write_text(
            MOCK_CORE.replace(
                "args = sys.argv[1:]\n",
                "args = sys.argv[1:]\n"
                "if args[:2] == ['generate', 'uuid']:\n"
                "    print('b7efdc96-87aa-4910-81b4-374b2eecc737')\n"
                "    sys.exit(0)\n"
                "if args[:2] == ['generate', 'rand']:\n"
                "    print('integration-password')\n"
                "    sys.exit(0)\n",
            ),
            encoding="utf-8",
        )
        integration_core.chmod(0o755)
        for choice, protocol, tag in (
            (10, "vless-grpc-tls", "integration-grpc-tls"),
            (18, "vless-tcp-tls", "integration-tcp-tls"),
        ):
            with self.subTest(protocol=protocol):
                self.config.write_bytes(self.original_bytes)
                certificate, private_key = self.tls_material()
                env = dict(
                    self.env,
                    TEST_CHOICE=str(choice),
                    TEST_TAG=tag,
                    TEST_CERTIFICATE=str(certificate),
                    TEST_PRIVATE_KEY=str(private_key),
                    TEST_WIZARD_CORE=str(integration_core),
                )
                body = r'''
                    require_core() { return 0; }
                    prompt() {
                        local variable=$1
                        case "$variable" in
                            choice) printf -v "$variable" '%s' "$TEST_CHOICE" ;;
                            tag) printf -v "$variable" '%s' "$TEST_TAG" ;;
                            port) printf -v "$variable" '%s' "$((28443 + TEST_CHOICE))" ;;
                            extra)
                                if [[ $TEST_CHOICE == 10 ]]; then
                                    printf -v "$variable" '%s' IntegrationGrpc
                                else
                                    printf 'unexpected extra prompt for TCP TLS\n' >&2
                                    return 99
                                fi ;;
                            tls_name) printf -v "$variable" '%s' example.com ;;
                            certificate) printf -v "$variable" '%s' "$TEST_CERTIFICATE" ;;
                            tls_key) printf -v "$variable" '%s' "$TEST_PRIVATE_KEY" ;;
                            server) printf -v "$variable" '%s' example.com ;;
                            *) printf -v "$variable" '%s' '' ;;
                        esac
                    }
                    CORE_BIN="$TEST_WIZARD_CORE"
                    add_wizard_locked
                '''
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; ' + body, "wizard-integration", str(SCRIPT)],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                config = json.loads(self.config.read_text(encoding="utf-8"))
                self.assertEqual(len(config["inbounds"]), 1)
                inbound = config["inbounds"][0]
                self.assertEqual(inbound["tag"], tag)
                self.assertEqual(inbound["type"], "vless")
                if protocol == "vless-grpc-tls":
                    self.assertEqual(inbound["transport"], {"type": "grpc", "service_name": "IntegrationGrpc"})
                    self.assertEqual(inbound["tls"]["alpn"], ["h2"])
                else:
                    self.assertNotIn("transport", inbound)
                    self.assertNotIn("flow", inbound["users"][0])
                    self.assertNotIn("alpn", inbound["tls"])
                self.assertEqual(inbound["tls"]["server_name"], "example.com")
                self.assertEqual(inbound["tls"]["certificate_path"], str(certificate))
                self.assertEqual(inbound["tls"]["key_path"], str(private_key))

                metadata_path = self.state_dir / "clients" / (tag + ".json")
                self.assertTrue(metadata_path.is_file())
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["server"], "example.com")
                self.assertEqual(metadata["public_key"], "")
                self.assertEqual(metadata["inbound"], inbound)

                inbound_path = self.root / (tag + "-inbound.json")
                dump(inbound_path, inbound)
                exported = self.shell(
                    'generate_client "$1" "$2"', str(inbound_path), "example.com", check=True,
                )
                client = json.loads(exported.stdout)
                if protocol == "vless-grpc-tls":
                    self.assertEqual(client["transport"], {"type": "grpc", "service_name": "IntegrationGrpc"})
                else:
                    self.assertNotIn("transport", client)
                    self.assertNotIn("flow", client)
                self.assertTrue(client["tls"]["enabled"])
                self.assertEqual(client["tls"]["server_name"], "example.com")
                self.assertFalse(client["tls"].get("insecure", False))
                outbound_path = self.root / (tag + "-outbound.json")
                outbound_path.write_text(exported.stdout, encoding="utf-8")
                link = self.shell(
                    'client_link "$1" "integration"', str(outbound_path), check=True,
                ).stdout.strip()
                self.assertTrue(link.startswith("vless://"), link)
                self.assertIn("security=tls", link)
                self.assertIn("sni=example.com", link)
                if protocol == "vless-grpc-tls":
                    self.assertIn("type=grpc", link)
                    self.assertIn("serviceName=IntegrationGrpc", link)
                else:
                    self.assertIn("type=tcp", link)
                    self.assertNotIn("path=", link)
                    self.assertNotIn("serviceName=", link)
                    self.assertNotIn("flow=", link)

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

    def test_prompt_allows_backspace_delete_and_cursor_editing(self) -> None:
        import select
        import termios
        import time

        cases = (
            (b"example.xuz\x08\x08yz\n", "example.xyz", ""),
            (b"example.xuz\x7f\x7fyz\n", "example.xyz", ""),
            (b"ac\x1b[Db\n", "abc", ""),
            (b"abxc\x1b[D\x1b[D\x1b[3~\n", "abc", ""),
            (b"wrong\x15correct\n", "correct", ""),
            (b"\n", "fallback", "fallback"),
            (b"path\\name\n", "path\\name", ""),
            (b"example.xuz\x08\x08yz\n", "example.xyz", "fallback"),
        )
        for keys, expected, default in cases:
            with self.subTest(keys=keys, default=default):
                master, slave = pty.openpty()
                attrs = termios.tcgetattr(slave)
                attrs[6][termios.VERASE] = b"\x7f"
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
                env = dict(self.env, TERM="xterm", INPUTRC="/dev/null")
                process = subprocess.Popen(
                    ["bash", "--noprofile", "--norc", "-c",
                     'source "$1"; prompt answer EDIT "$2" && printf "%s" "$answer"',
                     "prompt-edit", str(SCRIPT), default],
                    env=env, stdin=slave, stdout=subprocess.PIPE, stderr=slave,
                )
                os.close(slave)
                try:
                    seen = b""
                    deadline = time.monotonic() + 5
                    while b"EDIT" not in seen and time.monotonic() < deadline:
                        ready, _, _ = select.select([master], [], [], 0.1)
                        if ready:
                            seen += os.read(master, 4096)
                    self.assertIn(b"EDIT", seen, "Prompt did not become ready")
                    os.write(master, keys)
                    stdout, _ = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0)
                    self.assertEqual(stdout.decode(), expected)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdout:
                        process.stdout.close()
                    os.close(master)

    def test_config_list_displays_complete_links_and_continues_past_missing_metadata(self) -> None:
        import base64
        from urllib.parse import parse_qs, urlsplit

        ws = self.inbound("ws-node", 18443)
        ws["transport"]["path"] = "/proxy?one=a&two=b"
        missing = self.inbound("missing-address", 18444)
        stale = self.inbound("changed-node", 18445)
        reality = self.inbound("missing-key", 18446)
        reality.pop("transport")
        reality["users"][0]["flow"] = "xtls-rprx-vision"
        reality["tls"] = {
            "enabled": True, "server_name": "example.com",
            "reality": {"enabled": True, "short_id": ["0123456789abcdef"]},
        }
        vmess = {
            "type": "vmess", "tag": "vmess-node", "listen_port": 18447,
            "users": [{"uuid": ws["users"][0]["uuid"], "alterId": 0}],
        }
        ss = {
            "type": "shadowsocks", "tag": "ss-node", "listen_port": 18448,
            "method": "2022-blake3-aes-128-gcm", "password": "8JCsPssfgS8tiRwiMlhARg==",
        }
        value = dict(self.original)
        value["inbounds"] = [ws, missing, stale, reality, vmess, ss]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        for inbound in (ws, reality, vmess, ss):
            dump(client_dir / (inbound["tag"] + ".json"), {
                "server": "example.com", "public_key": "", "inbound": inbound,
            })
        dump(client_dir / "changed-node.json", {
            "server": "stale.example.com", "public_key": "",
            "inbound": self.inbound("changed-node", 18445) | {"listen_port": 9999},
        })
        before = {path: path.read_bytes() for path in [self.config, *client_dir.glob("*.json")]}
        result = self.shell(
            'prompt() { [[ $1 == choice ]] || return 97; printf -v "$1" "%s" 1; }; show_config',
            check=True,
        )
        links = [line for line in result.stdout.splitlines() if line.startswith(("vless://", "vmess://", "ss://"))]
        self.assertEqual(len(links), 3, result.stdout + result.stderr)
        parsed = urlsplit(links[0])
        self.assertEqual(parsed.hostname, "example.com")
        self.assertEqual(parsed.port, 18443)
        self.assertEqual(parse_qs(parsed.query)["path"], [ws["transport"]["path"]])
        self.assertEqual(parsed.fragment, "ws-node")
        payload = json.loads(base64.b64decode(links[1].removeprefix("vmess://")))
        self.assertEqual(payload["add"], "example.com")
        self.assertEqual(payload["port"], "18447")
        self.assertTrue(links[2].endswith("@example.com:18448#ss-node"))
        self.assertNotIn("stale.example.com", result.stdout)
        self.assertNotIn('"outbounds"', result.stdout)
        self.assertNotIn("[信息] 客户端 outbound JSON", result.stdout)
        self.assertIn("缺少当前配置的公网", result.stderr)
        self.assertIn("缺少此 REALITY 入站对应的公钥", result.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.calls(self.system_log), [])

    def test_empty_config_list_has_no_links_or_prompts(self) -> None:
        result = self.shell('prompt() { return 97; }; show_config_list', check=True)
        self.assertIn("暂无入站配置", result.stdout)
        self.assertNotIn("://", result.stdout)

    def test_full_client_view_still_displays_json_and_link(self) -> None:
        inbound = self.inbound()
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        result = self.shell('show_client "$1" example.com', inbound["tag"], check=True)
        self.assertIn("客户端 outbound JSON", result.stdout)
        self.assertIn('"server": "example.com"', result.stdout)
        self.assertIn("vless://", result.stdout)

    @property
    def default_server_file(self) -> Path:
        return self.state_dir / "default-server"

    def test_default_server_read_write_modify_clear_and_permissions(self) -> None:
        result = self.shell("read_default_server", check=True)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.default_server_file.exists())

        self.shell('set_default_server "$1"', "global.example", check=True)
        self.assertEqual(self.default_server_file.read_text(encoding="utf-8"), "global.example\n")
        self.assertEqual(self.default_server_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.shell("read_default_server", check=True).stdout, "global.example\n")

        self.shell('set_default_server "$1"', "changed.example", check=True)
        self.assertEqual(self.shell("read_default_server", check=True).stdout, "changed.example\n")

        self.shell('set_default_server -', check=True)
        self.assertEqual(self.shell("read_default_server", check=True).stdout, "")
        self.assertFalse(self.default_server_file.exists())

    def test_default_server_rejects_invalid_and_symlink_without_writing(self) -> None:
        self.shell('set_default_server "$1"', "safe.example", check=True)
        before = self.default_server_file.read_bytes()
        for address in ("https://example.com", "example.com:443", "bad..example", "bad host", "127.0.0.1:443"):
            with self.subTest(address=address):
                result = self.shell('set_default_server "$1"', address)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.default_server_file.read_bytes(), before)

        invalid = self.default_server_file.with_name("invalid-default-server")
        invalid.write_text("bad..example\n", encoding="utf-8")
        result = self.shell('DEFAULT_SERVER_FILE="$1"; read_default_server', str(invalid))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invalid.read_text(encoding="utf-8"), "bad..example\n")

        target = self.root / "default-target"
        target.write_text("target.example\n", encoding="utf-8")
        self.default_server_file.unlink()
        self.default_server_file.symlink_to(target)
        result = self.shell("read_default_server", check=False)
        self.assertNotEqual(result.returncode, 0)
        result = self.shell('set_default_server "$1"', "new.example")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "target.example\n")
        self.assertTrue(self.default_server_file.is_symlink())

    def test_server_cli_uses_lock_and_show_config_menu_five_routes(self) -> None:
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        result = self.shell(
            'require_root() { return 0; }; require_systemd() { return 0; }; '
            'with_lock() { printf "locked\\n"; "$@"; }; SB_ROOT_PREFIX=""; main server "$1"',
            "cli.example", check=True,
        )
        self.assertIn("locked", result.stdout)
        self.assertEqual(self.shell("read_default_server", check=True).stdout, "cli.example\n")

        result = self.shell(
            'prompt() { printf -v "$1" "%s" 5; }; '
            'with_lock() { printf "locked\\n"; "$@"; }; '
            'set_default_server() { printf "server-route\\n"; }; show_config',
            check=True,
        )
        self.assertIn("locked", result.stdout)
        self.assertIn("server-route", result.stdout)

    def test_show_client_address_priority_and_hysteria2_sni_fallback(self) -> None:
        certificate, private_key = self.tls_material("hy2.example")
        inbound = self.generate_inbound(
            "hysteria2", "hy2-password", tls_server_name="hy2.example",
            certificate=certificate, key=private_key,
        )
        inbound["tag"] = "hy2-node"
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        metadata_path = self.state_dir / "clients" / "hy2-node.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)

        dump(metadata_path, {"server": "node.example", "public_key": "", "inbound": inbound})
        self.shell('set_default_server "$1"', "global.example", check=True)
        config_before_list = self.config.read_bytes()
        metadata_before_list = metadata_path.read_bytes()
        list_result = self.shell("show_config_list", check=True)
        self.assertIn("hysteria2://", list_result.stdout)
        self.assertIn("@node.example:18443", list_result.stdout)
        self.assertEqual(self.config.read_bytes(), config_before_list)
        self.assertEqual(metadata_path.read_bytes(), metadata_before_list)
        result = self.shell('show_client "$1"', "hy2-node", check=True)
        self.assertIn('"server": "node.example"', result.stdout)
        self.assertIn("@node.example:18443", result.stdout)

        result = self.shell('show_client "$1" "$2"', "hy2-node", "explicit.example", check=True)
        self.assertIn('"server": "explicit.example"', result.stdout)
        self.assertIn("@explicit.example:18443", result.stdout)

        metadata_path.unlink()
        result = self.shell('show_client "$1"', "hy2-node", check=True)
        self.assertIn('"server": "global.example"', result.stdout)
        self.assertIn("@global.example:18443", result.stdout)

        self.shell("set_default_server -", check=True)
        result = self.shell(
            'prompt() { printf "unexpected prompt\\n" >&2; return 97; }; show_client "$1"',
            "hy2-node", check=True,
        )
        self.assertIn('"server": "hy2.example"', result.stdout)
        self.assertIn("@hy2.example:18443", result.stdout)
        metadata_path.write_text(
            json.dumps({"server": "", "public_key": "", "inbound": inbound}) + "\n",
            encoding="utf-8",
        )
        result = self.shell(
            'prompt() { printf "unexpected prompt\\n" >&2; return 97; }; show_client "$1"',
            "hy2-node", check=True,
        )
        self.assertIn('"server": "hy2.example"', result.stdout)
        self.assertIn("@hy2.example:18443", result.stdout)
        config_before_fallback_list = self.config.read_bytes()
        metadata_before_fallback_list = metadata_path.read_bytes()
        list_result = self.shell("show_config_list", check=True)
        self.assertIn("hysteria2://", list_result.stdout)
        self.assertIn("@hy2.example:18443", list_result.stdout)
        self.assertEqual(self.config.read_bytes(), config_before_fallback_list)
        self.assertEqual(metadata_path.read_bytes(), metadata_before_fallback_list)

    def test_global_default_beats_hysteria2_sni_and_stale_metadata_is_ignored(self) -> None:
        certificate, private_key = self.tls_material("hy2.example")
        inbound = self.generate_inbound(
            "hysteria2", "hy2-password", tls_server_name="hy2.example",
            certificate=certificate, key=private_key,
        )
        inbound["tag"] = "stale-hy2"
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        metadata_path = self.state_dir / "clients" / "stale-hy2.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        stale = dict(inbound)
        stale["listen_port"] = 29999
        dump(metadata_path, {"server": "old.example", "public_key": "", "inbound": stale})
        self.shell('set_default_server "$1"', "global.example", check=True)
        before = self.config.read_bytes()
        result = self.shell('show_client "$1"', "stale-hy2", check=True)
        self.assertIn('"server": "global.example"', result.stdout)
        self.assertNotIn("old.example", result.stdout)
        self.assertEqual(self.config.read_bytes(), before)

        ordinary = self.inbound("ordinary-vless", 18444)
        value["inbounds"] = [ordinary]
        dump(self.config, value)
        result = self.shell('show_client "$1"', "ordinary-vless", check=True)
        self.assertIn('"server": "global.example"', result.stdout)
        self.assertIn("@global.example:18444", result.stdout)

    def run_wizard_for_server_default(self, choice: str, tls_name: str, expected: str) -> None:
        wizard_core = self.root / ("wizard-core-" + choice)
        wizard_core.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == generate && $2 == uuid ]]; then printf '%s\\n' 'b7efdc96-87aa-4910-81b4-374b2eecc737'; exit 0; fi\n"
            "if [[ $1 == generate && $2 == rand ]]; then printf '%s\\n' 'wizard-password'; exit 0; fi\n",
            encoding="utf-8",
        )
        wizard_core.chmod(0o755)
        prompt_log = self.root / ("wizard-defaults-" + choice + ".log")
        env = dict(self.env, TEST_WIZARD_CHOICE=choice, TEST_TLS_NAME=tls_name, TEST_WIZARD_CORE=str(wizard_core), TEST_PROMPT_LOG=str(prompt_log))
        body = r'''
            require_core() { return 0; }
            prompt() {
                local variable=$1
                printf '%s|%s\n' "$variable" "${3-}" >> "$TEST_PROMPT_LOG"
                case "$variable" in
                    choice) printf -v "$variable" '%s' "$TEST_WIZARD_CHOICE" ;;
                    tag) printf -v "$variable" '%s' wizard-default ;;
                    port) printf -v "$variable" '%s' 29443 ;;
                    obfs_choice) printf -v "$variable" '%s' 1 ;;
                    extra) printf -v "$variable" '%s' WizardGrpc ;;
                    tls_name) printf -v "$variable" '%s' "$TEST_TLS_NAME" ;;
                    certificate) printf -v "$variable" '%s' /tmp/wizard-cert.pem ;;
                    tls_key) printf -v "$variable" '%s' /tmp/wizard-key.pem ;;
                    server) printf -v "$variable" '%s' "${3-}" ;;
                    *) printf -v "$variable" '%s' '' ;;
                esac
            }
            prompt_quic_bbr() { printf -v "$1" 2; }
            prepare_quic_bbr_wizard() { : >"$4"; cat -- "$1"; }
            generate_inbound() { printf '{"type":"direct","tag":"wizard-default"}\n'; }
            generate_client() { return 0; }
            add_managed_inbound() { return 0; }
            atomic_copy() { return 0; }
            show_client() { return 0; }
            CORE_BIN="$TEST_WIZARD_CORE"
            add_wizard_locked
        '''
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; ' + body, "wizard-default", str(SCRIPT)],
            env=env, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entries = [
            line.split("|", 1)[1]
            for line in prompt_log.read_text(encoding="utf-8").splitlines()
            if line.startswith("server|")
        ]
        self.assertEqual(entries[-1], expected)

    def test_wizard_server_defaults_use_global_or_hysteria2_sni(self) -> None:
        self.shell('set_default_server "$1"', "global.example", check=True)
        self.run_wizard_for_server_default("7", "hy2.example", "global.example")
        self.run_wizard_for_server_default("10", "tls.example", "global.example")
        self.shell("set_default_server -", check=True)
        self.run_wizard_for_server_default("7", "hy2.example", "hy2.example")
        self.run_wizard_for_server_default("10", "tls.example", "")

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

    def test_detect_inbound_template_identifies_all_protocols(self) -> None:
        certificate, private_key = self.tls_material()
        protocols = (
            "vless-reality", "vless-ws", "vless-tcp", "shadowsocks",
            "hysteria2", "tuic", "anytls", "vless-grpc-tls", "vless-tcp-tls",
            "vmess-tcp", "vmess-tcp-tls", "vmess-ws", "vmess-ws-tls",
            "vmess-grpc-tls", "vless-grpc-reality", "vless-ws-tls",
        )
        for index, protocol in enumerate(protocols, start=1):
            with self.subTest(protocol=protocol):
                inbound = self.edit_protocol_inbound(
                    protocol, "detect-" + str(index), 21000 + index, certificate, private_key,
                )
                path = self.root / ("detect-" + protocol + ".json")
                dump(path, inbound)
                result = self.shell('detect_inbound_template "$1"', str(path), check=True)
                self.assertEqual(result.stdout.strip(), protocol)

    def test_replace_inbound_preserves_order_and_syncs_renamed_metadata(self) -> None:
        old = self.inbound("old-node", 22001)
        other = self.inbound("other-node", 22002)
        value = dict(self.original)
        value["inbounds"] = [old, other]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        old_metadata = {"server": "old.example", "public_key": "", "inbound": old}
        other_metadata = {"server": "other.example", "public_key": "", "inbound": other}
        dump(client_dir / "old-node.json", old_metadata)
        dump(client_dir / "other-node.json", other_metadata)
        other_before = (client_dir / "other-node.json").read_bytes()

        candidate = dict(self.inbound("renamed-node", 22003))
        candidate["unknown_edit_field"] = {"keep": True}
        candidate_path = self.root / "replace-node.json"
        dump(candidate_path, candidate)
        result = self.shell(
            'replace_inbound_file "$1" "$2" "$3" "$4"',
            "old-node", str(candidate_path), "node.example", "", check=True,
        )
        self.assertEqual(result.returncode, 0)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual([node["tag"] for node in saved["inbounds"]], ["renamed-node", "other-node"])
        self.assertEqual(saved["inbounds"][0]["unknown_edit_field"], {"keep": True})
        self.assertFalse((client_dir / "old-node.json").exists())
        new_metadata_path = client_dir / "renamed-node.json"
        self.assertTrue(new_metadata_path.is_file())
        new_metadata = json.loads(new_metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(new_metadata["server"], "node.example")
        self.assertEqual(new_metadata["public_key"], "")
        self.assertEqual(new_metadata["inbound"], candidate)
        self.assertEqual(new_metadata_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((client_dir / "other-node.json").read_bytes(), other_before)
        self.assertFalse(any(
            any(action in call for action in ("start", "stop", "restart", "enable", "disable"))
            for call in self.calls(self.system_log)
        ))

    def test_replace_each_protocol_template_succeeds(self) -> None:
        certificate, private_key = self.tls_material()
        protocols = (
            "vless-reality", "vless-ws", "vless-tcp", "shadowsocks",
            "hysteria2", "tuic", "anytls", "vless-grpc-tls", "vless-tcp-tls",
            "vmess-tcp", "vmess-tcp-tls", "vmess-ws", "vmess-ws-tls",
            "vmess-grpc-tls", "vless-grpc-reality", "vless-ws-tls",
        )
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        for index, protocol in enumerate(protocols, start=1):
            with self.subTest(protocol=protocol):
                old_tag, new_tag = "old-" + protocol, "new-" + protocol
                old = self.edit_protocol_inbound(protocol, old_tag, 22100 + index, certificate, private_key)
                other = self.inbound("keep-" + protocol, 22200 + index)
                value = dict(self.original)
                value["inbounds"] = [old, other]
                dump(self.config, value)
                dump(client_dir / (old_tag + ".json"), {
                    "server": "old.example", "public_key": "", "inbound": old,
                })
                candidate = self.edit_protocol_inbound(protocol, new_tag, 22300 + index, certificate, private_key)
                candidate_path = self.root / ("replace-" + protocol + ".json")
                dump(candidate_path, candidate)
                public_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" if "reality" in protocol else ""
                result = self.shell(
                    'replace_inbound_file "$1" "$2" "" "$3"',
                    old_tag, str(candidate_path), public_key, check=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = json.loads(self.config.read_text(encoding="utf-8"))
                self.assertEqual(saved["inbounds"][0]["tag"], new_tag)
                self.assertEqual(saved["inbounds"][1]["tag"], other["tag"])
                self.assertFalse((client_dir / (old_tag + ".json")).exists())
                self.assertTrue((client_dir / (new_tag + ".json")).exists())
                if REAL_CORE:
                    checked = subprocess.run(
                        [REAL_CORE, "check", "-c", str(self.config)],
                        capture_output=True, text=True, timeout=20,
                    )
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_replace_rejects_tag_port_and_route_conflicts_without_changes(self) -> None:
        old = self.inbound("old-node", 22401)
        other = self.inbound("other-node", 22402)
        value = dict(self.original)
        value["inbounds"] = [old, other]
        value["route"] = {"rules": [{"inbound": ["old-node"]}]}
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "old.example", "public_key": "", "inbound": old})
        before_config = self.config.read_bytes()
        before_metadata = (client_dir / "old-node.json").read_bytes()

        for candidate_tag, candidate_port in (("other-node", 22403), ("renamed-node", 22402)):
            with self.subTest(candidate_tag=candidate_tag, candidate_port=candidate_port):
                candidate = self.inbound(candidate_tag, candidate_port)
                candidate_path = self.root / ("conflict-" + candidate_tag + ".json")
                dump(candidate_path, candidate)
                result = self.shell(
                    'replace_inbound_file "$1" "$2" "" ""',
                    "old-node", str(candidate_path),
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.config.read_bytes(), before_config)
                self.assertEqual((client_dir / "old-node.json").read_bytes(), before_metadata)
                self.assertFalse((client_dir / (candidate_tag + ".json")).exists())

    def test_replace_allows_same_port_when_the_replaced_node_owns_it(self) -> None:
        old = self.inbound("old-node", 22501)
        value = dict(self.original)
        value["inbounds"] = [old]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "", "public_key": "", "inbound": old})
        candidate = self.inbound("renamed-node", 22501)
        candidate_path = self.root / "same-port.json"
        dump(candidate_path, candidate)
        self.env["SB_MOCK_PORT_BUSY"] = "1"
        result = self.shell(
            'replace_inbound_file "$1" "$2" "" ""',
            "old-node", str(candidate_path), check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["inbounds"][0]["tag"], "renamed-node")

    def test_replace_metadata_write_failure_rolls_back_config_and_metadata(self) -> None:
        old = self.inbound("old-node", 22601)
        value = dict(self.original)
        value["inbounds"] = [old]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "old.example", "public_key": "", "inbound": old})
        candidate = self.inbound("renamed-node", 22602)
        candidate_path = self.root / "metadata-failure.json"
        dump(candidate_path, candidate)
        before_config = self.config.read_bytes()
        before_metadata = (client_dir / "old-node.json").read_bytes()
        body = r'''
            FAIL_METADATA_ONCE=1
            atomic_copy() {
                if [[ $FAIL_METADATA_ONCE == 1 && $2 == *"/clients/renamed-node.json" ]]; then
                    FAIL_METADATA_ONCE=0
                    return 1
                fi
                install -m "${3:-600}" "$1" "$2"
            }
            replace_inbound_file "$@"
        '''
        result = self.shell(body, "old-node", str(candidate_path), "", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((client_dir / "old-node.json").read_bytes(), before_metadata)
        self.assertFalse((client_dir / "renamed-node.json").exists())

    def test_replace_restart_failure_rolls_back_config_and_metadata(self) -> None:
        old = self.inbound("old-node", 22701)
        value = dict(self.original)
        value["inbounds"] = [old]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "old.example", "public_key": "", "inbound": old})
        candidate = self.inbound("renamed-node", 22702)
        candidate_path = self.root / "restart-failure.json"
        dump(candidate_path, candidate)
        before_config = self.config.read_bytes()
        before_metadata = (client_dir / "old-node.json").read_bytes()
        self.state(active=True, restart_failures=1)
        result = self.shell(
            'replace_inbound_file "$1" "$2" "" ""',
            "old-node", str(candidate_path),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((client_dir / "old-node.json").read_bytes(), before_metadata)
        self.assertFalse((client_dir / "renamed-node.json").exists())
        self.assertTrue(self.state()["active"])

    def test_replace_server_only_metadata_change_does_not_restart(self) -> None:
        old = self.inbound("old-node", 22801)
        value = dict(self.original)
        value["inbounds"] = [old]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "old.example", "public_key": "", "inbound": old})
        candidate_path = self.root / "same-node.json"
        dump(candidate_path, old)
        expected_path = self.root / "expected-old.json"
        dump(expected_path, old)
        self.state(active=True)
        result = self.shell(
            'replace_inbound_file "$1" "$2" "$3" "" "$4"',
            "old-node", str(candidate_path), "new.example", str(expected_path),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.state()["active"])
        self.assertFalse(any("restart" in call for call in self.calls(self.system_log)))
        self.assertEqual(json.loads((client_dir / "old-node.json").read_text())["server"], "new.example")

    def test_replace_expected_old_snapshot_drift_is_rejected_without_changes(self) -> None:
        old = self.inbound("old-node", 22811)
        value = dict(self.original)
        value["inbounds"] = [old]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "old-node.json", {"server": "old.example", "public_key": "", "inbound": old})
        candidate = self.inbound("renamed-node", 22812)
        candidate_path = self.root / "snapshot-candidate.json"
        dump(candidate_path, candidate)
        expected = dict(old)
        expected["listen_port"] = 29999
        expected_path = self.root / "drifted-old.json"
        dump(expected_path, expected)
        before_config = self.config.read_bytes()
        before_metadata = (client_dir / "old-node.json").read_bytes()
        result = self.shell(
            'replace_inbound_file "$1" "$2" "" "" "$3"',
            "old-node", str(candidate_path), str(expected_path),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((client_dir / "old-node.json").read_bytes(), before_metadata)
        self.assertFalse((client_dir / "renamed-node.json").exists())

    def run_edit_wizard_with_test_prompts(self, tag: str, certificate: Path, private_key: Path, cancel: bool = False) -> subprocess.CompletedProcess[str]:
        env = dict(self.env, TEST_CERTIFICATE=str(certificate), TEST_PRIVATE_KEY=str(private_key))
        if cancel:
            env["TEST_EDIT_CANCEL"] = "1"
        body = r'''
            prompt() {
                local variable=$1 message=$2 value
                if [[ $TEST_EDIT_CANCEL == 1 ]]; then
                    printf -v "$variable" '%s' "$3"
                    return 0
                fi
                case "$message" in
                    *gRPC*) value=EditedGrpc ;;
                    *TLS*域名*|*握手域名*) value=edited.example ;;
                    *REALITY*|*Reality*) value=$3 ;;
                    *证书*) value=$TEST_CERTIFICATE ;;
                    *私钥*) value=$TEST_PRIVATE_KEY ;;
                    *公网*|*服务器*|*连接地址*|*客户端*地址*) value=edited.example ;;
                    *) value=$3 ;;
                esac
                printf -v "$variable" '%s' "$value"
            }
            prompt_secret() {
                local value=$3
                [[ -n $value ]] || value=edit-secret
                printf -v "$1" '%s' "$value"
            }
            confirm() { [[ -z $TEST_EDIT_CANCEL || $TEST_EDIT_CANCEL == 0 ]]; }
            edit_wizard_locked "$1"
        '''
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + body, "edit-wizard", str(SCRIPT), tag],
            env=env, text=True, capture_output=True, timeout=20,
        )

    def test_edit_wizard_tls_updates_config_and_metadata(self) -> None:
        certificate, private_key = self.tls_material("example.com")
        original_certificate = self.root / "original-cert.pem"
        original_private_key = self.root / "original-key.pem"
        shutil.copy2(certificate, original_certificate)
        shutil.copy2(private_key, original_private_key)
        edited_certificate, edited_private_key = self.tls_material("edited.example")
        inbound = self.edit_protocol_inbound(
            "vless-grpc-tls", "tls-edit", 22901, original_certificate, original_private_key,
        )
        inbound["unknown_edit_field"] = {"keep": True}
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "tls-edit.json", {"server": "old.example", "public_key": "", "inbound": inbound})
        result = self.run_edit_wizard_with_test_prompts("tls-edit", edited_certificate, edited_private_key)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        self.assertEqual(saved["tag"], "tls-edit")
        self.assertEqual(saved["transport"]["service_name"], "EditedGrpc")
        self.assertEqual(saved["tls"]["server_name"], "edited.example")
        self.assertEqual(saved["unknown_edit_field"], {"keep": True})
        metadata = json.loads((client_dir / "tls-edit.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["server"], "edited.example")
        self.assertEqual(metadata["inbound"], saved)

    def test_edit_wizard_vless_tls_templates_updates_uuid_transport_sni_and_metadata(self) -> None:
        new_uuid = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
        cases = {
            "vless-ws-tls": ("/edited-vless-tls", True),
            "vless-tcp-tls": ("", False),
        }
        for index, (protocol, case) in enumerate(cases.items()):
            path, websocket = case
            with self.subTest(protocol=protocol):
                original_certificate, original_private_key = self.tls_material("example.com")
                original_certificate_copy = self.root / (protocol + "-original-cert.pem")
                original_private_key_copy = self.root / (protocol + "-original-key.pem")
                shutil.copy2(original_certificate, original_certificate_copy)
                shutil.copy2(original_private_key, original_private_key_copy)
                edited_certificate, edited_private_key = self.tls_material("edited.example")
                tag = protocol + "-edit"
                inbound = self.edit_protocol_inbound(
                    protocol, tag, 22904 + index, original_certificate_copy, original_private_key_copy,
                )
                value = dict(self.original)
                value["inbounds"] = [inbound]
                dump(self.config, value)
                client_dir = self.state_dir / "clients"
                client_dir.mkdir(parents=True, exist_ok=True)
                dump(client_dir / (tag + ".json"), {
                    "server": "old.example", "public_key": "", "inbound": inbound,
                })
                env = dict(
                    self.env,
                    TEST_CERTIFICATE=str(edited_certificate),
                    TEST_PRIVATE_KEY=str(edited_private_key),
                    TEST_WEBSOCKET="1" if websocket else "",
                )
                body = r'''
                    prompt() {
                        local variable=$1 message=$2 value=${3-}
                        if [[ $TEST_WEBSOCKET == 1 && $message == *WebSocket* ]]; then
                            value=/edited-vless-tls
                        elif [[ $message == *TLS*域名* ]]; then
                            value=edited.example
                        elif [[ $message == *公网* || $message == *服务器* || $message == *连接地址* || $message == *客户端*地址* ]]; then
                            value=edited.example
                        fi
                        printf -v "$variable" '%s' "$value"
                    }
                    prompt_secret() {
                        if [[ $2 == *UUID* ]]; then printf -v "$1" '%s' "$TEST_NEW_UUID"; else printf -v "$1" '%s' "${3-}"; fi
                    }
                    prompt_tls_certificate_files() {
                        printf -v certificate '%s' "$TEST_CERTIFICATE"
                        printf -v tls_key '%s' "$TEST_PRIVATE_KEY"
                    }
                    confirm() { return 0; }
                    edit_wizard_locked "$1"
                '''
                env["TEST_NEW_UUID"] = new_uuid
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; shift; ' + body, "edit-vless-tls", str(SCRIPT), tag],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
                self.assertEqual(saved["users"][0]["uuid"], new_uuid)
                if websocket:
                    self.assertEqual(saved["transport"], {"type": "ws", "path": path})
                else:
                    self.assertNotIn("transport", saved)
                self.assertNotIn("flow", saved["users"][0])
                self.assertTrue(saved["tls"]["enabled"])
                self.assertEqual(saved["tls"]["server_name"], "edited.example")
                self.assertEqual(saved["tls"]["certificate_path"], str(edited_certificate))
                self.assertEqual(saved["tls"]["key_path"], str(edited_private_key))
                metadata = json.loads((client_dir / (tag + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(metadata["server"], "edited.example")
                self.assertEqual(metadata["inbound"], saved)

    def test_edit_wizard_reality_updates_handshake_and_metadata(self) -> None:
        inbound = self.edit_protocol_inbound("vless-reality", "reality-edit", 22902)
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "reality-edit.json", {
            "server": "old.example", "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "inbound": inbound,
        })
        result = self.run_edit_wizard_with_test_prompts(
            "reality-edit", self.root / "unused-cert.pem", self.root / "unused-key.pem",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        self.assertEqual(saved["tls"]["server_name"], "edited.example")
        self.assertEqual(saved["tls"]["reality"]["handshake"]["server"], "edited.example")
        self.assertEqual(saved["users"][0]["flow"], "xtls-rprx-vision")
        metadata = json.loads((client_dir / "reality-edit.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["inbound"], saved)

    def test_edit_wizard_cancel_does_not_write_config_or_metadata(self) -> None:
        certificate, private_key = self.tls_material()
        inbound = self.edit_protocol_inbound("vless-grpc-tls", "cancel-edit", 22903, certificate, private_key)
        value = dict(self.original)
        value["inbounds"] = [inbound]
        dump(self.config, value)
        client_dir = self.state_dir / "clients"
        client_dir.mkdir(parents=True, exist_ok=True)
        dump(client_dir / "cancel-edit.json", {"server": "old.example", "public_key": "", "inbound": inbound})
        before_config = self.config.read_bytes()
        before_metadata = (client_dir / "cancel-edit.json").read_bytes()
        result = self.run_edit_wizard_with_test_prompts("cancel-edit", certificate, private_key, cancel=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((client_dir / "cancel-edit.json").read_bytes(), before_metadata)

    def test_prompt_secret_does_not_echo_input_or_default_and_preserves_empty_default(self) -> None:
        import select
        import time

        def run_secret(keys: bytes, default: str) -> tuple[str, str]:
            result_path = self.root / "prompt-secret-result"
            master, slave = pty.openpty()
            process = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c",
                 'source "$1"; prompt_secret answer SECRET "$2" && printf "%s" "$answer" > "$3"',
                 "prompt-secret", str(SCRIPT), default, str(result_path)],
                env=dict(self.env, TERM="xterm", INPUTRC="/dev/null"),
                stdin=slave, stdout=subprocess.PIPE, stderr=slave,
            )
            os.close(slave)
            transcript = b""
            try:
                deadline = time.monotonic() + 5
                while b"SECRET" not in transcript and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        transcript += os.read(master, 4096)
                self.assertIn(b"SECRET", transcript)
                os.write(master, keys)
                stdout, _ = process.communicate(timeout=5)
                transcript += stdout
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if not ready:
                        continue
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    transcript += chunk
                self.assertEqual(process.returncode, 0)
                return transcript.decode(errors="replace"), result_path.read_text(encoding="utf-8")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                os.close(master)

        transcript, value = run_secret(b"typed-secret\n", "default-secret")
        self.assertEqual(value, "typed-secret")
        self.assertNotIn("typed-secret", transcript)
        self.assertNotIn("default-secret", transcript)

        transcript, value = run_secret(b"typed-secrex\x7ft\n", "default-secret")
        self.assertEqual(value, "typed-secret")
        self.assertNotIn("typed-secret", transcript)
        self.assertNotIn("default-secret", transcript)

        transcript, value = run_secret(b"\n", "default-secret")
        self.assertEqual(value, "default-secret")
        self.assertNotIn("default-secret", transcript)

        transcript, value = run_secret(b"@new\n", "default-secret")
        self.assertEqual(value, "@new")
        self.assertNotIn("@new", transcript)

    def test_edit_cli_and_show_config_menu_six_route_to_edit_wizard(self) -> None:
        marker = self.core.parent / ".managed"
        marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        result = self.shell(
            'require_root() { return 0; }; require_systemd() { return 0; }; '
            'edit_wizard_locked() { printf "edit:%s\\n" "$1"; }; '
            'SB_ROOT_PREFIX=""; main edit "$1"',
            "node-to-edit", check=True,
        )
        self.assertIn("edit:node-to-edit", result.stdout)
        result = self.shell(
            'prompt() { printf -v "$1" "%s" 6; }; '
            'edit_wizard_locked() { printf "edit:%s\\n" "$1"; }; '
            'show_config',
            check=True,
        )
        self.assertIn("edit:", result.stdout)

    def test_generated_vless_grpc_reality_has_no_vision_flow(self) -> None:
        inbound = self.generate_inbound(
            "vless-grpc-reality", "b7efdc96-87aa-4910-81b4-374b2eecc737", "GrpcReality",
            "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
            "www.example.com",
        )
        self.assertEqual(inbound["type"], "vless")
        self.assertEqual(inbound["transport"], {"type": "grpc", "service_name": "GrpcReality"})
        self.assertNotIn("flow", inbound["users"][0])
        self.assertTrue(inbound["tls"]["reality"]["enabled"])
        self.assertEqual(inbound["tls"]["reality"]["short_id"], ["0123456789abcdef"])
        self.assertEqual(inbound["tls"]["alpn"], ["h2"])

    def test_extended_protocol_templates_have_expected_server_fields(self) -> None:
        certificate, private_key = self.tls_material()
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        cases = {
            "hysteria2": ("hy-password", "", {}, "h3"),
            "tuic": (uuid, "", {"uuid": uuid, "password": "tuic-password"}, "h3"),
            "anytls": ("anytls-password", "", {}, None),
            "vless-grpc-tls": (uuid, "GrpcService", {"uuid": uuid}, "h2"),
            "vless-ws-tls": (uuid, "/vless-tls", {"uuid": uuid}, None),
            "vless-tcp-tls": (uuid, "", {"uuid": uuid}, None),
            "vmess-tcp": (uuid, "", {"uuid": uuid}, None),
            "vmess-tcp-tls": (uuid, "", {"uuid": uuid}, None),
            "vmess-ws": (uuid, "/vmess", {"uuid": uuid}, None),
            "vmess-ws-tls": (uuid, "/vmess-tls", {"uuid": uuid}, None),
            "vmess-grpc-tls": (uuid, "VmessGrpc", {"uuid": uuid}, "h2"),
        }
        for protocol, (credential, extra, user, alpn) in cases.items():
            with self.subTest(protocol=protocol):
                tls = protocol.endswith("-tls") or protocol in {"hysteria2", "tuic", "anytls"}
                inbound = self.generate_inbound(
                    protocol, credential, extra, tls_server_name="example.com" if tls else "",
                    certificate=certificate if tls else None, key=private_key if tls else None,
                    tuic_password="tuic-password" if protocol == "tuic" else "",
                )
                expected_type = "hysteria2" if protocol == "hysteria2" else protocol.split("-", 1)[0]
                self.assertEqual(inbound["type"], expected_type)
                self.assertEqual(inbound["listen_port"], 18443)
                self.assertEqual(inbound["listen"], "::")
                if protocol == "hysteria2":
                    self.assertEqual(inbound["users"][0]["password"], credential)
                elif protocol == "tuic":
                    self.assertEqual(inbound["users"], [user])
                elif protocol == "anytls":
                    self.assertEqual(inbound["users"][0]["password"], credential)
                else:
                    self.assertEqual(inbound["users"][0]["uuid"], uuid)
                if "ws" in protocol:
                    self.assertEqual(inbound["transport"], {"type": "ws", "path": extra})
                if "grpc" in protocol:
                    self.assertEqual(inbound["transport"]["type"], "grpc")
                    self.assertEqual(inbound["transport"]["service_name"], extra)
                if protocol == "vless-tcp-tls":
                    self.assertNotIn("transport", inbound)
                    self.assertNotIn("flow", inbound["users"][0])
                if tls:
                    self.assertTrue(inbound["tls"]["enabled"])
                    self.assertEqual(inbound["tls"]["server_name"], "example.com")
                    self.assertEqual(inbound["tls"]["certificate_path"], str(certificate))
                    self.assertEqual(inbound["tls"]["key_path"], str(private_key))
                    self.assertNotIn("insecure", inbound["tls"])
                    if alpn:
                        self.assertEqual(inbound["tls"]["alpn"], [alpn])

    def test_extended_protocol_template_validation_rejects_unsafe_inputs(self) -> None:
        certificate, private_key = self.tls_material()
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        invalid = [
            ("vless-grpc-tls", uuid, "", "missing gRPC service name"),
            ("vless-ws-tls", uuid, "vless-tls", "WebSocket TLS path must start with slash"),
            ("vmess-ws", uuid, "vmess", "WebSocket path must start with slash"),
            ("vmess-grpc-tls", uuid, "grpc service", "gRPC service name contains spaces"),
        ]
        for protocol, credential, extra, reason in invalid:
            with self.subTest(reason=reason):
                args = (protocol, "invalid", "18443", credential, extra, "", "", "example.com", str(certificate), str(private_key), "")
                result = self.shell('generate_inbound "$@"', *args)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

        missing = self.shell(
            'generate_inbound "$@"', "vmess-tcp-tls", "missing-cert", "18443", uuid,
            "", "", "", "example.com", str(self.root / "no-cert.pem"), str(private_key), "",
        )
        self.assertNotEqual(missing.returncode, 0)

        other_key = self.root / "other-key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(other_key), "2048"],
            capture_output=True, text=True, check=True, timeout=20,
        )
        mismatch = self.shell(
            'generate_inbound "$@"', "vmess-tcp-tls", "mismatch", "18443", uuid,
            "", "", "", "example.com", str(certificate), str(other_key), "",
        )
        self.assertNotEqual(mismatch.returncode, 0)

        reality_grpc = self.shell(
            'generate_inbound "$@"', "vless-grpc-reality-vision", "reality-grpc", "18443", uuid,
            "GrpcService", "", "0123456789abcdef", "example.com", "", "", "",
        )
        self.assertNotEqual(reality_grpc.returncode, 0)

    def test_extended_client_exports_preserve_transport_tls_and_credentials(self) -> None:
        certificate, private_key = self.tls_material()
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        cases = {
            "hysteria2": ("hy-password", "", "h3"),
            "tuic": (uuid, "", "h3"),
            "anytls": ("anytls-password", "", None),
            "vless-grpc-tls": (uuid, "GrpcService", "h2"),
            "vless-ws-tls": (uuid, "/vless-tls", None),
            "vless-tcp-tls": (uuid, "", None),
            "vmess-tcp": (uuid, "", None),
            "vmess-tcp-tls": (uuid, "", None),
            "vmess-ws": (uuid, "/vmess", None),
            "vmess-ws-tls": (uuid, "/vmess-tls", None),
            "vmess-grpc-tls": (uuid, "VmessGrpc", "h2"),
        }
        for protocol, (credential, extra, alpn) in cases.items():
            with self.subTest(protocol=protocol):
                tls = protocol.endswith("-tls") or protocol in {"hysteria2", "tuic", "anytls"}
                inbound = self.generate_inbound(
                    protocol, credential, extra, tls_server_name="example.com" if tls else "",
                    certificate=certificate if tls else None, key=private_key if tls else None,
                    tuic_password="tuic-password" if protocol == "tuic" else "",
                )
                source = self.root / (protocol + "-source.json")
                dump(source, inbound)
                result = self.shell(
                    'generate_client "$1" "example.com"', str(source), check=True,
                )
                client = json.loads(result.stdout)
                self.assertEqual(client["server"], "example.com")
                self.assertEqual(client["server_port"], 18443)
                self.assertEqual(client["type"], inbound["type"])
                if protocol == "tuic":
                    self.assertEqual(client["uuid"], uuid)
                    self.assertEqual(client["password"], "tuic-password")
                elif protocol in {"hysteria2", "anytls"}:
                    self.assertEqual(client["password"], credential)
                else:
                    self.assertEqual(client["uuid"], uuid)
                if "transport" in inbound:
                    self.assertEqual(client["transport"], inbound["transport"])
                if protocol == "vless-tcp-tls":
                    self.assertNotIn("transport", client)
                    self.assertNotIn("flow", client)
                if tls:
                    self.assertTrue(client["tls"]["enabled"])
                    self.assertEqual(client["tls"]["server_name"], "example.com")
                    self.assertFalse(client["tls"].get("insecure", False))
                    self.assertNotIn("certificate_path", client["tls"])
                    self.assertNotIn("key_path", client["tls"])
                    if alpn:
                        self.assertEqual(client["tls"]["alpn"], [alpn])

    def test_extended_client_export_rejects_tls_without_verifiable_material(self) -> None:
        inbound = {
            "type": "vmess", "tag": "imported", "listen": "::", "listen_port": 18443,
            "users": [{"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737"}],
            "tls": {"enabled": True, "server_name": "example.com", "certificate_path": "/missing.pem", "key_path": "/missing.key"},
        }
        path = self.root / "unverifiable-tls.json"
        dump(path, inbound)
        result = self.shell('generate_client "$1" "example.com"', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_vless_grpc_reality_client_export_keeps_reality_without_flow(self) -> None:
        inbound = self.generate_inbound(
            "vless-grpc-reality", "b7efdc96-87aa-4910-81b4-374b2eecc737", "GrpcReality",
            "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
            "www.example.com",
        )
        source = self.root / "vless-grpc-reality-source.json"
        dump(source, inbound)
        result = self.shell(
            'generate_client "$1" "example.com" "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"',
            str(source), check=True,
        )
        client = json.loads(result.stdout)
        self.assertEqual(client["transport"], {"type": "grpc", "service_name": "GrpcReality"})
        self.assertTrue(client["tls"]["reality"]["enabled"])
        self.assertEqual(client["tls"]["alpn"], ["h2"])
        self.assertNotIn("flow", client)
        self.assertNotIn("private_key", json.dumps(client))

    def test_extended_client_links_encode_credentials_and_vmess_json(self) -> None:
        certificate, private_key = self.tls_material()
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        cases = {
            "hysteria2": ("hy-password", "", "hysteria2://"),
            "tuic": (uuid, "", "tuic://"),
            "anytls": ("anytls-password", "", "anytls://"),
            "vless-grpc-tls": (uuid, "GrpcService", "vless://"),
            "vless-ws-tls": (uuid, "/vless-tls", "vless://"),
            "vless-tcp-tls": (uuid, "", "vless://"),
            "vless-grpc-reality": (uuid, "GrpcReality", "vless://"),
            "vmess-tcp": (uuid, "", "vmess://"),
            "vmess-tcp-tls": (uuid, "", "vmess://"),
            "vmess-ws": (uuid, "/vmess", "vmess://"),
            "vmess-ws-tls": (uuid, "/vmess-tls", "vmess://"),
            "vmess-grpc-tls": (uuid, "VmessGrpc", "vmess://"),
        }
        for protocol, (credential, extra, prefix) in cases.items():
            with self.subTest(protocol=protocol):
                tls = protocol.endswith("-tls") or protocol in {"hysteria2", "tuic", "anytls"}
                if protocol == "vless-grpc-reality":
                    inbound = self.generate_inbound(
                        protocol, credential, extra,
                        "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw", "0123456789abcdef",
                        "www.example.com",
                    )
                else:
                    inbound = self.generate_inbound(
                        protocol, credential, extra, tls_server_name="example.com" if tls else "",
                        certificate=certificate if tls else None, key=private_key if tls else None,
                        tuic_password="tuic-password" if protocol == "tuic" else "",
                    )
                source = self.root / (protocol + "-link-source.json")
                dump(source, inbound)
                outbound = self.shell(
                    'generate_client "$1" "example.com" "$2"', str(source),
                    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", check=True,
                )
                outbound_path = self.root / (protocol + "-outbound.json")
                outbound_path.write_text(outbound.stdout, encoding="utf-8")
                link = self.shell('client_link "$1" "friendly name"', str(outbound_path), check=True).stdout.strip()
                self.assertTrue(link.startswith(prefix), link)
                self.assertNotIn("server-key.pem", link)
                if protocol == "vmess-tcp":
                    import base64
                    payload = base64.urlsafe_b64decode(link.removeprefix("vmess://") + "===")
                    value = json.loads(payload)
                    self.assertEqual(value["id"], uuid)
                    self.assertEqual(value["add"], "example.com")
                    self.assertEqual(value["port"], "18443")
                if protocol == "vless-grpc-reality":
                    self.assertIn("type=grpc", link)
                    self.assertIn("security=reality", link)
                    self.assertIn("serviceName=GrpcReality", link)
                    self.assertNotIn("flow=", link)
                if protocol == "vless-ws-tls":
                    self.assertIn("type=ws", link)
                    self.assertIn("security=tls", link)
                    self.assertIn("sni=example.com", link)
                    self.assertIn("path=%2Fvless-tls", link)
                if protocol == "vless-tcp-tls":
                    self.assertIn("type=tcp", link)
                    self.assertIn("security=tls", link)
                    self.assertIn("sni=example.com", link)
                    self.assertNotIn("path=", link)
                    self.assertNotIn("serviceName=", link)
                    self.assertNotIn("flow=", link)

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
        certificate, private_key = self.tls_material()
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        protocols = {
            "vless-tcp": (uuid, "", "", "", "", None, None, ""),
            "vless-ws": (uuid, "/vless", "", "", "", None, None, ""),
            "vless-reality": (
                uuid, "www.example.com", "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw",
                "0123456789abcdef", "", None, None, "",
            ),
            "vless-grpc-reality": (
                uuid, "GrpcReality", "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw",
                "0123456789abcdef", "www.example.com", None, None, "",
            ),
            "shadowsocks": ("8JCsPssfgS8tiRwiMlhARg==", "", "", "", "", None, None, ""),
            "hysteria2": ("hy-password", "", "", "", "example.com", certificate, private_key, ""),
            "tuic": (uuid, "", "", "", "example.com", certificate, private_key, "tuic-password"),
            "anytls": ("anytls-password", "", "", "", "example.com", certificate, private_key, ""),
            "vless-grpc-tls": (uuid, "GrpcService", "", "", "example.com", certificate, private_key, ""),
            "vless-ws-tls": (uuid, "/vless-tls", "", "", "example.com", certificate, private_key, ""),
            "vless-tcp-tls": (uuid, "", "", "", "example.com", certificate, private_key, ""),
            "vmess-tcp": (uuid, "", "", "", "", None, None, ""),
            "vmess-tcp-tls": (uuid, "", "", "", "example.com", certificate, private_key, ""),
            "vmess-ws": (uuid, "/vmess", "", "", "", None, None, ""),
            "vmess-ws-tls": (uuid, "/vmess-tls", "", "", "example.com", certificate, private_key, ""),
            "vmess-grpc-tls": (uuid, "VmessGrpc", "", "", "example.com", certificate, private_key, ""),
        }
        for protocol, fields in protocols.items():
            with self.subTest(protocol=protocol):
                credential, extra, private, short_id, tls_name, cert, key, tuic_password = fields
                result = self.shell(
                    'generate_inbound "$@"', protocol, "real-test", "18443", credential,
                    extra, private, short_id, tls_name, str(cert or ""), str(key or ""), tuic_password,
                    check=True,
                )
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
