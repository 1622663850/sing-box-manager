#!/usr/bin/env python3
"""Independent tests for the Xray protocol shell fragment."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_xray_manager as fixture

UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
UUID2 = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
PRIVATE = "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw"
PUBLIC = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class XrayProtocolsTest(fixture.XrayFixture):
    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("openssl"):
            self.skipTest("openssl is required for TLS template tests")
        self.certificate = self.root / "xray-test-cert.pem"
        self.private_key = self.root / "xray-test-key.pem"
        result = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(self.private_key), "-out", str(self.certificate), "-days", "1",
             "-subj", "/CN=example.com", "-addext", "subjectAltName=DNS:example.com"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.XrayFixture.shell(self, body, *args, check=check)

    def generate(self, protocol: str, credential: str = UUID, extra: str = "") -> dict:
        result = self.shell(
            'generate_inbound "$@"', protocol, "node", "443", credential, extra, PRIVATE,
            "0123456789abcdef", "example.com", str(self.certificate), str(self.private_key),
            "password-value", "obfs-value" if protocol == "hysteria" else "", check=True,
        )
        return json.loads(result.stdout)

    def write(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_generate_and_detect_all_supported_templates(self) -> None:
        protocols = {
            "vless-tcp": (UUID, ""),
            "vless-tcp-tls": (UUID, ""),
            "vless-reality": (UUID, ""),
            "vless-ws": (UUID, "/vless"),
            "vless-ws-tls": (UUID, "/vless"),
            "vless-grpc-tls": (UUID, "GrpcService"),
            "vless-grpc-reality": (UUID, "GrpcService"),
            "vless-xhttp-tls": (UUID, "/xhttp"),
            "vless-xhttp-reality": (UUID, "/xhttp"),
            "vmess-tcp": (UUID, ""),
            "vmess-tcp-tls": (UUID, ""),
            "vmess-ws": (UUID, "/vmess"),
            "vmess-ws-tls": (UUID, "/vmess"),
            "vmess-grpc-tls": (UUID, "VmessGrpc"),
            "trojan": ("trojan-password", ""),
            "shadowsocks": ("8JCsPssfgS8tiRwiMlhARg==", ""),
            "hysteria": ("hy-password", ""),
        }
        for protocol, (credential, extra) in protocols.items():
            with self.subTest(protocol=protocol):
                value = self.generate(protocol, credential, extra)
                path = self.write(protocol + ".json", value)
                detected = self.shell('detect_inbound_template "$1"', str(path), check=True)
                self.assertEqual(detected.stdout.strip(), protocol)

    def test_user_validation_and_count_cover_multi_user_tables(self) -> None:
        inbound = self.generate("vless-tcp")
        inbound["settings"]["users"].append({"id": UUID2, "flow": ""})
        path = self.write("vless-users.json", inbound)
        self.assertEqual(self.shell('inbound_user_count "$1"', str(path), check=True).stdout.strip(), "2")
        self.assertEqual(self.shell('validate_inbound_users "$1"', str(path), check=True).stdout.strip(), "")
        invalid = json.loads(json.dumps(inbound))
        invalid["settings"]["users"][1]["id"] = "bad"
        invalid_path = self.write("invalid-users.json", invalid)
        self.assertNotEqual(self.shell('validate_inbound_users "$1"', str(invalid_path)).returncode, 0)
        ss = self.generate("shadowsocks", "8JCsPssfgS8tiRwiMlhARg==")
        self.assertEqual(ss["settings"]["network"], "tcp,udp")
        self.assertEqual(self.shell('inbound_user_count "$1"', str(self.write("ss.json", ss)), check=True).stdout.strip(), "1")

    def test_generate_client_preserves_reality_transport_and_multi_user_index(self) -> None:
        inbound = self.generate("vless-reality")
        inbound["settings"]["users"].append({"id": UUID2})
        path = self.write("reality.json", inbound)
        for index, expected in ((1, UUID), (2, UUID2)):
            with self.subTest(index=index):
                result = self.shell(
                    'generate_client "$@"', str(path), "127.0.0.1", PUBLIC, str(index), check=True,
                )
                outbound = json.loads(result.stdout)
                self.assertEqual(outbound["protocol"], "vless")
                self.assertEqual(outbound["settings"]["id"], expected)
                reality = outbound["streamSettings"]["realitySettings"]
                self.assertEqual(reality["password"], PUBLIC)
                self.assertEqual(reality["shortId"], "0123456789abcdef")

    def test_client_links_roundtrip_supported_standard_protocols(self) -> None:
        cases = ("vless-tcp", "vmess-tcp", "trojan", "shadowsocks", "hysteria")
        for protocol in cases:
            with self.subTest(protocol=protocol):
                inbound = self.generate(protocol, "8JCsPssfgS8tiRwiMlhARg==" if protocol == "shadowsocks" else "hy-password" if protocol == "hysteria" else UUID)
                path = self.write(protocol + "-inbound.json", inbound)
                outbound = json.loads(self.shell(
                    'generate_client "$@"', str(path), "example.com", PUBLIC, "1", check=True,
                ).stdout)
                outbound_path = self.write(protocol + "-outbound.json", outbound)
                link = self.shell('client_link "$@"', str(outbound_path), "xray-node", check=True).stdout.strip()
                self.assertTrue(link.startswith(("vless://", "vmess://", "trojan://", "ss://", "hysteria2://")))
                self.assertEqual(json.loads(self.shell(
                    'generate_client "$@"', str(path), "example.com", PUBLIC, "1", check=True,
                ).stdout)["protocol"], outbound["protocol"])

    def test_hysteria_client_auth_lives_in_hysteria_settings(self) -> None:
        inbound = self.generate("hysteria", "hy-password")
        path = self.write("hysteria-auth.json", inbound)
        outbound = json.loads(self.shell(
            'generate_client "$@"', str(path), "example.com", PUBLIC, "1", check=True,
        ).stdout)
        self.assertNotIn("auth", outbound["settings"])
        self.assertEqual(outbound["settings"]["version"], 2)
        self.assertEqual(outbound["streamSettings"]["hysteriaSettings"]["version"], 2)
        self.assertEqual(outbound["streamSettings"]["hysteriaSettings"]["auth"], "hy-password")

    def test_invalid_protocol_security_and_credentials_fail_without_echo(self) -> None:
        cases = (
            ("unknown", "secret-password"),
            ("vless-grpc-reality-vision", "secret-password"),
            ("vmess-tcp", "bad-uuid"),
            ("vless-tcp", "bad-uuid"),
            ("tuic", "secret-password"),
        )
        for protocol, credential in cases:
            with self.subTest(protocol=protocol):
                result = self.shell(
                    'generate_inbound "$@"', protocol, "bad", "443", credential, "", PRIVATE,
                    "0123456789abcdef", "example.com", "/tmp/cert", "/tmp/key", "secret-password",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertNotIn(credential, result.stderr)
                self.assertNotIn("secret-password", result.stderr)


if __name__ == "__main__":
    unittest.main()
