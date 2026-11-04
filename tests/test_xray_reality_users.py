#!/usr/bin/env python3
"""Regression coverage for REALITY client export after adding inbound users."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import unittest

import test_xray_manager as fixture
import test_xray_pq as pq_fixture


UUID2 = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
TAG = "vless-xhttp-reality-184134"
PATH = "/xray"


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class XrayRealityUsersTest(fixture.XrayFixture):
    """Adding a second user must not make REALITY exports lose the public key."""

    def setUp(self) -> None:
        super().setUp()
        self.certificate = self.root / "unused-cert.pem"
        self.private_key = self.root / "unused-key.pem"
        self.certificate.write_text("", encoding="utf-8")
        self.private_key.write_text("", encoding="utf-8")
        self.core.write_text(pq_fixture.PQ_CORE, encoding="utf-8")
        self.core.chmod(0o755)

    def generate(self, protocol: str, *, credential: str = pq_fixture.UUID,
                 extra: str = "", decryption: str = "none",
                 mldsa_seed: str = "") -> dict:
        return pq_fixture.XrayPostQuantumTest.generate(
            self, protocol, credential=credential, extra=extra,
            decryption=decryption, mldsa_seed=mldsa_seed,
        )

    def make_node(self) -> dict:
        node = self.generate(
            "vless-xhttp-reality",
            credential=pq_fixture.UUID,
            extra=PATH,
            decryption=pq_fixture.DECRYPTION,
            mldsa_seed=pq_fixture.MLDSA_SEED,
        )
        node["tag"] = TAG
        return node

    def add_second_user(self, node: dict, *, cached_public_key: str | None = pq_fixture.PUBLIC) -> Path:
        self.write_config([node])
        metadata = self.write_metadata(node, server="example.com", public_key=cached_public_key or "")
        if cached_public_key is None:
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value.pop("public_key", None)
            metadata.write_text(json.dumps(value) + "\n", encoding="utf-8")
        driver = r'''
prompt() {
    case "$1" in
        name) printf -v "$1" '%s' added-user ;;
        *) printf -v "$1" '%s' "${3:-}" ;;
    esac
}
prompt_secret() {
    if [[ $1 == credential ]]; then
        printf -v "$1" '%s' '0325ba41-4946-4ad5-b6b3-476eb50c37cb'
    else
        printf -v "$1" '%s' "${3:-}"
    fi
}
confirm() { return 0; }
change_inbound_user add "$1"
'''
        result = self.shell(driver, TAG)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        users = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["users"]
        self.assertEqual([user["id"] for user in users], [pq_fixture.UUID, UUID2])
        self.assertEqual(users[1]["email"], "added-user")
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"]["settings"]["users"], users)
        return metadata

    def export_all_users(self) -> list[dict]:
        result = self.shell('show_client "$@"', TAG, "example.com", "json-only", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exported = []
        decoder = json.JSONDecoder()
        offset = 0
        while offset < len(result.stdout):
            while offset < len(result.stdout) and result.stdout[offset].isspace():
                offset += 1
            if offset >= len(result.stdout):
                break
            value, offset = decoder.raw_decode(result.stdout, offset)
            exported.append(value)
        self.assertEqual(len(exported), 2, result.stdout)
        for item, expected in zip(exported, (pq_fixture.UUID, UUID2)):
            self.assertEqual(item["settings"]["id"], expected)
            reality = item["streamSettings"]["realitySettings"]
            self.assertEqual(reality["password"], pq_fixture.PUBLIC)
            serialized = json.dumps(item, ensure_ascii=False)
            self.assertNotIn(pq_fixture.PRIVATE, serialized)
            self.assertNotIn(pq_fixture.MLDSA_SEED, serialized)
            self.assertNotIn(pq_fixture.MLKEM_SEED, serialized)
        link = self.shell('show_client "$@"', TAG, "example.com", "link-only", "all")
        self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
        self.assertEqual(len([line for line in link.stdout.splitlines() if line.startswith("vless://")]), 2)
        return exported

    def test_menu18_add_keeps_complete_reality_metadata_and_exports_all_users(self) -> None:
        node = self.make_node()
        self.add_second_user(node)
        self.export_all_users()
        self.assertEqual(list(self.backups_dir.iterdir()), [])

    def test_missing_public_key_is_derived_from_existing_reality_private_key(self) -> None:
        node = self.make_node()
        self.add_second_user(node, cached_public_key=None)
        self.core_log.unlink(missing_ok=True)
        self.export_all_users()
        self.assertIn(["x25519", "-i", pq_fixture.PRIVATE], self.core_calls())

    def test_config_list_exports_every_user_when_cached_public_key_is_missing(self) -> None:
        self.add_second_user(self.make_node(), cached_public_key=None)
        result = self.shell('show_config_list')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("客户端配置无法安全导出", result.stderr)
        self.assertNotIn("此节点导出失败", result.stderr)
        self.assertEqual(sum(line.startswith("vless://") for line in result.stdout.splitlines()), 2)

    def test_empty_or_wrong_cached_public_key_is_not_trusted(self) -> None:
        node = self.make_node()
        metadata = self.add_second_user(node)
        for cached in ("", "B" * 43):
            with self.subTest(cached=cached):
                value = json.loads(metadata.read_text(encoding="utf-8"))
                value["public_key"] = cached
                metadata.write_text(json.dumps(value) + "\n", encoding="utf-8")
                self.core_log.unlink(missing_ok=True)
                self.export_all_users()
                self.assertIn(["x25519", "-i", pq_fixture.PRIVATE], self.core_calls())

    def test_missing_or_stale_metadata_still_exports_with_explicit_server(self) -> None:
        node = self.make_node()
        metadata = self.add_second_user(node)
        metadata.unlink()
        self.export_all_users()
        stale = self.clients_dir / f"{TAG}.json"
        stale.write_text(json.dumps({"server": "example.com", "inbound": {"tag": TAG, "port": 1}}) + "\n", encoding="utf-8")
        self.export_all_users()

    def test_invalid_reality_private_key_fails_without_generating_a_new_key(self) -> None:
        node = self.make_node()
        self.add_second_user(node)
        changed = copy.deepcopy(node)
        changed["streamSettings"]["realitySettings"]["privateKey"] = "invalid"
        self.write_config([changed])
        self.core_log.unlink(missing_ok=True)
        result = self.shell('show_client "$@"', TAG, "example.com", "json-only", "all")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("invalid", result.stdout + result.stderr)
        self.assertFalse(any(call == ["x25519"] for call in self.core_calls()))


if __name__ == "__main__":
    unittest.main()
