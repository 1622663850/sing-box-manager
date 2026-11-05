#!/usr/bin/env python3
"""Additional isolated coverage for Xray manager operations.

These tests use the existing XrayFixture and therefore never touch a live
configuration, systemd unit, network listener, or production credential.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import unittest

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no POSIX PTY module.
    pty = None

import test_xray_manager as fixture


UUID = fixture.UUID
UUID2 = fixture.UUID2
PUBLIC_KEY = fixture.PUBLIC_KEY


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"),
                     "bash and jq are required")
class XrayFeatureTest(fixture.XrayFixture):
    """Behavioral checks for management functions not covered by core tests."""

    def test_service_actions_status_diagnose_and_logs(self) -> None:
        node = self.write_node("status-node", 18501)
        self.write_config([node])
        self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)

        self.state(active=False, enabled=False)
        self.assertEqual(self.shell("service_action start", check=True).returncode, 0)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.shell("service_action enable", check=True).returncode, 0)
        self.assertTrue(self.state()["enabled"])
        self.assertEqual(self.shell("service_action disable-autostart", check=True).returncode, 0)
        self.assertFalse(self.state()["enabled"])
        self.assertEqual(self.shell("service_action stop", check=True).returncode, 0)
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.shell("service_action disable", check=True).returncode, 0)
        self.assertFalse(self.state()["active"])

        status = self.shell("show_status", check=True)
        self.assertIn("Xray 1.8.24", status.stdout)
        self.assertIn("inactive", status.stdout)

        # journalctl is an isolated fixture command, so this verifies the
        # log entrypoint without depending on the host's journal.
        journalctl = self.bin / "journalctl"
        journalctl.write_text("#!/usr/bin/env bash\nprintf 'fixture-log\\n'\n", encoding="utf-8")
        journalctl.chmod(0o755)
        logs = self.shell("show_logs", check=True)
        self.assertEqual(logs.stdout, "fixture-log\n")
        diagnostic = self.shell("diagnose")
        self.assertEqual(diagnostic.returncode, 1)  # service is stopped
        self.assertIn("诊断不检测公网", diagnostic.stdout)

    def test_default_server_clear_and_client_exports_all_formats(self) -> None:
        node = self.write_node("export-node", 18502)
        self.write_config([node])
        self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)
        backups_before = sorted(self.backups_dir.iterdir())

        self.assertEqual(self.shell('set_default_server "$1"', "example.com", check=True).returncode, 0)
        self.assertEqual(self.default_server.read_text(encoding="utf-8").strip(), "example.com")
        self.assertEqual(self.shell('read_default_server', check=True).stdout, "example.com")

        for suffix, fmt in (("links.txt", "links"), ("clients.json", "json"), ("clients.b64", "base64")):
            target = self.root / suffix
            result = self.shell('exports "$1" "$2"', fmt, str(target), check=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            raw = target.read_bytes()
            if fmt == "links":
                self.assertTrue(raw.startswith(b"vless://"))
            elif fmt == "json":
                self.assertEqual(json.loads(raw)["outbounds"][0]["protocol"], "vless")
            else:
                self.assertIn(b"vless://", base64.b64decode(raw))

        self.assertEqual(self.shell('set_default_server -', check=True).returncode, 0)
        self.assertFalse(self.default_server.exists())
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)

    def test_jsonc_format_import_and_backup_restore_round_trip(self) -> None:
        original = self.write_node("jsonc-node", 18503)
        self.write_config([original])
        source = self.config.read_text(encoding="utf-8").replace(
            "{\n", "{\n  // preserve this manager comment\n", 1
        )
        compact = json.dumps(json.loads(source.replace("  // preserve this manager comment\n", "")), separators=(",", ":"))
        self.config.write_text("// preserve this manager comment\n" + compact + "\n", encoding="utf-8")
        formatted = self.shell("format_config_locked", check=True)
        self.assertEqual(formatted.returncode, 0, formatted.stdout + formatted.stderr)
        self.assertIn("preserve this manager comment", self.config.read_text(encoding="utf-8"))
        self.assertEqual(list(self.backups_dir.iterdir()), [])

        manual = self.shell("create_backup", check=True)
        bundle = self.backup_path(manual)
        self.assertTrue(bundle.is_file())
        backups_before_import = sorted(self.backups_dir.iterdir())

        imported = self.write_node("imported-node", 18504)
        imported_file = self.root / "import.jsonc"
        imported_file.write_text(
            '{\n // import comment\n "inbounds": [' + json.dumps(imported) + '],\n'
            ' "outbounds": [{"protocol":"freedom","tag":"direct","settings":{}}]\n}\n',
            encoding="utf-8",
        )
        self.assertEqual(self.shell('import_config "$1"', str(imported_file), check=True).returncode, 0)
        imported_value = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual(imported_value["inbounds"][0]["tag"], "imported-node")
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before_import)
        restored = self.shell('confirm() { return 0; }; restore_backup "$1"', str(bundle), check=True)
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before_import)
        restored_value = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual(restored_value["inbounds"][0]["tag"], "jsonc-node")
        self.assertIn("preserve this manager comment", self.config.read_text(encoding="utf-8"))

    def test_user_management_add_delete_and_last_user_guard(self) -> None:
        node = self.write_node("users", 18505)
        self.write_config([node])
        self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)
        self.env["XR_TEST_USER_CREDENTIAL"] = UUID2
        backups_before = sorted(self.backups_dir.iterdir())

        add = self.shell(
            r'''prompt() { if [[ $1 == name ]]; then printf -v "$1" user-two; fi; }
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_USER_CREDENTIAL"; }
confirm() { return 0; }
change_inbound_user add "$1"''',
            node["tag"], check=True,
        )
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        users = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["users"]
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]["email"], "user-two")
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        listing = self.shell('list_inbound_users "$1"', node["tag"], check=True)
        self.assertIn("user-two", listing.stdout)

        delete = self.shell(
            r'''prompt() { printf -v "$1" 2; }
confirm() { return 0; }
change_inbound_user delete "$1"''',
            node["tag"], check=True,
        )
        self.assertEqual(delete.returncode, 0, delete.stdout + delete.stderr)
        users = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        last = self.shell('change_inbound_user delete "$1"', node["tag"])
        self.assertNotEqual(last.returncode, 0)
        self.assertIn("最后一个用户", last.stderr)

    def _exercise_user_protocol(self, protocol: str) -> None:
        """Run the same add/delete/last-user contract for every user table."""
        for path in self.clients_dir.glob("*.json"):
            path.unlink()
        for path in self.backups_dir.glob("*"):
            if path.is_file():
                path.unlink()
        self.system_log.unlink(missing_ok=True)
        self.state(active=False, enabled=False, restart_failures=0, start_failures=0)

        if protocol in {"vless", "vmess"}:
            node = self.write_node("users-" + protocol, 18510, protocol=protocol)
            credential = UUID2
        elif protocol == "trojan":
            node = self.write_node("users-trojan", 18511, protocol="trojan", credential="trojan-two")
            credential = "trojan-added-password"
        else:
            node = self.write_hy2("users-hysteria", 18512)
            credential = "hy2-added-password"
        self.write_config([node])
        metadata = self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)
        self.env["XR_TEST_USER_CREDENTIAL"] = credential
        backups_before = sorted(self.backups_dir.iterdir())

        add = self.shell(
            r'''prompt() { if [[ $1 == name ]]; then printf -v "$1" '%s' added-user; fi; }
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_USER_CREDENTIAL"; }
confirm() { return 0; }
change_inbound_user add "$1"''',
            node["tag"], check=True,
        )
        self.assertEqual(add.returncode, 0, protocol + ": " + add.stdout + add.stderr)
        current = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        users = current["settings"]["users"]
        self.assertEqual(len(users), 2)
        self.assertEqual(users[1]["email"], "added-user")
        key = {"vless": "id", "vmess": "id", "trojan": "password", "hysteria": "auth"}[protocol]
        self.assertEqual(users[1][key], credential)
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], current)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        self.assertIn("added-user", self.shell('list_inbound_users "$1"', node["tag"], check=True).stdout)

        delete = self.shell(
            r'''prompt() { printf -v "$1" 2; }
confirm() { return 0; }
change_inbound_user delete "$1"''',
            node["tag"], check=True,
        )
        self.assertEqual(delete.returncode, 0, protocol + ": " + delete.stdout + delete.stderr)
        current = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        self.assertEqual(len(current["settings"]["users"]), 1)
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], current)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        last = self.shell('change_inbound_user delete "$1"', node["tag"])
        self.assertNotEqual(last.returncode, 0)
        self.assertIn("最后一个用户", last.stderr)

    def test_user_management_covers_vmess_trojan_and_hysteria_tables(self) -> None:
        for protocol in ("vmess", "trojan", "hysteria"):
            with self.subTest(protocol=protocol):
                self._exercise_user_protocol(protocol)

    def test_outbound_add_bind_unbind_delete_and_reference_guard(self) -> None:
        node = self.write_node("route-node", 18506)
        self.write_config([node])
        self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)
        backups_before = sorted(self.backups_dir.iterdir())
        outbound_file = self.root / "outbound.json"
        fixture.dump(outbound_file, [{
            "protocol": "freedom", "tag": "remote-direct", "settings": {},
        }])
        added = self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(outbound_file), check=True)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        tags = json.loads(self.shell("config_jq '[.outbounds[].tag]'", check=True).stdout)
        self.assertIn("remote-direct", tags)

        bound = self.shell('confirm() { return 0; }; bind_outbound_locked "$1" "$2"', node["tag"], "remote-direct", check=True)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        blocked = self.shell('confirm() { return 0; }; delete_outbound_locked "$1"', "remote-direct")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("被路由", blocked.stderr)
        unbound = self.shell('confirm() { return 0; }; bind_outbound_locked "$1" -', node["tag"], check=True)
        self.assertEqual(unbound.returncode, 0, unbound.stdout + unbound.stderr)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        deleted = self.shell('confirm() { return 0; }; delete_outbound_locked "$1"', "remote-direct", check=True)
        self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        self.assertNotIn("remote-direct", json.loads(self.shell("config_jq '[.outbounds[].tag]'", check=True).stdout))

    def test_outbound_wizard_adds_and_removes_every_protocol_template(self) -> None:
        """Drive all eight outbound wizard choices through save and cleanup."""
        cases = (
            ("1", "vless", "vless", "credential"),
            ("2", "vmess", "vmess", "credential"),
            ("3", "trojan", "trojan", "credential"),
            ("4", "shadowsocks", "shadowsocks", "credential"),
            ("5", "hysteria", "hysteria", "credential"),
            ("6", "socks", "socks", "username"),
            ("7", "http", "http", "username"),
            ("8", "freedom", "freedom", "none"),
        )
        for choice, tag_suffix, protocol, credential_kind in cases:
            with self.subTest(protocol=protocol):
                tag = "wizard-" + tag_suffix
                port = str(20500 + int(choice))
                backups_before = sorted(self.backups_dir.iterdir())
                self.env.update({
                    "XR_TEST_OUTBOUND_CHOICE": choice,
                    "XR_TEST_OUTBOUND_TAG": tag,
                    "XR_TEST_OUTBOUND_PORT": port,
                    "XR_TEST_OUTBOUND_CREDENTIAL": "test-credential-" + tag_suffix,
                })
                driver = r'''prompt() {
    local variable=$1 message=${2:-} default=${3:-}
    case "$variable" in
      choice)
        case "$message" in
          *选择协议*) printf -v "$variable" '%s' "$XR_TEST_OUTBOUND_CHOICE";;
          *传输*) printf -v "$variable" 1;;
          *安全*) printf -v "$variable" 1;;
          *VLESS\ Encryption*) printf -v "$variable" 1;;
          *REALITY\ ML-DSA*) printf -v "$variable" 1;;
          *Vision*) printf -v "$variable" 2;;
          *) printf -v "$variable" 1;;
        esac;;
      tag) printf -v "$variable" '%s' "$XR_TEST_OUTBOUND_TAG";;
      server|sni) printf -v "$variable" 127.0.0.1;;
      port) printf -v "$variable" '%s' "$XR_TEST_OUTBOUND_PORT";;
      username) printf -v "$variable" '';;
      method) printf -v "$variable" 2022-blake3-aes-128-gcm;;
      path) printf -v "$variable" /wizard;;
      *) printf -v "$variable" '%s' "$default";;
    esac
}
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_OUTBOUND_CREDENTIAL"; }
confirm() { return 0; }
add_outbound_wizard_locked'''
                added = self.shell(driver, check=True)
                self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
                self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
                node = json.loads(self.shell(
                    "config_jq --arg t \"$1\" '.outbounds[]|select(.tag==$t)'", tag, check=True,
                ).stdout)
                self.assertEqual(node["protocol"], protocol)
                if protocol in {"vless", "vmess"}:
                    self.assertEqual(node["settings"]["id"], self.env["XR_TEST_OUTBOUND_CREDENTIAL"])
                    self.assertEqual(node["streamSettings"]["method"], "raw")
                    self.assertEqual(node["streamSettings"]["security"], "none")
                    if protocol == "vless":
                        self.assertEqual(node["settings"]["encryption"], "none")
                    else:
                        self.assertEqual(node["settings"]["security"], "auto")
                elif protocol == "trojan":
                    self.assertEqual(node["settings"]["password"], self.env["XR_TEST_OUTBOUND_CREDENTIAL"])
                    self.assertEqual(node["streamSettings"]["security"], "tls")
                    self.assertEqual(node["streamSettings"]["tlsSettings"]["serverName"], "127.0.0.1")
                elif protocol == "shadowsocks":
                    self.assertEqual(node["settings"]["method"], "2022-blake3-aes-128-gcm")
                    self.assertEqual(node["settings"]["password"], self.env["XR_TEST_OUTBOUND_CREDENTIAL"])
                elif protocol == "hysteria":
                    self.assertEqual(node["settings"]["version"], 2)
                    self.assertEqual(node["streamSettings"]["method"], "hysteria")
                    self.assertEqual(node["streamSettings"]["security"], "tls")
                    self.assertEqual(node["streamSettings"]["hysteriaSettings"]["auth"], self.env["XR_TEST_OUTBOUND_CREDENTIAL"])
                elif protocol in {"socks", "http"}:
                    server = node["settings"]["servers"][0]
                    self.assertEqual(server["address"], "127.0.0.1")
                    self.assertEqual(server["port"], int(port))
                else:
                    self.assertEqual(node["settings"], {})
                deleted = self.shell('confirm() { return 0; }; delete_outbound_locked "$1"', tag, check=True)
                self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
                self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
                remaining = json.loads(self.shell("config_jq '[.outbounds[].tag]'", check=True).stdout)
                self.assertNotIn(tag, remaining)

    def test_ipv4_preference_writes_only_freedom_and_ipv6_success_path(self) -> None:
        node = self.write_node("ip-node", 18507)
        self.write_config([node])
        self.state(active=True, enabled=False)
        before = json.loads(self.config.read_text(encoding="utf-8"))
        backups_before = sorted(self.backups_dir.iterdir())
        ipv4 = self.shell('confirm() { return 0; }; set_outbound_preference ipv4', check=True)
        self.assertEqual(ipv4.returncode, 0, ipv4.stdout + ipv4.stderr)
        value = json.loads(self.config.read_text(encoding="utf-8"))
        freedom = next(o for o in value["outbounds"] if o["protocol"] == "freedom")
        self.assertEqual(freedom["streamSettings"]["sockopt"]["domainStrategy"], "UseIPv4v6")
        self.assertEqual(value["inbounds"], before["inbounds"])
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)
        self.env["XR_MOCK_CURL_MODE"] = "success"
        ipv6 = self.shell('confirm() { return 0; }; set_outbound_preference ipv6', check=True)
        self.assertEqual(ipv6.returncode, 0, ipv6.stdout + ipv6.stderr)
        value = json.loads(self.config.read_text(encoding="utf-8"))
        freedom = next(o for o in value["outbounds"] if o["protocol"] == "freedom")
        self.assertEqual(freedom["streamSettings"]["sockopt"]["domainStrategy"], "UseIPv6v4")
        self.assertEqual(self.state()["active"], True)
        self.assertEqual(sorted(self.backups_dir.iterdir()), backups_before)

    def _pty_menu_cancel(self, function: str) -> str:
        """Drive a real prompt/read loop through a Linux PTY, sending 0."""
        if os.name == "nt" or pty is None:
            self.skipTest("PTY menu verification requires Linux")
        master, slave = pty.openpty()
        command = 'source "$1"; ' + function
        process = subprocess.Popen(
            ["bash", "-c", command, "xray-menu-test", str(fixture.SCRIPT)],
            env=self.env, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
        os.close(slave)
        chunks: list[bytes] = []
        try:
            os.write(master, b"0\n")
            while process.poll() is None:
                ready, _, _ = select.select([master], [], [], 5)
                if ready:
                    try:
                        chunks.append(os.read(master, 65536))
                    except OSError:
                        break
            while True:
                ready, _, _ = select.select([master], [], [], 0)
                if not ready:
                    break
                try:
                    chunks.append(os.read(master, 65536))
                except OSError:
                    break
            if process.poll() is None:
                process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            os.close(master)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def test_all_menu_entrypoints_return_on_cancel_without_writes(self) -> None:
        node = self.write_node("menu-node", 18508)
        self.write_config([node])
        before = self.config.read_bytes()
        write_actions = {"start", "restart", "stop", "enable", "disable",
                         "daemon-reload", "reset-failed"}
        chooser = self.shell('prompt() { printf -v "$1" 0; }; choose_template selected')
        self.assertEqual(chooser.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        menus = (
            ("menu", "Xray 管理面板"),
            ("show_config", "查看 Xray 配置"),
            ("management_menu", "备份、恢复、导出和诊断"),
            ("bbr_menu", "BBR 设置"),
            ("autostart_menu", "开机自启"),
            ("logs_menu", "查看 Xray 日志"),
            ("uninstall_menu", "卸载 Xray"),
            ("outbound_preference_menu", "出站 IP 优先级"),
            ("outbounds_menu", "Xray 出站管理"),
            ("users_menu", "入站用户管理"),
        )
        for function, expected in menus:
            with self.subTest(function=function):
                output = self._pty_menu_cancel(function)
                self.assertIn(expected, output)
                self.assertEqual(self.config.read_bytes(), before)
                writes = [call for call in self.calls() if any(action in call for action in write_actions)]
                self.assertEqual(writes, [], "cancelled menu attempted a systemctl write: " + repr(writes))


if __name__ == "__main__":
    unittest.main()
