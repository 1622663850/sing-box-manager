#!/usr/bin/env python3
"""Post-quantum Xray protocol regressions.

The normal Xray fixture intentionally uses a small core mock which predates
the ML-KEM/ML-DSA commands.  This module replaces only that temporary core
with a deterministic command stub; no production binary, configuration, or
other test fixture is changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit

import test_xray_manager as fixture


UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
UUID2 = "0325ba41-4946-4ad5-b6b3-476eb50c37cb"
PRIVATE = "Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw"
PUBLIC = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def repeated_bytes(seed: bytes, size: int) -> bytes:
    value = bytearray()
    counter = 0
    while len(value) < size:
        value.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(value[:size])


def derived(label: str, seed: str, size: int) -> str:
    return b64url(repeated_bytes(label.encode() + b"\0" + seed.encode(), size))


def noncanonical(value: str) -> str:
    """Change only unused terminal base64 bits while preserving decoded bytes."""

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(value[-1])
    unused = 0x0F if len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))) % 3 == 1 else 0x03
    replacement = (index & ~unused) | 1
    if replacement == index:
        replacement = (index & ~unused) | 2
    return value[:-1] + alphabet[replacement]


MLKEM_SEED = b64url(bytes(range(64)))
MLDSA_SEED = b64url(bytes(range(32)))
GENERATED_MLKEM_SEED = b64url(bytes([17]) * 64)
GENERATED_MLDSA_SEED = b64url(bytes([23]) * 32)
MLKEM_CLIENT = derived("mlkem768", MLKEM_SEED, 1184)
MLDSA_VERIFY = derived("mldsa65", MLDSA_SEED, 1952)
DECRYPTION = "mlkem768x25519plus.native.600s." + MLKEM_SEED


PQ_CORE = r'''#!/usr/bin/env python3
import base64
import hashlib
import json
import os
from pathlib import Path
import sys


def b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def repeated(seed, size):
    value = bytearray()
    counter = 0
    while len(value) < size:
        value.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(value[:size])


def derived(label, seed, size):
    return b64(repeated(label.encode() + b"\0" + seed.encode(), size))


args = sys.argv[1:]
log = os.environ.get("XR_MOCK_CORE_LOG")
if log:
    with Path(log).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(args) + "\n")

if args in (["version"], ["--version"]):
    print("Xray 26.10.31-pq-test")
    raise SystemExit(0)
if args == ["uuid"] or args == ["generate", "uuid"]:
    print("b7efdc96-87aa-4910-81b4-374b2eecc737")
    raise SystemExit(0)
if args and args[0] == "x25519":
    print("PrivateKey: Cph7W9JiwwNiPF1IaxyKHmta4D5-FdwLmlPB8EF0COw")
    print("Password (PublicKey): AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    raise SystemExit(0)
if args and args[0] in {"mlkem768", "mldsa65"}:
    kind = args[0]
    size = 64 if kind == "mlkem768" else 32
    public_size = 1184 if kind == "mlkem768" else 1952
    label = "Client" if kind == "mlkem768" else "Verify"
    if os.environ.get("XR_PQ_CORE_BAD_OUTPUT") == "1":
        print("Seed: malformed")
        print(label + ": malformed")
        raise SystemExit(0)
    if "-i" in args:
        position = args.index("-i")
        if position + 1 >= len(args):
            raise SystemExit(2)
        seed = args[position + 1]
    else:
        seed = b64(bytes([17 if kind == "mlkem768" else 23]) * size)
    print("Seed: " + seed)
    print(label + ": " + derived(kind, seed, public_size))
    raise SystemExit(0)
if args and args[0] == "run":
    raise SystemExit(0)
raise SystemExit(2)
'''


WIZARD_DRIVER = r'''
wizard_mode=${1:-off}
wizard_selected=${2:-}
prompt() {
    local variable=$1 message=${2:-} default=${3:-} value
    value=$default
    case "$variable" in
        protocol_choice)
            case "$wizard_mode" in
                only-mlkem) value=2;;
                *) value=1;;
            esac
            ;;
        tag)
            if [[ -n $wizard_selected ]]; then
                value=$wizard_selected
            else
                case "$wizard_mode" in
                    off) value=pq-add-off;;
                    on) value=pq-add-on;;
                    only-mlkem) value=pq-only-mlkem;;
                    only-mldsa) value=pq-only-mldsa;;
                    *) value=pq-add-test;;
                esac
            fi
            ;;
        port)
            [[ -n $wizard_selected ]] || value=18480
            ;;
        tls_name) value=${default:-example.com};;
        server) value=${default:-example.com};;
        choice)
            case "$message" in
                *"VLESS 后量子混合加密"*)
                    case "$wizard_mode" in
                        on|only-mlkem|retain) value=2;;
                        cancel) value=0;;
                        *) value=1;;
                    esac
                    ;;
                *"REALITY 密钥"*) value=1;;
                *"REALITY ML-DSA-65"*)
                    case "$wizard_mode" in
                        on|only-mldsa|retain|target-fail) value=2;;
                        *) value=1;;
                    esac
                    ;;
            esac
            ;;
    esac
    printf -v "$variable" '%s' "$value"
}
prompt_secret() {
    local variable=$1 default=${3:-}
    printf -v "$variable" '%s' "$default"
}
confirm() { return 0; }
check_reality_pq_target() { [[ $wizard_mode != target-fail ]]; }
if [[ -n $wizard_selected ]]; then
    edit_wizard_locked "$wizard_selected"
else
    add_wizard_locked
fi
'''


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class XrayPostQuantumTest(fixture.XrayFixture):
    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("openssl"):
            self.skipTest("openssl is required for TLS template tests")
        self.certificate = self.root / "xray-pq-cert.pem"
        self.private_key = self.root / "xray-pq-key.pem"
        result = subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(self.private_key), "-out", str(self.certificate),
                "-days", "1", "-subj", "/CN=example.com",
                "-addext", "subjectAltName=DNS:example.com",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.core.write_text(PQ_CORE, encoding="utf-8")
        self.core.chmod(0o755)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.XrayFixture.shell(self, body, *args, check=check)

    def write(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def generate(
        self,
        protocol: str,
        *,
        credential: str = UUID,
        extra: str = "",
        decryption: str = "none",
        mldsa_seed: str = "",
    ) -> dict:
        result = self.shell(
            'generate_inbound "$@"',
            protocol,
            "pq-node",
            "18443",
            credential,
            extra,
            PRIVATE,
            "01234567",
            "example.com",
            str(self.certificate),
            str(self.private_key),
            "",
            "",
            decryption,
            mldsa_seed,
            check=True,
        )
        return json.loads(result.stdout)

    def pq(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self.shell('pq_crypto "$@"', *args, check=check)

    def wizard(self, mode: str, selected: str = "") -> subprocess.CompletedProcess[str]:
        return self.shell(WIZARD_DRIVER, mode, selected)

    def seed_reality(self, tag: str, *, with_pq: bool = True) -> tuple[dict, Path]:
        node = self.generate(
            "vless-reality",
            decryption=DECRYPTION if with_pq else "none",
            mldsa_seed=MLDSA_SEED if with_pq else "",
        )
        node["tag"] = tag
        self.write_config([node])
        return node, self.write_metadata(node, server="example.com", public_key=PUBLIC)

    def assert_unchanged(self, config: bytes, metadata: Path, metadata_bytes: bytes) -> None:
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(metadata.read_bytes(), metadata_bytes)
        self.assertEqual(list(self.backups_dir.iterdir()), [])

    def test_pq_crypto_generates_canonical_mlkem_and_mldsa_pairs(self) -> None:
        for kind, seed, size, public_size, expected in (
            ("mlkem768", MLKEM_SEED, 64, 1184, MLKEM_CLIENT),
            ("mldsa65", MLDSA_SEED, 32, 1952, MLDSA_VERIFY),
        ):
            with self.subTest(kind=kind):
                result = self.pq(kind, seed, check=True)
                value = json.loads(result.stdout)
                self.assertEqual(value["private"], seed)
                self.assertEqual(value["public"], expected)
                for field, length in (("private", size), ("public", public_size)):
                    raw = base64.urlsafe_b64decode(value[field] + "=" * (-len(value[field]) % 4))
                    self.assertEqual(len(raw), length)
                    self.assertEqual(b64url(raw), value[field])

        for kind, size, public_size in (("mlkem768", 64, 1184), ("mldsa65", 32, 1952)):
            with self.subTest(kind=kind, generated=True):
                value = json.loads(self.pq(kind, check=True).stdout)
                self.assertEqual(len(base64.urlsafe_b64decode(value["private"] + "=" * (-len(value["private"]) % 4))), size)
                self.assertEqual(len(base64.urlsafe_b64decode(value["public"] + "=" * (-len(value["public"]) % 4))), public_size)

    def test_pq_crypto_rejects_wrong_lengths_noncanonical_and_unknown_modes(self) -> None:
        cases = (
            ("mlkem768", MLDSA_SEED),
            ("mldsa65", MLKEM_SEED),
            ("mlkem768", MLKEM_SEED + "="),
            ("mldsa65", noncanonical(MLDSA_SEED)),
            ("unknown", MLKEM_SEED),
        )
        for kind, seed in cases:
            with self.subTest(kind=kind):
                result = self.pq(kind, seed)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "")
                self.assertNotIn(seed, result.stdout + result.stderr)

    def test_generate_inbound_pq_fields_and_legacy_none_are_preserved(self) -> None:
        node = self.generate("vless-tcp", decryption=DECRYPTION)
        self.assertEqual(node["settings"]["decryption"], DECRYPTION)
        self.assertNotIn("mldsa65Seed", node["streamSettings"].get("realitySettings", {}))

        old = json.loads(
            self.shell(
                'generate_inbound "$@"',
                "vless-tcp", "pq-node", "18443", UUID, "", PRIVATE,
                "01234567", "example.com", str(self.certificate), str(self.private_key), "", "",
                check=True,
            ).stdout
        )
        explicit = self.generate("vless-tcp", decryption="none")
        self.assertEqual(old, explicit)
        self.assertEqual(old["settings"]["decryption"], "none")

        reality = self.generate(
            "vless-reality", decryption=DECRYPTION, mldsa_seed=MLDSA_SEED
        )
        self.assertEqual(reality["settings"]["decryption"], DECRYPTION)
        self.assertEqual(reality["streamSettings"]["realitySettings"]["mldsa65Seed"], MLDSA_SEED)

    def test_generate_client_exports_pq_for_every_vless_template_without_seeds(self) -> None:
        templates = {
            "vless-tcp": "",
            "vless-tcp-tls": "",
            "vless-reality": "",
            "vless-ws": "/pq-ws",
            "vless-ws-tls": "/pq-ws",
            "vless-grpc-tls": "PqGrpc",
            "vless-grpc-reality": "PqGrpcReality",
            "vless-xhttp-tls": "/pq-xhttp",
            "vless-xhttp-reality": "/pq-xhttp",
        }
        for protocol, extra in templates.items():
            with self.subTest(protocol=protocol):
                mldsa_seed = MLDSA_SEED if "reality" in protocol else ""
                inbound = self.generate(
                    protocol,
                    extra=extra,
                    decryption=DECRYPTION,
                    mldsa_seed=mldsa_seed,
                )
                source = self.write(protocol + "-pq-inbound.json", inbound)
                result = self.shell(
                    'generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True
                )
                outbound = json.loads(result.stdout)
                self.assertEqual(outbound["protocol"], "vless")
                settings = outbound["settings"]
                self.assertEqual(settings["encryption"], "mlkem768x25519plus.native.0rtt." + MLKEM_CLIENT)
                serialized = json.dumps(outbound, ensure_ascii=False)
                self.assertNotIn(MLKEM_SEED, serialized)
                self.assertNotIn(MLDSA_SEED, serialized)
                if "reality" in protocol:
                    reality = outbound["streamSettings"]["realitySettings"]
                    self.assertEqual(reality["mldsa65Verify"], MLDSA_VERIFY)
                    self.assertNotIn("mldsa65Seed", reality)
                    self.assertNotIn("privateKey", reality)
                    self.assertNotIn("target", reality)
                    self.assertNotIn("serverNames", reality)
                    self.assertNotIn("shortIds", reality)

    def test_reality_pq_link_contains_real_values_and_parser_roundtrips_them(self) -> None:
        inbound = self.generate(
            "vless-reality", decryption=DECRYPTION, mldsa_seed=MLDSA_SEED
        )
        source = self.write("reality-pq-inbound.json", inbound)
        derived_client = json.loads(self.pq("client", str(source), check=True).stdout)
        self.assertEqual(
            derived_client,
            {
                "encryption": "mlkem768x25519plus.native.0rtt." + MLKEM_CLIENT,
                "verify": MLDSA_VERIFY,
            },
        )
        outbound = json.loads(
            self.shell(
                'generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True
            ).stdout
        )
        outbound_path = self.write("reality-pq-outbound.json", outbound)
        link = self.shell('client_link "$@"', str(outbound_path), "pq-reality", check=True).stdout.strip()
        parts = urlsplit(link)
        query = parse_qs(parts.query, keep_blank_values=True)
        self.assertEqual(query["encryption"], ["mlkem768x25519plus.native.0rtt." + MLKEM_CLIENT])
        self.assertEqual(query["pqv"], [MLDSA_VERIFY])
        self.assertNotIn(MLKEM_SEED, link)
        self.assertNotIn(MLDSA_SEED, link)
        self.assertNotIn(MLDSA_SEED, query["pqv"][0])

        link_file = self.root / "reality-pq.links"
        link_file.write_text(link + "\n", encoding="utf-8")
        parsed = json.loads(self.shell('parse_outbound_links "$1"', str(link_file), check=True).stdout)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["settings"]["encryption"], "mlkem768x25519plus.native.0rtt." + MLKEM_CLIENT)
        parsed_reality = parsed[0]["streamSettings"]["realitySettings"]
        self.assertEqual(parsed_reality["mldsa65Verify"], MLDSA_VERIFY)
        self.assertNotIn(MLKEM_SEED, json.dumps(parsed))
        self.assertNotIn(MLDSA_SEED, json.dumps(parsed))

    def test_reality_links_advertise_mihomo_hybrid_support_and_keep_pq_on_roundtrip(self) -> None:
        for suffix, mldsa_seed in (("no-mldsa", ""), ("mldsa", MLDSA_SEED)):
            with self.subTest(mode=suffix):
                inbound = self.generate(
                    "vless-reality", decryption=DECRYPTION, mldsa_seed=mldsa_seed
                )
                source = self.write("mihomo-" + suffix + "-inbound.json", inbound)
                outbound = json.loads(
                    self.shell(
                        'generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True
                    ).stdout
                )
                outbound_path = self.write("mihomo-" + suffix + "-outbound.json", outbound)
                link = self.shell(
                    'client_link "$@"', str(outbound_path), "mihomo-" + suffix, check=True
                ).stdout.strip()
                query = parse_qs(urlsplit(link).query, keep_blank_values=True)
                self.assertEqual(query["support-x25519mlkem768"], ["true"])
                self.assertEqual(query["encryption"], [outbound["settings"]["encryption"]])
                reality = outbound["streamSettings"]["realitySettings"]
                if mldsa_seed:
                    self.assertEqual(query["pqv"], [reality["mldsa65Verify"]])
                else:
                    self.assertNotIn("pqv", query)
                self.assertNotIn(MLKEM_SEED, link)
                self.assertNotIn(MLDSA_SEED, link)

                link_file = self.root / ("mihomo-" + suffix + ".links")
                link_file.write_text(link + "\n", encoding="utf-8")
                parsed = json.loads(
                    self.shell('parse_outbound_links "$1"', str(link_file), check=True).stdout
                )[0]
                self.assertEqual(parsed["settings"], outbound["settings"])
                parsed_reality = parsed["streamSettings"]["realitySettings"]
                self.assertEqual(
                    parsed_reality.get("mldsa65Verify"), reality.get("mldsa65Verify")
                )

                # The flag is an optional compatibility extension; old links
                # without it must remain importable and preserve PQ fields.
                legacy = link.replace("&support-x25519mlkem768=true", "", 1)
                link_file.write_text(legacy + "\n", encoding="utf-8")
                legacy_parsed = json.loads(
                    self.shell('parse_outbound_links "$1"', str(link_file), check=True).stdout
                )[0]
                self.assertEqual(legacy_parsed["settings"], outbound["settings"])
                self.assertEqual(
                    legacy_parsed["streamSettings"]["realitySettings"].get("mldsa65Verify"),
                    reality.get("mldsa65Verify"),
                )

        tls_inbound = self.generate("vless-tcp-tls", decryption="none")
        tls_source = self.write("mihomo-tls-inbound.json", tls_inbound)
        tls_outbound = json.loads(
            self.shell(
                'generate_client "$@"', str(tls_source), "example.com", PUBLIC, "1", check=True
            ).stdout
        )
        tls_link = self.shell(
            'client_link "$@"', str(self.write("mihomo-tls-outbound.json", tls_outbound)), "tls-node", check=True
        ).stdout.strip()
        self.assertNotIn("support-x25519mlkem768", tls_link)

    def test_reality_support_flag_rejects_invalid_values_and_non_reality_use(self) -> None:
        inbound = self.generate(
            "vless-reality", decryption=DECRYPTION, mldsa_seed=MLDSA_SEED
        )
        source = self.write("mihomo-invalid-inbound.json", inbound)
        outbound = json.loads(
            self.shell(
                'generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True
            ).stdout
        )
        link = self.shell(
            'client_link "$@"', str(self.write("mihomo-invalid-outbound.json", outbound)), "invalid-pq", check=True
        ).stdout.strip()
        for index, value in enumerate(("false", "1", "")):
            with self.subTest(value=value or "empty"):
                candidate = link.replace(
                    "support-x25519mlkem768=true", "support-x25519mlkem768=" + value, 1
                )
                path = self.root / ("mihomo-invalid-" + str(index))
                path.write_text(candidate + "\n", encoding="utf-8")
                result = self.shell('parse_outbound_links "$1"', str(path))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)
                self.assertNotIn(MLDSA_SEED, result.stdout + result.stderr)

        tls_inbound = self.generate("vless-tcp-tls", decryption="none")
        tls_source = self.write("mihomo-non-reality-inbound.json", tls_inbound)
        tls_outbound = json.loads(
            self.shell(
                'generate_client "$@"', str(tls_source), "example.com", PUBLIC, "1", check=True
            ).stdout
        )
        tls_link = self.shell(
            'client_link "$@"', str(self.write("mihomo-non-reality-outbound.json", tls_outbound)), "tls-pq", check=True
        ).stdout.strip()
        self.assertNotIn("support-x25519mlkem768", tls_link)
        non_reality = tls_link.replace(
            "security=tls", "security=tls&support-x25519mlkem768=true", 1
        )
        path = self.root / "mihomo-non-reality-invalid"
        path.write_text(non_reality + "\n", encoding="utf-8")
        result = self.shell('parse_outbound_links "$1"', str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)
        self.assertNotIn(MLDSA_SEED, result.stdout + result.stderr)

    def test_none_client_derivation_does_not_call_core_and_keeps_old_output(self) -> None:
        inbound = self.generate("vless-tcp", decryption="none")
        source = self.write("none-pq-inbound.json", inbound)
        before = self.core_calls()
        result = self.pq("client", str(source), check=True)
        self.assertEqual(json.loads(result.stdout), {"encryption": "none", "verify": ""})
        self.assertEqual(self.core_calls(), before)
        outbound = json.loads(
            self.shell('generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True).stdout
        )
        self.assertEqual(outbound["settings"]["encryption"], "none")
        self.assertEqual(self.core_calls(), before)

    def test_multi_user_exports_share_pq_material_but_not_credentials(self) -> None:
        inbound = self.generate(
            "vless-reality", decryption=DECRYPTION, mldsa_seed=MLDSA_SEED
        )
        inbound["settings"]["users"].append({"id": UUID2})
        source = self.write("multi-pq-inbound.json", inbound)
        first = json.loads(
            self.shell('generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True).stdout
        )
        second = json.loads(
            self.shell('generate_client "$@"', str(source), "example.com", PUBLIC, "2", check=True).stdout
        )
        self.assertNotEqual(first["settings"]["id"], second["settings"]["id"])
        self.assertEqual(first["settings"]["encryption"], second["settings"]["encryption"])
        self.assertEqual(
            first["streamSettings"]["realitySettings"]["mldsa65Verify"],
            second["streamSettings"]["realitySettings"]["mldsa65Verify"],
        )
        for outbound in (first, second):
            serialized = json.dumps(outbound, ensure_ascii=False)
            self.assertNotIn(MLKEM_SEED, serialized)
            self.assertNotIn(MLDSA_SEED, serialized)

    def test_pq_config_and_client_derivation_reject_bad_seeds_and_unknown_encryption(self) -> None:
        bad_decryptions = (
            "mlkem768x25519plus.native.600s." + MLDSA_SEED,
            "mlkem768x25519plus.native.600s." + noncanonical(MLKEM_SEED),
            "not-a-vless-encryption",
        )
        for value in bad_decryptions:
            with self.subTest(decryption=value[:24]):
                result = self.shell(
                    'generate_inbound "$@"',
                    "vless-tcp", "bad-pq", "18443", UUID, "", PRIVATE,
                    "01234567", "example.com", str(self.certificate), str(self.private_key), "", "", value, "",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(value, result.stdout + result.stderr)

        bad_mldsa = self.generate("vless-reality")
        bad_mldsa["streamSettings"]["realitySettings"]["mldsa65Seed"] = MLKEM_SEED
        bad_mldsa_path = self.write("bad-mldsa-seed.json", bad_mldsa)
        result = self.pq("client", str(bad_mldsa_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)

        bad_client = self.generate("vless-tcp")
        bad_client["settings"]["decryption"] = "unknown-encryption"
        bad_client_path = self.write("bad-client-encryption.json", bad_client)
        result = self.shell(
            'generate_client "$@"', str(bad_client_path), "example.com", PUBLIC, "1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unknown-encryption", result.stdout + result.stderr)

    def test_wizard_add_defaults_to_pq_off_without_core_pq_calls(self) -> None:
        result = self.wizard("off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        node = json.loads(self.shell('config_jq .inbounds[0]', check=True).stdout)
        self.assertEqual(node["tag"], "pq-add-off")
        self.assertEqual(node["settings"]["decryption"], "none")
        self.assertNotIn("mldsa65Seed", node["streamSettings"].get("realitySettings", {}))
        calls = self.core_calls()
        self.assertFalse(any(call and call[0] in {"mlkem768", "mldsa65"} for call in calls))

    def test_wizard_add_can_enable_both_pq_layers(self) -> None:
        result = self.wizard("on")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(GENERATED_MLKEM_SEED, result.stdout + result.stderr)
        self.assertNotIn(GENERATED_MLDSA_SEED, result.stdout + result.stderr)
        node = json.loads(self.shell('config_jq .inbounds[0]', check=True).stdout)
        self.assertTrue(node["settings"]["decryption"].startswith("mlkem768x25519plus.native.600s."))
        self.assertEqual(node["settings"]["decryption"].rsplit(".", 1)[-1], GENERATED_MLKEM_SEED)
        self.assertEqual(node["streamSettings"]["realitySettings"]["mldsa65Seed"], GENERATED_MLDSA_SEED)
        calls = self.core_calls()
        self.assertIn(["mlkem768"], calls)
        self.assertIn(["mldsa65"], calls)

    def test_wizard_independent_only_mlkem_and_only_mldsa_switches(self) -> None:
        mlkem = self.wizard("only-mlkem")
        self.assertEqual(mlkem.returncode, 0, mlkem.stdout + mlkem.stderr)
        first = json.loads(self.shell('config_jq .inbounds[0]', check=True).stdout)
        self.assertEqual(first["tag"], "pq-only-mlkem")
        self.assertTrue(first["settings"]["decryption"].startswith("mlkem768x25519plus.native.600s."))
        self.assertNotIn("realitySettings", first.get("streamSettings", {}))
        self.assertIn(["mlkem768"], self.core_calls())
        self.assertFalse(any(call and call[0] == "mldsa65" for call in self.core_calls()))

        # The second independent switch is exercised in a fresh isolated
        # configuration by a separate test method; this assertion keeps the
        # first case explicitly limited to ML-KEM.

    def test_wizard_independent_only_mldsa_switch(self) -> None:
        result = self.wizard("only-mldsa")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        node = json.loads(self.shell('config_jq .inbounds[0]', check=True).stdout)
        self.assertEqual(node["tag"], "pq-only-mldsa")
        self.assertEqual(node["settings"]["decryption"], "none")
        self.assertEqual(node["streamSettings"]["realitySettings"]["mldsa65Seed"], GENERATED_MLDSA_SEED)
        calls = self.core_calls()
        self.assertIn(["mldsa65"], calls)
        self.assertFalse(any(call and call[0] == "mlkem768" for call in calls))

    def test_edit_retain_keeps_reality_keys_without_generating_new_pq_material(self) -> None:
        original, metadata = self.seed_reality("retain-pq")
        self.core_log.unlink(missing_ok=True)
        result = self.wizard("retain", "retain-pq")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        reality = current["streamSettings"]["realitySettings"]
        self.assertEqual(reality["privateKey"], original["streamSettings"]["realitySettings"]["privateKey"])
        self.assertEqual(reality["mldsa65Seed"], MLDSA_SEED)
        self.assertEqual(current["settings"]["decryption"], DECRYPTION)
        self.assertEqual(current["settings"], original["settings"])
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], current)
        calls = self.core_calls()
        self.assertFalse(any(call in (["mlkem768"], ["mldsa65"]) for call in calls))
        self.assertFalse(any(call == ["x25519"] for call in calls))

    def test_edit_disabling_pq_removes_seed_and_sets_decryption_none(self) -> None:
        _, metadata = self.seed_reality("close-pq")
        result = self.wizard("close", "close-pq")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        node = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        self.assertEqual(node["settings"]["decryption"], "none")
        self.assertNotIn("mldsa65Seed", node["streamSettings"]["realitySettings"])
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["inbound"], node)

    def test_edit_cancel_and_reality_pq_target_failure_leave_config_and_metadata_unchanged(self) -> None:
        _, metadata = self.seed_reality("cancel-pq")
        before = self.config.read_bytes()
        metadata_before = metadata.read_bytes()
        cancelled = self.wizard("cancel", "cancel-pq")
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        self.assert_unchanged(before, metadata, metadata_before)

        # A second edit attempts to enable a fresh ML-DSA seed, but the
        # mocked REALITY TLS target check rejects it before confirmation or
        # any commit/backup operation.
        node = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
        node["settings"]["decryption"] = "none"
        node["streamSettings"]["realitySettings"].pop("mldsa65Seed", None)
        self.write_config([node])
        metadata.write_text(
            json.dumps({"server": "example.com", "public_key": PUBLIC, "inbound": node}) + "\n",
            encoding="utf-8",
        )
        before_failure = self.config.read_bytes()
        metadata_before_failure = metadata.read_bytes()
        failed = self.wizard("target-fail", "cancel-pq")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assert_unchanged(before_failure, metadata, metadata_before_failure)

    def test_import_rejects_invalid_encryption_pqv_length_and_wrong_security(self) -> None:
        inbound = self.generate(
            "vless-reality", decryption=DECRYPTION, mldsa_seed=MLDSA_SEED
        )
        source = self.write("import-pq-inbound.json", inbound)
        outbound = json.loads(
            self.shell(
                'generate_client "$@"', str(source), "example.com", PUBLIC, "1", check=True
            ).stdout
        )
        link = self.shell(
            'client_link "$@"', str(self.write("import-pq-outbound.json", outbound)), "pq-import", check=True
        ).stdout.strip()
        replacements = (
            (
                "encryption=mlkem768x25519plus.native.0rtt." + MLKEM_CLIENT,
                "encryption=not-a-vless-encryption",
            ),
            ("pqv=" + MLDSA_VERIFY, "pqv=short"),
            ("security=reality", "security=tls"),
        )
        for index, (old, new) in enumerate(replacements):
            with self.subTest(case=index):
                self.assertIn(old, link)
                candidate = link.replace(old, new, 1)
                path = self.root / ("invalid-pq-link-" + str(index))
                path.write_text(candidate + "\n", encoding="utf-8")
                result = self.shell('parse_outbound_links "$1"', str(path))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)
                self.assertNotIn(MLDSA_SEED, result.stdout + result.stderr)
                self.assertNotIn(MLDSA_VERIFY, result.stdout + result.stderr)

    def test_pq_core_malformed_output_with_zero_exit_is_rejected(self) -> None:
        self.env["XR_PQ_CORE_BAD_OUTPUT"] = "1"
        for kind, seed in (("mlkem768", MLKEM_SEED), ("mldsa65", MLDSA_SEED)):
            with self.subTest(kind=kind):
                result = self.pq(kind, seed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")
                self.assertNotIn("malformed", result.stdout + result.stderr)
                self.assertNotIn(seed, result.stdout + result.stderr)

        inbound = self.generate("vless-tcp", decryption=DECRYPTION)
        source = self.write("malformed-pq-client.json", inbound)
        result = self.pq("client", str(source))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)

    def test_reality_pq_target_guard_requires_sni_handshake_and_full_pq_output(self) -> None:
        ping_core = r'''#!/usr/bin/env python3
import os
import sys

if sys.argv[1:3] != ["tls", "ping"]:
    raise SystemExit(2)
print(os.environ.get("XR_PQ_PING_OUTPUT", ""), end="")
raise SystemExit(int(os.environ.get("XR_PQ_PING_RC", "0")))
'''
        self.core.write_text(ping_core, encoding="utf-8")
        self.core.chmod(0o755)

        complete = (
            "Pinging with SNI example.com\n"
            "Handshake succeeded\n"
            "TLS Version: TLS 1.3\n"
            "TLS Post-Quantum key exchange: true (X25519MLKEM768)\n"
            "Certificate chain's total length: 3501\n"
        )
        self.env["XR_PQ_PING_RC"] = "0"
        self.env["XR_PQ_PING_OUTPUT"] = complete
        passed = self.shell('check_reality_pq_target "$1"', "example.com:443")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

        failures = (
            complete.replace("3501", "3500"),
            complete.replace("true (X25519MLKEM768)", "false (X25519MLKEM768)"),
            complete.replace("TLS 1.3", "TLS 1.2"),
            (
                "Pinging with SNI example.com\nHandshake failed\n"
                "-------------------\n"
                "Pinging without SNI example.com\nHandshake succeeded\n"
                "TLS Version: TLS 1.3\n"
                "TLS Post-Quantum key exchange: true (X25519MLKEM768)\n"
                "Certificate chain's total length: 3501\n"
            ),
            (
                "Pinging with SNI example.com\nHandshake failed\n"
                "TLS Version: TLS 1.3\n"
                "TLS Post-Quantum key exchange: true (X25519MLKEM768)\n"
                "Certificate chain's total length: 3501\n"
            ),
            "Pinging with SNI example.com\nHandshake succeeded\n",
        )
        for index, output in enumerate(failures):
            with self.subTest(case=index):
                self.env["XR_PQ_PING_OUTPUT"] = output
                result = self.shell('check_reality_pq_target "$1"', "example.com:443")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("example.com:443", result.stdout + result.stderr)
                self.assertNotIn(MLKEM_SEED, result.stdout + result.stderr)
                self.assertNotIn(MLDSA_SEED, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
