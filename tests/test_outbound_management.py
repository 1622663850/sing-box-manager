#!/usr/bin/env python3
"""Focused tests for outbound import, route binding and deletion."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import time
import unittest

import test_manager as fixture

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no termios/pty.
    pty = None


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class OutboundManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def calls(self) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, self.system_log)

    def _config(self, *, comments: bool = False) -> dict:
        value = copy.deepcopy(self.original)
        value["inbounds"] = [{"type": "vless", "tag": "node", "listen": "127.0.0.1", "listen_port": 25501, "users": [{"uuid": "b7efdc96-87aa-4910-81b4-374b2eecc737"}]}]
        value["outbounds"] = [{"type": "direct", "tag": "direct"}]
        value["route"] = {"rules": [{"domain_suffix": ["user.example"], "outbound": "direct"}]}
        if comments:
            self.config.write_text(
                '{\n  // preserve outbound comment\n  "inbounds":' + json.dumps(value["inbounds"]) + ',\n'
                '  "outbounds":[{"type":"direct","tag":"direct"}],\n'
                '  "route":{"rules":[{"domain_suffix":["user.example"],"outbound":"direct"}]}\n}\n',
                encoding="utf-8",
            )
        else:
            fixture.dump(self.config, value)
        return value

    def _metadata(self, tag: str = "node", **extra: object) -> Path:
        path = self.state_dir / "clients" / (tag + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"server": "example.com", "public_key": "", "inbound": json.loads(self.shell("config_jq --arg tag \"$1\" '.inbounds[]|select(.tag==$tag)'", tag, check=True).stdout)}
        value.update(extra)
        fixture.dump(path, value)
        return path

    def _proxy(self, tag: str = "proxy") -> dict:
        return {"type": "socks", "tag": tag, "server": "127.0.0.1", "server_port": 1080}

    def test_normalize_jsonc_array_wrapper_and_links_reject_bad_input(self) -> None:
        cases = (
            {"type": "direct", "tag": "one"},
            [{"type": "direct", "tag": "two"}, {"type": "block", "tag": "three"}],
            {"outbounds": [{"type": "direct", "tag": "four"}]},
        )
        for index, value in enumerate(cases):
            path = self.root / (f"outbound-{index}.jsonc")
            path.write_text("// outbound input\n" + json.dumps(value) + "\n", encoding="utf-8")
            result = self.shell('normalize_outbound_import "$1"', str(path), check=True)
            normalized = json.loads(result.stdout)
            self.assertEqual([item["tag"] for item in normalized], [["one"], ["two", "three"], ["four"]][index])
        links = self.root / "links.txt"
        links.write_text("vless://b7efdc96-87aa-4910-81b4-374b2eecc737@example.com:443?encryption=none&type=tcp#link-one\n", encoding="utf-8")
        normalized = json.loads(self.shell('normalize_outbound_import "$1"', str(links), check=True).stdout)
        self.assertEqual(normalized[0]["tag"], "link-one")
        bad = self.root / "bad-outbound.json"
        bad.write_text('{"outbounds": [', encoding="utf-8")
        result = self.shell('normalize_outbound_import "$1"', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_add_outbounds_import_validates_tags_and_preserves_jsonc(self) -> None:
        self._config(comments=True)
        source = self.root / "outbounds.json"
        fixture.dump(source, {"outbounds": [self._proxy("proxy"), {"type": "direct", "tag": "direct2"}]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        saved = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual([item["tag"] for item in saved["outbounds"]], ["direct", "proxy", "direct2"])
        self.assertIn("// preserve outbound comment", self.config.read_text(encoding="utf-8"))
        duplicate = self.root / "duplicate-outbound.json"
        fixture.dump(duplicate, {"outbounds": [self._proxy("direct")]})
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(duplicate))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_batch_duplicate_endpoint_and_invalid_tag_leave_config_backup_and_service_untouched(self) -> None:
        value = self._config()
        value["endpoints"] = [{"tag": "endpoint-used", "type": "wireguard"}]
        fixture.dump(self.config, value)
        cases = (
            [{"type": "direct", "tag": "dup"}, {"type": "socks", "tag": "dup", "server": "127.0.0.1", "server_port": 1080}],
            [{"type": "direct", "tag": "endpoint-used"}],
            [{"type": "direct", "tag": "bad/tag"}],
        )
        for index, outbounds in enumerate(cases):
            with self.subTest(case=index):
                source = self.root / (f"invalid-batch-{index}.json")
                fixture.dump(source, {"outbounds": outbounds})
                before = self.config.read_bytes()
                backups = list((self.state_dir / "backups").glob("bundle-*.json"))
                result = self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_bytes(), before)
                self.assertEqual(list((self.state_dir / "backups").glob("bundle-*.json")), backups)
                self.assertFalse(any(any(action in call for action in ("start", "stop", "restart")) for call in self.calls()))

    def test_active_add_preserves_boot_state_and_existing_metadata(self) -> None:
        self._config()
        metadata = self._metadata()
        before_metadata = metadata.read_bytes()
        self.state(active=True, enabled=True)
        source = self.root / "active-outbound.json"
        fixture.dump(source, {"outbounds": [self._proxy("active-proxy")]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])
        self.assertTrue(any("restart" in call for call in self.calls()))
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_bind_switch_cancel_and_stale_owned_rule_protection(self) -> None:
        self._config(comments=True)
        self._metadata()
        source = self.root / "proxy.json"
        fixture.dump(source, {"outbounds": [self._proxy("proxy-a"), self._proxy("proxy-b")]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        self.shell('confirm() { return 0; }; bind_outbound_locked node proxy-a', check=True)
        first = json.loads(self.shell("config_jq .", check=True).stdout)
        owned = {"inbound": ["node"], "action": "route", "outbound": "proxy-a"}
        self.assertEqual(first["route"]["rules"][0], owned)
        self.assertEqual(first["route"]["rules"][1]["outbound"], "direct")
        metadata = json.loads((self.state_dir / "clients/node.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["outbound_route"]["rule"], owned)
        self.shell('confirm() { return 0; }; bind_outbound_locked node proxy-b', check=True)
        switched = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual(switched["route"]["rules"][0]["outbound"], "proxy-b")
        before = self.config.read_bytes()
        self.shell('confirm() { return 0; }; bind_outbound_locked node -', check=True)
        canceled = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual(canceled["route"]["rules"][0]["outbound"], "direct")
        self.assertNotIn("outbound_route", json.loads((self.state_dir / "clients/node.json").read_text(encoding="utf-8")))
        self.assertNotEqual(before, self.config.read_bytes())

        self.shell('confirm() { return 0; }; bind_outbound_locked node proxy-a', check=True)
        current = json.loads(self.shell("config_jq .", check=True).stdout)
        current["route"]["rules"][0]["outbound"] = "external"
        fixture.dump(self.config, current)
        before = self.config.read_bytes()
        result = self.shell('confirm() { return 0; }; bind_outbound_locked node -')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_bind_same_target_is_noop_and_unowned_metadata_is_removed_on_cancel(self) -> None:
        self._config()
        self._metadata()
        source = self.root / "noop-proxy.json"
        fixture.dump(source, {"outbounds": [self._proxy("noop-proxy")]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        self.shell('confirm() { return 0; }; bind_outbound_locked node noop-proxy', check=True)
        before_config = self.config.read_bytes()
        before_metadata = (self.state_dir / "clients/node.json").read_bytes()
        marker = self.root / "bind-confirm-marker"
        self.env["BIND_CONFIRM_MARKER"] = str(marker)
        result = self.shell('confirm() { printf x > "$BIND_CONFIRM_MARKER"; return 99; }; bind_outbound_locked node noop-proxy', check=True)
        self.assertFalse(marker.exists())
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((self.state_dir / "clients/node.json").read_bytes(), before_metadata)
        self.assertFalse(any("restart" in call for call in self.calls()))

        self.shell('confirm() { return 0; }; bind_outbound_locked node -', check=True)
        (self.state_dir / "clients/node.json").unlink()
        self.shell('confirm() { return 0; }; bind_outbound_locked node noop-proxy', check=True)
        created = json.loads((self.state_dir / "clients/node.json").read_text(encoding="utf-8"))
        self.assertTrue(created["outbound_route"]["created_metadata"])
        before_cancel = self.config.read_bytes()
        result = self.shell('confirm() { return 1; }; bind_outbound_locked node -')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_cancel)
        self.assertTrue((self.state_dir / "clients/node.json").exists())
        self.shell('confirm() { return 0; }; bind_outbound_locked node -', check=True)
        self.assertFalse((self.state_dir / "clients/node.json").exists())

    def test_bind_without_owned_record_does_not_delete_user_rule(self) -> None:
        self._config()
        self._metadata()
        result = self.shell('confirm() { return 0; }; bind_outbound_locked node -', check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rules = json.loads(self.shell("config_jq .route.rules", check=True).stdout)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["outbound"], "direct")

    def test_delete_outbound_rejects_references_and_preserves_last(self) -> None:
        self._config()
        source = self.root / "outbounds-delete.json"
        fixture.dump(source, {"outbounds": [self._proxy("proxy"), {"type": "direct", "tag": "unused"}]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        config = json.loads(self.shell("config_jq .", check=True).stdout)
        config["route"]["final"] = "proxy"
        fixture.dump(self.config, config)
        before = self.config.read_bytes()
        self.assertNotEqual(self.shell('confirm() { return 0; }; delete_outbound_locked proxy').returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        config["route"].pop("final")
        fixture.dump(self.config, config)
        self.shell('confirm() { return 0; }; delete_outbound_locked proxy', check=True)
        self.assertNotIn("proxy", [item["tag"] for item in json.loads(self.shell("config_jq .", check=True).stdout)["outbounds"]])
        self.shell('confirm() { return 0; }; delete_outbound_locked unused', check=True)
        self.assertNotEqual(self.shell('confirm() { return 0; }; delete_outbound_locked direct').returncode, 0)

    def test_active_failure_rolls_back_binding_and_metadata(self) -> None:
        self._config()
        metadata = self._metadata()
        source = self.root / "proxy-failure.json"
        fixture.dump(source, {"outbounds": [self._proxy("proxy-failure")]})
        self.shell('confirm() { return 0; }; add_outbounds_file_locked "$1"', str(source), check=True)
        self.state(active=True, restart_failures=1)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self.shell('confirm() { return 0; }; bind_outbound_locked node proxy-failure',)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])

    @unittest.skipUnless(pty is not None, "PTY support is required for menu routing")
    def test_main_menu_twenty_lists_and_zero_returns(self) -> None:
        command = 'source "$1"; show_status(){ :; }; menu'
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", "-c", command, "outbound-menu", str(fixture.SCRIPT)], self.env)
        os.write(fd, b"20\n1\n\n0\n0\n")
        output = b""
        status = None
        deadline = time.time() + 5
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    output += os.read(fd, 8192)
                except OSError:
                    pass
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            self.fail("outbound menu did not exit")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
        self.assertIn("出站", output.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
