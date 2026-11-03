#!/usr/bin/env python3
"""Isolated regression tests for the standalone Xray manager.

The Xray manager deliberately does not share the sing-box manager's runtime
directories.  This module therefore owns its fixture instead of inheriting
``test_manager.ManagerTest``.  The fixture is also importable by protocol
tests; it intentionally contains no ``test_*`` methods of its own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "xray-manager.sh"
UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
UUID2 = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


MOCK_SYSTEMCTL = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["XR_MOCK_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with Path(os.environ["XR_MOCK_SYSTEMCTL_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

actions = {"is-active", "is-enabled", "start", "stop", "restart", "enable",
           "disable", "daemon-reload", "reset-failed", "status", "show"}
action = next((arg for arg in args if arg in actions), "")
quiet = "--quiet" in args or "-q" in args
if action == "is-active":
    if state.get("health_failures", 0):
        state["health_failures"] -= 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if not quiet:
            print("inactive")
        raise SystemExit(3)
    if not quiet:
        print("active" if state["active"] else "inactive")
    raise SystemExit(0 if state["active"] else 3)
if action == "is-enabled":
    if not quiet:
        print("enabled" if state["enabled"] else "disabled")
    raise SystemExit(0 if state["enabled"] else 1)
if action == "restart" and state.get("restart_failures", 0):
    state["restart_failures"] -= 1
    state["active"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print("simulated Xray restart failure", file=sys.stderr)
    raise SystemExit(1)
if action == "start" and state.get("start_failures", 0):
    state["start_failures"] -= 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print("simulated Xray start failure", file=sys.stderr)
    raise SystemExit(1)
if action == "start" and state.get("start_unhealthy_once"):
    state["start_unhealthy_once"] = False
    state["health_failures"] = 3
if action in {"start", "restart"}:
    state["active"] = True
elif action == "stop":
    state["active"] = False
elif action == "enable":
    state["enabled"] = True
    if "--now" in args:
        state["active"] = True
elif action == "disable":
    state["enabled"] = False
    if "--now" in args:
        state["active"] = False
elif action == "show":
    if "--value" in args:
        print(os.environ.get("XR_MOCK_UNIT_FILE", ""))
    else:
        print("FragmentPath=" + os.environ.get("XR_MOCK_UNIT_FILE", ""))
state_path.write_text(json.dumps(state), encoding="utf-8")
raise SystemExit(0)
'''


MOCK_CORE = r'''#!/usr/bin/env python3
"""Small Xray CLI substitute used only by the isolated test fixture."""
import json
import os
from pathlib import Path
import sys


def strip_jsonc(source: str) -> str:
    output = []
    quoted = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(source) and source[index + 1] == "/":
            newline = source.find("\n", index)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


args = sys.argv[1:]
with Path(os.environ["XR_MOCK_CORE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

if args in (["version"], ["--version"]):
    print("Xray 1.8.24")
    raise SystemExit(0)
if args == ["uuid"] or args == ["generate", "uuid"]:
    print("b7efdc96-87aa-4910-81b4-374b2eecc737")
    raise SystemExit(0)
if args in (["x25519"], ["generate", "x25519"]):
    print("Private key: Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw")
    print("Password (PublicKey): AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    raise SystemExit(0)

if args and args[0] == "run" and "-test" in args:
    try:
        option = "-config" if "-config" in args else "--config"
        path = Path(args[args.index(option) + 1])
        value = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
        assert isinstance(value, dict)
        assert isinstance(value.get("inbounds", []), list)
        assert isinstance(value.get("outbounds", []), list)
        assert not value.get("_test_invalid")
    except (AssertionError, OSError, ValueError, IndexError):
        print("mock Xray: invalid configuration", file=sys.stderr)
        raise SystemExit(23)
    raise SystemExit(0)

print(json.dumps(args, ensure_ascii=False))
raise SystemExit(0)
'''


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class XrayFixture(unittest.TestCase):
    """Reusable isolated fixture; this class deliberately has no test methods."""

    def setUp(self) -> None:
        if not SCRIPT.is_file():
            self.skipTest("xray-manager.sh is not present yet")
        if not shutil.which("bash") or not shutil.which("jq"):
            self.skipTest("bash and jq are required")
        self.temp = tempfile.TemporaryDirectory(prefix="xray-manager-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "mock-bin"
        self.bin.mkdir()
        self.lib_dir = self.root / "usr/local/lib/xray-manager"
        self.lib_dir.mkdir(parents=True)
        self.core = self.lib_dir / "xray"
        self.wrapper = self.root / "usr/local/bin/xray"
        self.wrapper.parent.mkdir(parents=True)
        self.config = self.root / "etc/xray/config.json"
        self.config.parent.mkdir(parents=True)
        self.state_dir = self.root / "var/lib/xray-manager"
        self.clients_dir = self.state_dir / "clients"
        self.backups_dir = self.state_dir / "backups"
        self.clients_dir.mkdir(parents=True)
        self.backups_dir.mkdir(parents=True)
        self.default_server = self.state_dir / "default-server"
        self.unit = self.root / "etc/systemd/system/xray.service"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("# xray-manager\n[Service]\nType=simple\n", encoding="utf-8")
        self.marker = self.lib_dir / ".managed"
        self.marker.write_text("xray-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.mock_state = self.root / "service-state.json"
        self.system_log = self.root / "systemctl.jsonl"
        self.core_log = self.root / "xray.jsonl"
        self.original = {
            "log": {"loglevel": "warning"},
            "inbounds": [],
            "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {}}],
            "routing": {"domainStrategy": "AsIs", "rules": []},
        }
        dump(self.config, self.original)
        self.original_bytes = self.config.read_bytes()
        state = {
            "active": False,
            "enabled": False,
            "restart_failures": 0,
            "start_failures": 0,
            "start_unhealthy_once": False,
            "health_failures": 0,
        }
        dump(self.mock_state, state)
        for path, content in (
            (self.bin / "systemctl", MOCK_SYSTEMCTL),
            (self.core, MOCK_CORE),
        ):
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "XR_ROOT_PREFIX": str(self.root),
            "XR_MOCK_STATE_FILE": str(self.mock_state),
            "XR_MOCK_SYSTEMCTL_LOG": str(self.system_log),
            "XR_MOCK_CORE_LOG": str(self.core_log),
            "XR_MOCK_UNIT_FILE": str(self.unit),
            "XR_MOCK_MANAGER": str(SCRIPT),
            "XR_CORE_ASSETS": str(self.lib_dir),
            "XR_TEST_CORE": str(self.core),
            "LC_ALL": "C.UTF-8",
        })

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; wait_healthy() { service_is_active; }; ' + body,
             "xray-manager-test", str(SCRIPT), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=25,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=25,
        )

    def state(self, **updates: object) -> dict:
        value = json.loads(self.mock_state.read_text(encoding="utf-8"))
        if updates:
            value.update(updates)
            dump(self.mock_state, value)
        return value

    def calls(self) -> list[list[str]]:
        if not self.system_log.exists():
            return []
        return [json.loads(line) for line in self.system_log.read_text(encoding="utf-8").splitlines()]

    def core_calls(self) -> list[list[str]]:
        if not self.core_log.exists():
            return []
        return [json.loads(line) for line in self.core_log.read_text(encoding="utf-8").splitlines()]

    def write_node(self, tag: str = "vless", port: int = 18443, *, protocol: str = "vless",
                   method: str = "tcp", credential: str = UUID) -> dict:
        user = {"id": credential} if protocol in {"vless", "vmess"} else {"password": credential}
        return {
            "protocol": protocol,
            "tag": tag,
            "listen": "127.0.0.1",
            "port": port,
            "settings": {"users": [user]},
            "streamSettings": {"method": method, "security": "none"},
        }

    def write_hy2(self, tag: str = "hy2", port: int = 18443) -> dict:
        return {
            "protocol": "hysteria",
            "tag": tag,
            "listen": "::",
            "port": port,
            "settings": {"version": 2,"users": [{"auth": "hy2-password"}]},
            "streamSettings": {"method": "hysteria", "security": "tls"},
        }

    def write_config(self, inbounds: list[dict], *, outbounds: list[dict] | None = None,
                     routing: dict | None = None) -> None:
        value = dict(self.original)
        value["inbounds"] = inbounds
        if outbounds is not None:
            value["outbounds"] = outbounds
        if routing is not None:
            value["routing"] = routing
        dump(self.config, value)

    def write_metadata(self, node: dict, *, server: str = "example.com",
                       public_key: str = "") -> Path:
        path = self.clients_dir / f"{node['tag']}.json"
        dump(path, {"server": server, "public_key": public_key, "inbound": node})
        path.chmod(0o600)
        return path

    def candidate(self, node: dict, name: str = "candidate.json") -> Path:
        path = self.root / name
        dump(path, node)
        return path

    def seed(self, node: dict | None = None, *, server: str = "example.com",
             public_key: str = "") -> tuple[dict, Path]:
        node = node or self.write_node()
        self.write_config([node])
        return node, self.write_metadata(node, server=server, public_key=public_key)

    def backup_path(self, result: subprocess.CompletedProcess[str]) -> Path:
        paths = [Path(line.strip()) for line in result.stdout.splitlines()
                 if line.strip().startswith(str(self.root)) and Path(line.strip()).is_file()]
        self.assertEqual(len(paths), 1, result.stdout + result.stderr)
        return paths[0]


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and SCRIPT.is_file(),
                     "bash, jq, and xray-manager.sh are required")
class XrayManagerTest(XrayFixture):
    """Management, transaction, path-isolation, and routing regressions."""

    def test_private_paths_core_passthrough_and_cli_rejects_test_seam(self) -> None:
        self.assertEqual(self.core, self.root / "usr/local/lib/xray-manager/xray")
        self.assertEqual(self.wrapper, self.root / "usr/local/bin/xray")
        self.assertEqual(self.config, self.root / "etc/xray/config.json")
        self.assertEqual(self.state_dir, self.root / "var/lib/xray-manager")
        self.assertEqual(self.default_server, self.state_dir / "default-server")
        self.assertEqual(self.shell('printf "%s\\n" "$CORE_BIN" "$WRAPPER" "$CONFIG_FILE" "$STATE_DIR" "$SERVICE"', check=True).stdout.splitlines(), [
            str(self.core), str(self.wrapper), str(self.config), str(self.state_dir), "xray.service",
        ])
        self.assertEqual(self.shell('"$CORE_BIN" version', check=True).stdout.strip(), "Xray 1.8.24")
        self.assertEqual(self.shell('"$CORE_BIN" uuid', check=True).stdout.strip(), UUID)
        x25519 = self.shell('"$CORE_BIN" x25519', check=True).stdout
        self.assertIn("Password (PublicKey):", x25519)
        cli = self.cli("version")
        self.assertNotEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertNotIn(str(self.root), cli.stdout + cli.stderr)

    def test_stopped_add_starts_without_enabling_and_preserves_metadata(self) -> None:
        old = self.write_node("old", 18442)
        self.write_config([old])
        old_meta = self.write_metadata(old, server="old.example", public_key=PUBLIC_KEY)
        old_metadata_before = old_meta.read_bytes()
        self.config.write_text(self.config.read_text(encoding="utf-8").replace("{\n", "{\n  // keep xray JSONC\n", 1), encoding="utf-8")
        new = self.write_node("new", 18443)
        result = self.shell('add_managed_inbound "$1" "$2" "$3"', str(self.candidate(new)), "new.example", PUBLIC_KEY,
                            check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(self.shell("config_jq .", check=True).stdout)
        self.assertEqual({node["tag"] for node in value["inbounds"]}, {"old", "new"})
        self.assertIn("// keep xray JSONC", self.config.read_text(encoding="utf-8"))
        self.assertEqual(old_meta.read_bytes(), old_metadata_before)
        self.assertTrue((self.clients_dir / "new.json").is_file())
        current = self.state()
        self.assertTrue(current["active"])
        self.assertFalse(current["enabled"])
        self.assertTrue(any("start" in call for call in self.calls()))
        self.assertFalse(any("enable" in call for call in self.calls()))

    def test_add_start_failure_restores_config_and_new_metadata(self) -> None:
        before = self.config.read_bytes()
        new = self.write_node("start-fail", 18444)
        self.state(start_failures=1)
        result = self.shell('add_managed_inbound "$1" "$2" "$3"', str(self.candidate(new)), "example.com", PUBLIC_KEY)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.clients_dir / "start-fail.json").exists())
        self.assertFalse(self.state()["active"])

    def test_active_replace_restart_failure_restores_config_metadata_and_boot_flag(self) -> None:
        old, metadata = self.seed(self.write_node("replace", 18445), public_key=PUBLIC_KEY)
        self.state(active=True, enabled=True, restart_failures=1)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        changed = self.write_node("replace", 18446, credential=UUID2)
        expected = self.candidate(old, "expected-old.json")
        result = self.shell(
            'replace_inbound_file "$1" "$2" "$3" "$4" "$5"',
            old["tag"], str(self.candidate(changed, "replace-new.json")), "example.com", PUBLIC_KEY, str(expected),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        self.assertTrue(self.state()["active"])
        self.assertTrue(self.state()["enabled"])

    def test_create_backup_and_apply_bundle_restore_config_metadata_and_default(self) -> None:
        node, metadata = self.seed(self.write_node("backup", 18447), server="backup.example")
        self.default_server.write_text("backup.example\n", encoding="utf-8")
        result = self.shell("create_backup", check=True)
        bundle = self.backup_path(result)
        original_config = self.config.read_bytes()
        original_metadata = metadata.read_bytes()
        original_default = self.default_server.read_bytes()
        self.write_config([self.write_node("changed", 18448)])
        metadata.write_text('{"server":"changed.example"}\n', encoding="utf-8")
        self.default_server.write_text("changed.example\n", encoding="utf-8")
        restored = self.shell('apply_bundle "$1" preserve preserve', str(bundle), check=True)
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertEqual(metadata.read_bytes(), original_metadata)
        self.assertEqual(self.default_server.read_bytes(), original_default)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]["tag"], node["tag"])

    def test_delete_reference_protection_then_removes_node_and_metadata(self) -> None:
        node, metadata = self.seed(self.write_node("delete", 18449))
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["routing"]["rules"].append({"type": "field", "inboundTag": [node["tag"]], "outboundTag": "direct"})
        dump(self.config, value)
        before = self.config.read_bytes()
        blocked = self.shell('confirm() { return 0; }; delete_inbound "$1"', node["tag"])
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        value["routing"]["rules"] = []
        dump(self.config, value)
        deleted = self.shell('confirm() { return 0; }; delete_inbound "$1"', node["tag"], check=True)
        self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"], [])
        self.assertFalse(metadata.exists())

    def test_bind_owned_route_unbind_preserves_unrelated_rule_and_metadata(self) -> None:
        node, metadata = self.seed(self.write_node("bound", 18450))
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["outbounds"].append({"protocol": "freedom", "tag": "proxy", "settings": {}})
        unrelated = {"type": "field", "inboundTag": [node["tag"]], "outboundTag": "direct"}
        value["routing"]["rules"] = [unrelated]
        dump(self.config, value)
        bound = self.shell('confirm() { return 0; }; bind_outbound_locked "$1" "$2"', node["tag"], "proxy", check=True)
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        value = json.loads(self.config.read_text(encoding="utf-8"))
        owned = {"type": "field", "inboundTag": [node["tag"]], "outboundTag": "proxy"}
        self.assertIn(owned, value["routing"]["rules"])
        self.assertIn(unrelated, value["routing"]["rules"])
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(meta["outbound_route"]["format"], "xray-manager-outbound-route-v1")
        self.assertEqual(meta["outbound_route"]["rule"], owned)
        calls_before = len(self.calls())
        noop = self.shell('confirm() { return 1; }; bind_outbound_locked "$1" "$2"', node["tag"], "proxy")
        self.assertEqual(noop.returncode, 0, noop.stdout + noop.stderr)
        self.assertEqual(len(self.calls()), calls_before)
        unbound = self.shell('confirm() { return 0; }; bind_outbound_locked "$1" -', node["tag"], check=True)
        self.assertEqual(unbound.returncode, 0, unbound.stdout + unbound.stderr)
        value = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn(owned, value["routing"]["rules"])
        self.assertIn(unrelated, value["routing"]["rules"])
        self.assertNotIn("outbound_route", json.loads(metadata.read_text(encoding="utf-8")))

    def test_bind_rejects_stale_owned_rule_without_writing(self) -> None:
        node, metadata = self.seed(self.write_node("stale", 18451))
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["outbounds"].append({"protocol": "freedom", "tag": "proxy", "settings": {}})
        stale = {"type": "field", "inboundTag": [node["tag"]], "outboundTag": "other"}
        value["routing"]["rules"] = [stale]
        dump(self.config, value)
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        metadata_value["outbound_route"] = {
            "format": "xray-manager-outbound-route-v1",
            "rule": {"type": "field", "inboundTag": [node["tag"]], "outboundTag": "proxy"},
        }
        dump(metadata, metadata_value)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        result = self.shell('confirm() { return 0; }; bind_outbound_locked "$1" "$2"', node["tag"], "proxy")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)

    def test_same_tcp_port_rejects_but_hysteria_udp_port_can_share(self) -> None:
        first = self.write_node("tcp-first", 18452)
        self.write_config([first])
        same_tcp = self.write_node("tcp-second", 18452)
        conflict = self.shell('check_inbound_conflicts "$1" "" "$2"', str(self.candidate(same_tcp, "same-tcp.json")), str(self.config))
        self.assertNotEqual(conflict.returncode, 0, conflict.stdout + conflict.stderr)
        hy2 = self.write_hy2("udp-same", 18452)
        allowed = self.shell('check_inbound_conflicts "$1" "" "$2"', str(self.candidate(hy2, "same-udp.json")), str(self.config))
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_duplicate_tag_is_rejected_even_when_network_differs(self) -> None:
        first = self.write_node("same-tag", 18453)
        self.write_config([first])
        candidate = self.write_hy2("same-tag", 18454)
        result = self.shell('check_inbound_conflicts "$1" "" "$2"', str(self.candidate(candidate, "duplicate-tag.json")), str(self.config))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
