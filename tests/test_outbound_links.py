#!/usr/bin/env python3
"""Tests for the single-file outbound share-link parser."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import unittest
from urllib.parse import quote

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class OutboundLinkParserTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False):
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def parse(self, *links: str) -> list[dict]:
        path = self.root / "outbound-links.txt"
        path.write_text("\n".join(links) + "\n", encoding="utf-8")
        result = self.shell('parse_outbound_links "$1"', str(path), check=True)
        value = json.loads(result.stdout)
        self.assertIsInstance(value, list)
        return value

    @staticmethod
    def vmess(value: dict) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
        ).decode().rstrip("=")
        return "vmess://" + encoded

    def test_positive_all_supported_link_types_and_security_fields(self) -> None:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        links = [
            "vless://" + uuid + "@[::1]:443?encryption=none&type=grpc&security=reality"
            "&flow=xtls-rprx-vision&fp=chrome&sni=example.com&pbk="
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=0123456789abcdef"
            "&alpn=h2&serviceName=RealityGrpc&mode=gun#vless-node",
            self.vmess({
                "v": "2", "ps": "vmess-node", "add": "::1", "port": "8443", "id": uuid,
                "aid": "0", "scy": "auto", "net": "ws", "type": "none",
                "host": "cdn.example.com", "path": "/vmess", "tls": "tls",
                "sni": "cdn.example.com", "alpn": "h2,http/1.1",
            }),
            "hysteria2://hy-password@[::1]:443?sni=example.com&alpn=h3"
            "&obfs=salamander&obfs-password=salty-password#hy2-node",
            "tuic://" + uuid + ":tuic-password@[::1]:443?sni=example.com&alpn=h3"
            "&congestion_control=bbr&udp_relay_mode=native#tuic-node",
            "anytls://any-password@example.com:443?sni=example.com&alpn=h2#anytls-node",
            "ss://" + quote("2022-blake3-aes-128-gcm") + ":" + quote("ss-password")
            + "@[::1]:8388#ss-node",
            "trojan://trojan-password@example.com:443?security=tls&sni=example.com"
            "&alpn=h2&type=grpc&serviceName=TrojanGrpc&mode=gun#trojan-node",
            "socks5://user%40name:pass%3Aword@[::1]:1080#socks-node",
            "http://proxy-user:proxy-pass@example.com:8080#http-node",
            "https://proxy-user:proxy-pass@example.com:8443#https-node",
        ]
        result = self.parse(*links)
        self.assertEqual([item["type"] for item in result], [
            "vless", "vmess", "hysteria2", "tuic", "anytls",
            "shadowsocks", "trojan", "socks", "http", "http",
        ])
        self.assertEqual(result[0]["server"], "::1")
        self.assertEqual(result[0]["transport"], {"type": "grpc", "service_name": "RealityGrpc"})
        self.assertEqual(result[0]["flow"], "xtls-rprx-vision")
        self.assertEqual(result[0]["tls"]["reality"]["public_key"], "A" * 43)
        self.assertEqual(result[1]["transport"]["headers"]["Host"], "cdn.example.com")
        self.assertEqual(result[1]["tls"]["alpn"], ["h2", "http/1.1"])
        self.assertEqual(result[2]["obfs"], {"type": "salamander", "password": "salty-password"})
        self.assertEqual(result[3]["congestion_control"], "bbr")
        self.assertEqual(result[6]["transport"], {"type": "grpc", "service_name": "TrojanGrpc"})
        self.assertEqual(result[7]["username"], "user@name")
        self.assertEqual(result[7]["password"], "pass:word")
        self.assertTrue(result[9]["tls"]["enabled"])

    def test_ss_legacy_full_base64_and_hy2_alias(self) -> None:
        sip002_user = base64.urlsafe_b64encode(b"2022-blake3-aes-128-gcm:sip-password").decode().rstrip("=")
        legacy = base64.urlsafe_b64encode(
            b"2022-blake3-aes-128-gcm:legacy-password@example.com:8388",
        ).decode().rstrip("=")
        parsed = self.parse(
            "ss://" + sip002_user + "@example.com:8387#sip002-node",
            "ss://" + legacy + "#legacy-node",
            "hy2://hy-password@example.com:443?sni=example.com#hy2-alias",
        )
        self.assertEqual(parsed[0]["type"], "shadowsocks")
        self.assertEqual(parsed[0]["password"], "sip-password")
        self.assertEqual(parsed[1]["type"], "shadowsocks")
        self.assertEqual(parsed[1]["method"], "2022-blake3-aes-128-gcm")
        self.assertEqual(parsed[1]["password"], "legacy-password")
        self.assertEqual(parsed[2]["type"], "hysteria2")

    def test_tags_use_valid_names_or_stable_ascii_hash_slugs(self) -> None:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        links = [
            f"vless://{uuid}@example.com:443?encryption=none#中文名称",
            f"vless://{uuid}@example.com:444?encryption=none#",
            f"vless://{uuid}@example.com:445?encryption=none#"
            + "x" * 100,
        ]
        first = self.parse(*links)
        second = self.parse(*links)
        self.assertEqual([item["tag"] for item in first], [item["tag"] for item in second])
        for item in first:
            self.assertRegex(item["tag"], r"^import-[0-9a-f]{12}$")
            self.assertLessEqual(len(item["tag"]), 64)

    def test_vmess_ps_fallback_and_ipv6_brackets(self) -> None:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        value = {
            "v": "2", "ps": "vmess-ps", "add": "::1", "port": "443", "id": uuid,
            "aid": "0", "scy": "auto", "net": "tcp", "type": "none",
        }
        parsed = self.parse(self.vmess(value))[0]
        self.assertEqual(parsed["tag"], "vmess-ps")
        self.assertEqual(parsed["server"], "::1")
        self.assertEqual(parsed["server_port"], 443)

    def test_reality_empty_short_id_is_preserved_and_grpc_vision_is_rejected(self) -> None:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        link = (
            f"vless://{uuid}@example.com:443?encryption=none&type=tcp&security=reality"
            "&sni=example.com&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=#empty-sid"
        )
        self.assertEqual(self.parse(link)[0]["tls"]["reality"]["short_id"], "")
        rejected = (
            f"vless://{uuid}@example.com:443?encryption=none&type=grpc&security=reality"
            "&flow=xtls-rprx-vision&sni=example.com&pbk="
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=0123456789abcdef"
            "&serviceName=Grpc"
        )
        path = self.root / "grpc-vision-link.txt"
        path.write_text(rejected + "\n", encoding="utf-8")
        result = self.shell('parse_outbound_links "$1"', str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertNotIn("xtls-rprx-vision", result.stderr)

    def test_client_link_roundtrip_for_project_outbound_types(self) -> None:
        uuid = "b7efdc96-87aa-4910-81b4-374b2eecc737"
        outbounds = {
            "vless": {
                "type": "vless", "server": "example.com", "server_port": 443, "uuid": uuid,
                "tag": "round-vless", "transport": {"type": "ws", "path": "/vless", "headers": {"Host": "cdn.example.com"}},
                "tls": {"enabled": True, "server_name": "example.com", "alpn": ["h2"]},
            },
            "vmess": {
                "type": "vmess", "server": "::1", "server_port": 8443, "uuid": uuid,
                "tag": "round-vmess", "security": "auto", "alter_id": 0,
                "transport": {"type": "grpc", "service_name": "VmessGrpc"},
                "tls": {"enabled": True, "server_name": "example.com", "alpn": ["h2"]},
            },
            "hysteria2": {
                "type": "hysteria2", "server": "example.com", "server_port": 443,
                "password": "hy-password", "tag": "round-hy2",
                "tls": {"enabled": True, "server_name": "example.com", "alpn": ["h3"]},
                "obfs": {"type": "salamander", "password": "salty-password"},
            },
            "tuic": {
                "type": "tuic", "server": "example.com", "server_port": 443,
                "uuid": uuid, "password": "tuic-password", "tag": "round-tuic",
                "congestion_control": "bbr",
                "tls": {"enabled": True, "server_name": "example.com", "alpn": ["h3"]},
            },
            "anytls": {
                "type": "anytls", "server": "example.com", "server_port": 443,
                "password": "any-password", "tag": "round-anytls",
                "tls": {"enabled": True, "server_name": "example.com"},
            },
            "shadowsocks": {
                "type": "shadowsocks", "server": "example.com", "server_port": 8388,
                "method": "2022-blake3-aes-128-gcm", "password": "ss-password", "tag": "round-ss",
            },
        }
        for name, outbound in outbounds.items():
            with self.subTest(name=name):
                source = self.root / (name + "-outbound.json")
                fixture.dump(source, outbound)
                link = self.shell('client_link "$1" "$2"', str(source), outbound["tag"], check=True).stdout.strip()
                parsed = self.parse(link)[0]
                self.assertEqual(parsed["type"], outbound["type"])
                self.assertEqual(parsed["server"], outbound["server"])
                self.assertEqual(parsed["server_port"], outbound["server_port"])

    def test_malformed_or_unsupported_links_fail_without_echoing_credentials(self) -> None:
        cases = (
            "ftp://secret-password@example.com:443#bad",
            "vless://b7efdc96-87aa-4910-81b4-374b2eecc737@example.com:443?encryption=none&type=quic&password=secret-password",
            "vless://b7efdc96-87aa-4910-81b4-374b2eecc737@example.com:443?encryption=none&unknown=secret-password",
            "hysteria2://secret-password@example.com:443?insecure=1",
            "tuic://not-a-uuid:secret-password@example.com:443",
            "ss://not-base64",
            "vmess://not-base64",
            "socks5://example.com:70000",
        )
        path = self.root / "bad-links.txt"
        for index, link in enumerate(cases):
            path.write_text(link + "\n", encoding="utf-8")
            result = self.shell('parse_outbound_links "$1"', str(path))
            self.assertNotEqual(result.returncode, 0, link)
            self.assertEqual(result.stdout.strip(), "")
            self.assertNotIn("secret-password", result.stderr)
            self.assertNotIn(link, result.stderr)


if __name__ == "__main__":
    unittest.main()
