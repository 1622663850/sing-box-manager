#!/usr/bin/env python3
"""Isolated regression coverage for per-user Xray outbound routing.

The tests intentionally exercise the manager's transaction seam.  They never
touch a live Xray configuration, systemd unit, or production credential.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest

if os.name != "nt":
    import fcntl
    import pty
    import select
    import struct
    import termios

import test_xray_manager as fixture


UUID = fixture.UUID
UUID2 = fixture.UUID2
UUID3 = "52d3c6f4-21c2-4dd1-9fb6-3c5d6f5b6ce7"
PUBLIC_KEY = fixture.PUBLIC_KEY


def identity(protocol: str, credential: str) -> str:
    return hashlib.sha256((protocol + "\0" + credential).encode("utf-8")).hexdigest()


def user(protocol: str, credential: str, email: str | None = None) -> dict:
    key = {"vless": "id", "vmess": "id", "trojan": "password", "hysteria": "auth"}[protocol]
    value = {key: credential}
    if email is not None:
        value["email"] = email
    return value


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and fixture.SCRIPT.is_file(),
                     "bash, jq, and xray-manager.sh are required")
class XrayUserOutboundRouteTest(fixture.XrayFixture):
    """Per-user bindings must remain isolated from node-wide routing."""

    def setUp(self) -> None:
        super().setUp()
        self.outbounds = [
            {"protocol": "freedom", "tag": "direct", "settings": {}},
            {"protocol": "freedom", "tag": "exit-a", "settings": {}},
            {"protocol": "freedom", "tag": "exit-b", "settings": {}},
        ]
        self.unrelated_rule = {"type": "field", "domain": ["example.com"], "outboundTag": "direct"}

    def make_node(self, protocol: str = "vless", *, anonymous: bool = False) -> dict:
        if protocol in {"vless", "vmess"}:
            users = [user(protocol, UUID, None if anonymous else "user-a"),
                     user(protocol, UUID2, "user-b")]
        elif protocol == "trojan":
            users = [user(protocol, "trojan-a", None if anonymous else "user-a"),
                     user(protocol, "trojan-b", "user-b")]
        elif protocol == "hysteria":
            users = [user(protocol, "hy-a", None if anonymous else "user-a"),
                     user(protocol, "hy-b", "user-b")]
        else:
            raise ValueError(protocol)
        node = {
            "protocol": protocol,
            "tag": "user-route-node",
            "listen": "127.0.0.1",
            "port": 18600,
            "settings": {"users": users},
            "streamSettings": {"method": "raw", "security": "none"},
        }
        if protocol == "hysteria":
            node["listen"] = "::"
            node["settings"]["version"] = 2
            node["streamSettings"] = {"method": "hysteria", "security": "tls"}
        return node

    def seed(self, node: dict | None = None, *, rules: list[dict] | None = None) -> Path:
        node = node or self.make_node()
        self.write_config(
            [node],
            outbounds=copy.deepcopy(self.outbounds),
            routing={"domainStrategy": "AsIs", "rules": copy.deepcopy(rules or [self.unrelated_rule])},
        )
        return self.write_metadata(node, server="example.com", public_key=PUBLIC_KEY)

    def bind(self, index: int, outbound: str, *, check: bool = True):
        return self.shell(
            'confirm() { return 0; }; bind_user_outbound_locked "$@"',
            "user-route-node", str(index), outbound, check=check,
        )

    def unbind(self, index: int, *, check: bool = True):
        return self.bind(index, "-", check=check)

    def snapshot_bytes(self) -> tuple[bytes, dict[str, bytes], list[list[str]]]:
        metadata = {str(p): p.read_bytes() for p in self.clients_dir.glob("*.json")}
        return self.config.read_bytes(), metadata, self.calls()

    def routes(self) -> list[dict]:
        return json.loads(self.shell("config_jq '.routing.rules'", check=True).stdout)

    def metadata(self) -> dict:
        return json.loads((self.clients_dir / "user-route-node.json").read_text(encoding="utf-8"))

    def test_two_users_bind_to_different_outbounds_and_unbind_one(self) -> None:
        self.seed()
        before_backups = sorted(self.backups_dir.iterdir())

        self.bind(1, "exit-a")
        self.bind(2, "exit-b")
        rules = self.routes()
        self.assertEqual(rules[0]["user"], ["user-b"])
        self.assertEqual(rules[0]["outboundTag"], "exit-b")
        self.assertEqual(rules[1]["user"], ["user-a"])
        self.assertEqual(rules[1]["outboundTag"], "exit-a")
        self.assertIn(self.unrelated_rule, rules)

        meta = self.metadata()
        routes = meta["user_outbound_routes"]
        self.assertEqual(routes[identity("vless", UUID)]["rule"]["outboundTag"], "exit-a")
        self.assertEqual(routes[identity("vless", UUID2)]["rule"]["outboundTag"], "exit-b")
        self.assertEqual(sorted(self.backups_dir.iterdir()), before_backups)

        self.unbind(1)
        rules = self.routes()
        self.assertFalse(any(rule.get("user") == ["user-a"] for rule in rules))
        self.assertTrue(any(rule.get("user") == ["user-b"] and rule.get("outboundTag") == "exit-b" for rule in rules))
        self.assertIn(self.unrelated_rule, rules)
        self.assertNotIn(identity("vless", UUID), self.metadata()["user_outbound_routes"])
        self.assertIn(identity("vless", UUID2), self.metadata()["user_outbound_routes"])
        self.assertEqual(sorted(self.backups_dir.iterdir()), before_backups)

    def test_new_inbound_default_user_add_second_edit_and_unbind(self) -> None:
        self.write_config([], outbounds=self.outbounds, routing={"rules": []})
        driver = r'''prompt() {
    case "$1" in
        protocol_choice) printf -v "$1" 2;;
        tag) printf -v "$1" user-route-node;;
        port) printf -v "$1" 18600;;
        server) printf -v "$1" example.com;;
        choice) if [[ $2 == *"选择用户"* ]]; then printf -v "$1" 2; else printf -v "$1" '%s' "${3:-}"; fi;;
        *) printf -v "$1" '%s' "${3:-}";;
    esac
}
confirm() { return 0; }
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_USER_CREDENTIAL"; }
add_wizard_locked'''
        self.env["XR_TEST_USER_CREDENTIAL"] = UUID
        self.shell(driver, check=True)
        first = json.loads(self.config.read_text())["inbounds"][0]["settings"]["users"][0]
        self.assertEqual(first, {"id": UUID, "email": "user1"})
        added = r'''prompt() { printf -v "$1" '%s' "$XR_TEST_USER_NAME"; }
prompt_secret() { printf -v "$1" '%s' "$XR_TEST_USER_CREDENTIAL"; }
confirm() { return 0; }
change_inbound_user add user-route-node'''
        self.env.update(XR_TEST_USER_NAME="user2", XR_TEST_USER_CREDENTIAL=UUID2)
        self.shell(added, check=True)
        users = json.loads(self.config.read_text())["inbounds"][0]["settings"]["users"]
        self.assertEqual(users, [first, {"id": UUID2, "email": "user2"}])
        self.env.update(XR_TEST_USER_NAME="user1", XR_TEST_USER_CREDENTIAL=UUID3)
        before = self.snapshot_bytes()
        duplicate = self.shell(added)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("用户名已存在", duplicate.stderr)
        self.assertEqual(self.snapshot_bytes(), before)
        self.bind(1, "exit-a")
        self.bind(2, "exit-b")
        self.env["XR_TEST_USER_CREDENTIAL"] = UUID2
        edit = driver.replace("port) printf -v \"$1\" 18600", "port) printf -v \"$1\" 18602")
        edit = edit.replace("add_wizard_locked", "edit_wizard_locked user-route-node")
        self.shell(edit, check=True)
        self.assertEqual(json.loads(self.config.read_text())["inbounds"][0]["settings"]["users"], users)
        self.unbind(2)
        self.assertTrue(any(r.get("user") == ["user1"] for r in self.routes()))
        self.assertFalse(any(r.get("user") == ["user2"] for r in self.routes()))
        self.assertEqual(sorted(self.backups_dir.iterdir()), [])

    def test_interactive_wizard_selects_node_user_outbound_and_unbinds(self) -> None:
        self.seed()
        prompt = r'''prompt() {
    case "$2" in
        *"选择用户所属节点"*) printf -v "$1" 1;;
        *"选择用户（"*) printf -v "$1" 1;;
        *"选择出站"*) printf -v "$1" 2;;
        *) printf -v "$1" "${3:-0}";;
    esac
}
confirm() { return 0; }
user_outbound_wizard_locked bind'''
        bound = self.shell(prompt, check=True)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        self.assertTrue(any(rule.get("user") == ["user-a"] and rule.get("outboundTag") == "exit-a" for rule in self.routes()))
        unbound = self.shell(prompt.replace("user_outbound_wizard_locked bind", "user_outbound_wizard_locked unbind"), check=True)
        self.assertEqual(unbound.returncode, 0, unbound.stdout + unbound.stderr)
        self.assertFalse(any(rule.get("user") == ["user-a"] for rule in self.routes()))
        self.assertNotIn("user_outbound_routes", self.metadata())

    def test_anonymous_user_gets_email_without_changing_credentials_or_other_user(self) -> None:
        node = self.make_node(anonymous=True)
        original = copy.deepcopy(node)
        metadata = self.seed(node)
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        metadata_value["xhttp_split"] = {"upload": "upload-out", "download": "download-out"}
        metadata_value["future_field"] = {"keep": True}
        fixture.dump(metadata, metadata_value)
        self.bind(1, "exit-a")
        current = json.loads(self.shell("config_jq '.inbounds[0]'", check=True).stdout)
        self.assertEqual(current["settings"]["users"][0]["id"], original["settings"]["users"][0]["id"])
        self.assertEqual(current["settings"]["users"][1], original["settings"]["users"][1])
        email = current["settings"]["users"][0].get("email", "")
        self.assertTrue(email and not email.startswith("regexp:"))
        route = next(rule for rule in self.routes() if rule.get("outboundTag") == "exit-a")
        self.assertEqual(route["user"], [email])
        key = identity("vless", UUID)
        self.assertEqual(self.metadata()["user_outbound_routes"][key]["identity"], key)
        self.assertEqual(self.metadata()["xhttp_split"], metadata_value["xhttp_split"])
        self.assertEqual(self.metadata()["future_field"], metadata_value["future_field"])

    def test_bind_supports_vmess_trojan_and_hysteria_and_rejects_ss(self) -> None:
        for protocol in ("vmess", "trojan", "hysteria"):
            with self.subTest(protocol=protocol):
                self.config.write_text(json.dumps(self.original) + "\n", encoding="utf-8")
                for path in self.clients_dir.glob("*.json"):
                    path.unlink()
                node = self.make_node(protocol)
                self.seed(node)
                result = self.bind(1, "exit-a")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                route = next(rule for rule in self.routes() if rule.get("outboundTag") == "exit-a")
                self.assertEqual(route["user"], ["user-a"])

        node = {
            "protocol": "shadowsocks", "tag": "user-route-node", "listen": "127.0.0.1", "port": 18601,
            "settings": {"method": "2022-blake3-aes-128-gcm", "password": "AAAAAAAAAAAAAAAAAAAAAA==", "network": "tcp,udp"},
            "streamSettings": {"method": "raw", "security": "none"},
        }
        self.seed(node)
        rejected = self.bind(1, "exit-a", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.routes(), [self.unrelated_rule])

    def test_duplicate_or_regexp_email_is_rejected_without_writes(self) -> None:
        duplicate = self.make_node()
        duplicate["settings"]["users"][0]["email"] = "same-user"
        duplicate["settings"]["users"][1]["email"] = "same-user"
        self.seed(duplicate)
        before = self.snapshot_bytes()
        rejected = self.bind(1, "exit-a", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

        regexp = self.make_node()
        regexp["settings"]["users"][0]["email"] = "regexp:^user-"
        regexp["settings"]["users"][1]["email"] = "user-b"
        self.seed(regexp)
        before = self.snapshot_bytes()
        rejected = self.bind(1, "exit-a", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

    def test_external_rule_edit_and_duplicate_rule_are_rejected(self) -> None:
        self.seed()
        self.bind(1, "exit-a")
        edited = json.loads(self.config.read_text(encoding="utf-8"))
        edited["routing"]["rules"][0]["outboundTag"] = "exit-b"
        self.config.write_text(json.dumps(edited) + "\n", encoding="utf-8")
        before = self.snapshot_bytes()
        rejected = self.unbind(1, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

        self.seed()
        duplicate = {"type": "field", "inboundTag": ["user-route-node"], "user": ["user-a"], "outboundTag": "exit-a"}
        current = json.loads(self.config.read_text(encoding="utf-8"))
        current["routing"]["rules"].insert(0, duplicate)
        self.config.write_text(json.dumps(current) + "\n", encoding="utf-8")
        rejected = self.bind(1, "exit-a", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.routes()[0], duplicate)

    def test_duplicate_owned_user_rule_is_rejected_without_deleting_external_rules(self) -> None:
        self.seed()
        self.bind(1, "exit-a")
        current = json.loads(self.config.read_text(encoding="utf-8"))
        own_rule = next(rule for rule in current["routing"]["rules"] if rule.get("user") == ["user-a"])
        current["routing"]["rules"].append(copy.deepcopy(own_rule))
        self.config.write_text(json.dumps(current) + "\n", encoding="utf-8")
        before = self.snapshot_bytes()
        rejected = self.bind(1, "exit-b", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

    def test_node_route_bind_keeps_user_rule_first_and_other_rule_order(self) -> None:
        other = {"type": "field", "domain": ["other.example"], "outboundTag": "direct"}
        self.seed(rules=[self.unrelated_rule, other])
        self.bind(1, "exit-a")
        before = self.routes()
        whole = self.shell('confirm() { return 0; }; bind_outbound_locked "$@"', "user-route-node", "exit-b", check=True)
        self.assertEqual(whole.returncode, 0, whole.stdout + whole.stderr)
        after = self.routes()
        self.assertEqual(after[0].get("user"), ["user-a"])
        self.assertEqual(after[0].get("outboundTag"), "exit-a")
        self.assertEqual(after[1], {"type": "field", "inboundTag": ["user-route-node"], "outboundTag": "exit-b"})
        self.assertEqual(after[2:], before[1:])

    def test_expected_node_snapshot_and_restart_failure_rollback_are_safe(self) -> None:
        node = self.make_node()
        self.seed(node)
        expected = copy.deepcopy(node)
        expected["port"] = 18611
        expected_path = self.candidate(expected, "stale-user-route-node.json")
        before = self.snapshot_bytes()
        stale = self.shell(
            'confirm() { return 0; }; bind_user_outbound_locked "$@"',
            node["tag"], "1", "exit-a", str(expected_path), check=False,
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

        self.state(active=True, restart_failures=1)
        before_config, before_meta = self.config.read_bytes(), {str(p): p.read_bytes() for p in self.clients_dir.glob("*.json")}
        failed = self.bind(1, "exit-a", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual({str(p): p.read_bytes() for p in self.clients_dir.glob("*.json")}, before_meta)
        self.assertTrue(self.state()["active"])
        self.assertEqual(sorted(self.backups_dir.iterdir()), [])

    def test_user_binding_has_priority_over_inbound_route_and_unbind_preserves_it(self) -> None:
        self.seed(rules=[{"type": "field", "inboundTag": ["user-route-node"], "outboundTag": "direct"}, self.unrelated_rule])
        self.bind(1, "exit-a")
        rules = self.routes()
        self.assertEqual(rules[0]["user"], ["user-a"])
        self.assertEqual(rules[1]["inboundTag"], ["user-route-node"])
        self.unbind(1)
        rules = self.routes()
        self.assertEqual(rules[0]["inboundTag"], ["user-route-node"])
        self.assertEqual(rules[1], self.unrelated_rule)

    def test_unbind_and_rebind_after_config_import_preserve_stale_client_cache_and_other_routes(self) -> None:
        self.seed()
        self.bind(1, "exit-a")
        self.bind(2, "exit-b")
        original_meta = self.metadata()
        before_meta = copy.deepcopy(original_meta)
        # Full-config import intentionally leaves client metadata untouched.
        imported = json.loads(self.config.read_text(encoding="utf-8"))
        imported["inbounds"][0]["port"] = 18602
        path = self.candidate(imported, "imported-user-route-config.json")
        self.shell('import_config "$1"', str(path), check=True)
        expected = copy.deepcopy(imported)
        selected_rule = before_meta["user_outbound_routes"][identity("vless", UUID2)]["rule"]
        expected["routing"]["rules"].remove(selected_rule)

        prompt = r'''prompt() { printf -v "$1" '%s' 1; }
confirm() { return 0; }
select_inbound_user() { printf -v "$1" '%s' 2; }
user_outbound_wizard_locked unbind'''
        result = self.shell(prompt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("用户绑定未保存", result.stderr)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), expected)
        after_meta = self.metadata()
        del before_meta["user_outbound_routes"][identity("vless", UUID2)]
        self.assertEqual(after_meta, before_meta)
        # A repeated unbind is a no-op, including when the cache is stale.
        before = self.snapshot_bytes()
        self.shell(prompt, check=True)
        self.assertEqual(self.snapshot_bytes(), before)
        rebind = prompt.replace('user_outbound_wizard_locked unbind', r'''
select_outbound() { printf -v "$1" '%s' exit-b; }
user_outbound_wizard_locked bind''')
        result = self.shell(rebind)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), imported)
        self.assertEqual(self.metadata(), original_meta)
        self.assertEqual(sorted(self.backups_dir.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "real menu prompts require a Linux PTY")
    def test_real_outbound_menu_binds_and_unbinds_second_user_on_second_node(self) -> None:
        node = self.make_node()
        self.seed(node)
        others = [dict(copy.deepcopy(node), tag=tag, port=port)
                  for tag, port in (("first-node", 18601), ("third-node", 18603))]
        self.write_config([others[0], node, others[1]], outbounds=self.outbounds,
                          routing={"rules": [self.unrelated_rule]})
        self.bind(1, "exit-a")
        before_config = json.loads(self.config.read_text(encoding="utf-8"))
        before_meta = self.metadata()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 240, 0, 0))
        process = subprocess.Popen(
            ["bash", "-c", 'source "$1"; wait_healthy() { service_is_active; }; outbounds_menu',
             "xray-user-route-menu-test", str(fixture.SCRIPT)],
            env=self.env, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        )
        os.close(slave)
        output = b""
        pending = b""
        deadline = time.monotonic() + 35
        steps = [
            ("请选择出站操作 [0]:", "9"), ("选择用户所属节点（", "2"),
            ("选择用户（0 返回）", "2"), ("选择出站（", "3"),
            ("[y/N]:", "y"), ("按回车返回出站管理:", ""),
            ("请选择出站操作 [0]:", "10"), ("选择用户所属节点（", "2"),
            ("选择用户（0 返回）", "2"), ("[y/N]:", "y"),
            ("按回车返回出站管理:", ""), ("请选择出站操作 [0]:", "0"),
        ]
        try:
            for marker, answer in steps:
                token = marker.encode()
                while token not in pending:
                    self.assertLess(time.monotonic(), deadline, "menu stalled: " + output.decode(errors="replace"))
                    if select.select([master], [], [], 0.2)[0]:
                        chunk = os.read(master, 65536)
                        output += chunk
                        pending += chunk
                pending = pending.split(token, 1)[1]
                os.write(master, (answer + "\n").encode())
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
        self.assertNotIn("用户绑定未保存", output.decode())
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), before_config)
        self.assertEqual(self.metadata(), before_meta)

    def test_stale_cache_unbind_still_rejects_changed_identity_or_owned_rule(self) -> None:
        self.seed()
        self.bind(2, "exit-b")
        original = self.config.read_text(encoding="utf-8")
        for change in ("credential", "email", "rule"):
            with self.subTest(change=change):
                current = json.loads(original)
                current["inbounds"][0]["port"] = 18602
                if change == "credential":
                    current["inbounds"][0]["settings"]["users"][1]["id"] = UUID3
                elif change == "email":
                    current["inbounds"][0]["settings"]["users"][1]["email"] = "changed-user"
                else:
                    current["routing"]["rules"][0]["outboundTag"] = "exit-a"
                fixture.dump(self.config, current)
                before = self.snapshot_bytes()
                result = self.unbind(2, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot_bytes(), before)

    def test_cancel_and_no_auto_backup_leave_state_unchanged(self) -> None:
        self.seed()
        before = self.snapshot_bytes()
        cancelled = self.shell(
            'confirm() { return 1; }; bind_user_outbound_locked "$@"',
            "user-route-node", "1", "exit-a",
        )
        self.assertNotEqual(cancelled.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)
        self.assertEqual(sorted(self.backups_dir.iterdir()), [])

    def test_bound_user_change_and_delete_are_rejected_until_unbound(self) -> None:
        node = self.make_node()
        metadata = self.seed(node)
        self.bind(1, "exit-a")
        before = self.snapshot_bytes()

        deleted = self.shell(
            'prompt() { printf -v "$1" 1; }; confirm() { return 0; }; change_inbound_user delete "$@"',
            node["tag"], check=False,
        )
        self.assertNotEqual(deleted.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)

        changed = copy.deepcopy(node)
        changed["settings"]["users"][0]["id"] = UUID3
        candidate = self.candidate(changed, "bound-user-change.json")
        expected = self.candidate(node, "bound-user-original.json")
        replaced = self.shell(
            'confirm() { return 0; }; replace_inbound_file "$@"',
            node["tag"], str(candidate), "example.com", PUBLIC_KEY, str(expected),
        )
        self.assertNotEqual(replaced.returncode, 0)
        self.assertEqual(self.snapshot_bytes(), before)
        self.assertTrue(metadata.is_file())

        self.unbind(1)
        self.assertEqual(self.bind(1, "exit-b").returncode, 0)

    def test_bound_outbound_and_node_deletion_are_reference_protected(self) -> None:
        node = self.make_node()
        self.seed(node)
        self.bind(1, "exit-a")
        outbound = self.shell('confirm() { return 0; }; delete_outbound_locked "$1"', "exit-a")
        self.assertNotEqual(outbound.returncode, 0)
        self.assertIn("被路由", outbound.stderr)
        node_delete = self.shell('confirm() { return 0; }; delete_inbound "$1"', node["tag"])
        self.assertNotEqual(node_delete.returncode, 0)
        self.assertEqual(json.loads(self.shell("config_jq .inbounds", check=True).stdout)[0]["tag"], node["tag"])

    def test_replace_guard_keeps_user_route_when_unrelated_node_fields_change(self) -> None:
        node = self.make_node()
        self.seed(node)
        self.bind(1, "exit-a")
        changed = copy.deepcopy(node)
        changed["port"] = 18602
        candidate = self.candidate(changed, "bound-user-port.json")
        expected = self.candidate(node, "bound-user-port-original.json")
        replaced = self.shell(
            'confirm() { return 0; }; replace_inbound_file "$@"',
            node["tag"], str(candidate), "example.com", PUBLIC_KEY, str(expected),
        )
        # A node edit may proceed only when the manager keeps the bound-user rule and metadata.
        self.assertEqual(replaced.returncode, 0, replaced.stdout + replaced.stderr)
        self.assertEqual(json.loads(self.config.read_text())["inbounds"][0]["port"], 18602)
        self.assertTrue(any(rule.get("user") == ["user-a"] and rule.get("outboundTag") == "exit-a" for rule in self.routes()))


if __name__ == "__main__":
    unittest.main()
