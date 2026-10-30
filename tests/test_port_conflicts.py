#!/usr/bin/env python3
"""Focused regression tests for TCP/UDP-aware inbound port conflicts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class PortConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self._install_ss_mock()

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def state(self, **updates: object) -> dict:
        return fixture.ManagerTest.state(self, **updates)

    def calls(self) -> list[list[str]]:
        return fixture.ManagerTest.calls(self, self.system_log)

    def _install_ss_mock(self) -> None:
        path = self.bin / "ss"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import os, re, sys\n"
            "args=sys.argv[1:]\n"
            "kind='tcp' if '-lnt' in args else 'udp' if '-lnu' in args else ''\n"
            "ports=set(filter(None, os.environ.get('MOCK_SS_'+kind.upper(), '').split(',')))\n"
            "match=re.search(r':([0-9]+)', ' '.join(args))\n"
            "target=match.group(1) if match else ''\n"
            "if kind and (not target or target in ports): print('LISTEN')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        self.env["MOCK_SS_TCP"] = ""
        self.env["MOCK_SS_UDP"] = ""

    def _write_config(self, nodes: list[dict]) -> None:
        fixture.dump(self.config, {**self.original, "inbounds": nodes})

    def _node(self, protocol: str, tag: str, port: int) -> dict:
        if protocol == "vless":
            return fixture.ManagerTest.generate_inbound(
                self, "vless-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
            ) | {"tag": tag, "listen_port": port}
        if protocol == "vmess":
            return fixture.ManagerTest.generate_inbound(
                self, "vmess-tcp", "b7efdc96-87aa-4910-81b4-374b2eecc737",
            ) | {"tag": tag, "listen_port": port}
        if protocol == "hysteria2":
            cert, key = fixture.ManagerTest.tls_material(self)
            return fixture.ManagerTest.generate_inbound(
                self, "hysteria2", "hy-password", tls_server_name="example.com",
                certificate=cert, key=key,
            ) | {"tag": tag, "listen_port": port}
        if protocol == "tuic":
            cert, key = fixture.ManagerTest.tls_material(self)
            return fixture.ManagerTest.generate_inbound(
                self, "tuic", "b7efdc96-87aa-4910-81b4-374b2eecc737",
                tls_server_name="example.com", certificate=cert, key=key,
                tuic_password="tuic-password",
            ) | {"tag": tag, "listen_port": port}
        if protocol == "shadowsocks":
            return fixture.ManagerTest.generate_inbound(self, "shadowsocks", "8JCsPssfgS8tiRwiMlhARg==") | {
                "tag": tag, "listen_port": port,
            }
        raise AssertionError(protocol)

    def _check(self, candidate: dict, current: list[dict], *, excluded: str = "") -> subprocess.CompletedProcess[str]:
        self._write_config(current)
        path = self.root / (candidate["tag"] + "-candidate.json")
        fixture.dump(path, candidate)
        return self.shell('check_inbound_conflicts "$1" "$2" "$3"', str(path), excluded, str(self.config))

    def test_tcp_udp_same_port_is_allowed_but_same_network_is_rejected(self) -> None:
        tcp = self._node("vless", "tcp-existing", 25501)
        udp = self._node("hysteria2", "udp-same-port", 25501)
        self.assertEqual(self._check(udp, [tcp]).returncode, 0)
        self.assertNotEqual(self._check(self._node("vmess", "tcp-conflict", 25501), [tcp]).returncode, 0)
        udp_existing = self._node("hysteria2", "udp-existing", 25502)
        self.assertEqual(self._check(self._node("vless", "tcp-same-port", 25502), [udp_existing]).returncode, 0)
        self.assertNotEqual(self._check(self._node("tuic", "udp-conflict", 25502), [udp_existing]).returncode, 0)

    def test_tag_conflict_is_rejected_across_networks_and_ss_defaults_to_both(self) -> None:
        tcp = self._node("vless", "same-tag", 25511)
        udp = self._node("hysteria2", "same-tag", 25512)
        self.assertNotEqual(self._check(udp, [tcp]).returncode, 0)
        ss = self._node("shadowsocks", "ss-default", 25513)
        self.assertNotEqual(self._check(self._node("vless", "tcp-ss", 25513), [ss]).returncode, 0)
        ss_tcp = copy.deepcopy(ss)
        ss_tcp["tag"] = "ss-tcp"
        ss_tcp["network"] = "tcp"
        self.assertEqual(self._check(self._node("hysteria2", "udp-ss-tcp", 25513), [ss_tcp]).returncode, 0)
        ss_udp = copy.deepcopy(ss)
        ss_udp["tag"] = "ss-udp"
        ss_udp["network"] = "udp"
        self.assertEqual(self._check(self._node("vless", "tcp-ss-udp", 25513), [ss_udp]).returncode, 0)
        self.env["MOCK_SS_UDP"] = "25514"
        self.assertNotEqual(self._check(self._node("shadowsocks", "ss-system-both", 25514), []).returncode, 0)

    def test_edit_only_skips_transports_already_owned_on_the_same_port(self) -> None:
        old = self._node("shadowsocks", "ss-owned", 25515) | {"network": "tcp"}
        self.env["MOCK_SS_TCP"] = "25515"
        self.env["MOCK_SS_UDP"] = "25515"
        self.assertEqual(self._check(old, [old], excluded=old["tag"]).returncode, 0)
        expanded = self._node("shadowsocks", old["tag"], 25515)
        self.assertNotEqual(self._check(expanded, [old], excluded=old["tag"]).returncode, 0)

    def test_system_tcp_and_udp_listeners_are_checked_independently(self) -> None:
        self.env["MOCK_SS_TCP"] = "25521"
        udp = self._node("hysteria2", "udp-listener", 25521)
        self.assertEqual(self._check(udp, []).returncode, 0)
        self.env["MOCK_SS_UDP"] = "25522"
        tcp = self._node("vless", "tcp-listener", 25522)
        self.assertEqual(self._check(tcp, []).returncode, 0)
        self.assertNotEqual(self._check(self._node("vless", "tcp-busy", 25521), []).returncode, 0)
        self.assertNotEqual(self._check(self._node("hysteria2", "udp-busy", 25522), []).returncode, 0)

    def test_quic_transport_is_udp_but_vless_network_field_does_not_change_tcp(self) -> None:
        tcp = self._node("vless", "tcp-existing", 25525)
        quic = self._node("vless", "quic-vless", 25525)
        quic["transport"] = {"type": "quic"}
        self.assertEqual(self._check(quic, [tcp]).returncode, 0)
        udp = self._node("hysteria2", "udp-existing", 25526)
        self.assertNotEqual(self._check(quic | {"listen_port": 25526}, [udp]).returncode, 0)
        network_lie = self._node("vless", "network-lie", 25527)
        network_lie["network"] = "udp"
        self.assertNotEqual(self._check(network_lie, [tcp | {"listen_port": 25527}]).returncode, 0)

    def test_add_managed_and_single_file_use_network_aware_conflicts(self) -> None:
        existing_tcp = self._node("vless", "existing-tcp", 25531)
        self._write_config([existing_tcp])
        udp_file = self.root / "udp-add.json"
        fixture.dump(udp_file, self._node("hysteria2", "udp-add", 25531))
        self.shell('add_managed_inbound "$1" "example.com" ""', str(udp_file), check=True)
        self.assertEqual(len(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"]), 2)

        self._write_config([self._node("hysteria2", "existing-udp", 25532)])
        tcp_file = self.root / "tcp-add.json"
        fixture.dump(tcp_file, self._node("vless", "tcp-add", 25532))
        self.shell('add_inbound_file "$1"', str(tcp_file), check=True)
        self.assertEqual(len(json.loads(self.config.read_text(encoding="utf-8"))["inbounds"]), 2)

    def test_replace_allows_udp_port_but_rejects_same_tcp_and_preserves_on_failure(self) -> None:
        old = self._node("vless", "edit-tcp", 25541)
        other_udp = self._node("hysteria2", "other-udp", 25542)
        other_tcp = self._node("vless", "other-tcp", 25543)
        self._write_config([old, other_udp, other_tcp])
        metadata = self.state_dir / "clients/edit-tcp.json"
        fixture.dump(metadata, {"server": "example.com", "public_key": "", "inbound": old})
        self.env["MOCK_SS_TCP"] = "25541"
        self.env["MOCK_SS_UDP"] = "25542"
        candidate_path = self.root / "candidate-tcp.json"
        same_port = dict(old, listen_port=25541)
        fixture.dump(candidate_path, same_port)
        self.shell('replace_inbound_file "$1" "$2" "" "" ""', old["tag"], str(candidate_path), check=True)
        before_config = self.config.read_bytes()
        before_metadata = metadata.read_bytes()
        before_calls = self.calls()
        candidate = dict(old, listen_port=25543)
        fixture.dump(candidate_path, candidate)
        result = self.shell('replace_inbound_file "$1" "$2" "" "" ""', old["tag"], str(candidate_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(metadata.read_bytes(), before_metadata)
        new_calls = self.calls()[len(before_calls):]
        self.assertFalse(any(any(action in call for action in ("start", "stop", "restart")) for call in new_calls))
        candidate["listen_port"] = 25542
        fixture.dump(candidate_path, candidate)
        self.shell('replace_inbound_file "$1" "$2" "" "" ""', old["tag"], str(candidate_path), check=True)
        saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"]
        self.assertEqual(saved[0]["listen_port"], 25542)


if __name__ == "__main__":
    unittest.main()
