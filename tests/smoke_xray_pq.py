#!/usr/bin/env python3
"""Real Xray post-quantum loopback smoke tests.

The test uses only temporary loopback listeners.  The manager is sourced with
``XR_ROOT_PREFIX`` pointing at a temporary tree, and the supplied Xray core is
linked into that tree so PQ seed derivation exercises the same core binary as
the server/client processes.  No production configuration, service, or
certificate is touched.

Example (Linux):

    python3 tests/smoke_xray_pq.py --core /usr/local/bin/xray
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from typing import Iterator

from smoke_core import fetch_through_socks, http_target, write_json
from smoke_xray import (
    LOOPBACK,
    _manager_result,
    _x25519,
    check_xray,
    client_config,
    fetch_through_socks_retry,
    free_port,
    prepare_tls_outbound,
    running_xray,
    server_config,
)


PROJECT = Path(__file__).resolve().parents[1]
MANAGER_DEFAULT = PROJECT / "xray-manager.sh"
HTTP_PAYLOAD = b"XRAY_PQ_LOOPBACK_OK"
UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"


def manager_json(
    manager: Path,
    environment: dict[str, str],
    function: str,
    *args: str,
) -> dict:
    result = _manager_result(manager, environment, function, *args)
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Xray manager returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("Xray manager returned a non-object")
    return value


def _decode_b64(value: object, expected_length: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise RuntimeError("PQ helper returned an invalid key field")
    try:
        raw = value.encode("ascii")
        padded = raw + b"=" * ((4 - len(raw) % 4) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, UnicodeError, base64.binascii.Error) as error:
        raise RuntimeError("PQ helper returned an invalid base64 key") from error
    if len(decoded) != expected_length:
        raise RuntimeError("PQ helper returned an unexpected key length")
    return decoded


def pq_pair(manager: Path, environment: dict[str, str], algorithm: str, seed: str = "") -> dict[str, str]:
    args = [algorithm]
    if seed:
        args.append(seed)
    value = manager_json(manager, environment, "pq_crypto", *args)
    if set(value) != {"private", "public"}:
        raise RuntimeError("PQ helper returned an unexpected object")
    expected = {"mlkem768": (64, 1184), "mldsa65": (32, 1952)}.get(algorithm)
    if expected is None:
        raise RuntimeError("unsupported PQ algorithm in smoke test")
    _decode_b64(value["private"], expected[0])
    _decode_b64(value["public"], expected[1])
    return {"private": str(value["private"]), "public": str(value["public"])}


def _decryption_value(private_seed: str) -> str:
    """Build the documented VLESS Encryption server value from a seed."""
    if private_seed.startswith("mlkem768x25519plus."):
        return private_seed
    return "mlkem768x25519plus.native.600s." + private_seed


def _manager_root(environment: dict[str, str], core: Path, work: Path) -> None:
    root = Path(environment["XR_ROOT_PREFIX"])
    core_path = root / "usr/local/lib/xray-manager/xray"
    core_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        core_path.symlink_to(core)
    except (FileExistsError, OSError):
        shutil.copy2(core, core_path)
        core_path.chmod(0o755)
    marker = core_path.parent / ".managed"
    marker.write_text("xray-manager-v1\n", encoding="utf-8")
    marker.chmod(0o600)
    config = root / "etc/xray/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    write_json(config, {"inbounds": [], "outbounds": []})


def _generate_inbound(
    manager: Path,
    environment: dict[str, str],
    template: str,
    tag: str,
    port: int,
    credential: str,
    extra: str,
    tls_name: str,
    certificate: Path | None,
    private_key: Path | None,
    decryption: str,
    mldsa_seed: str = "",
    reality_private: str = "",
    reality_short_id: str = "",
) -> dict:
    return manager_json(
        manager,
        environment,
        "generate_inbound",
        template,
        tag,
        str(port),
        credential,
        extra,
        reality_private,
        reality_short_id,
        tls_name,
        str(certificate or ""),
        str(private_key or ""),
        "",
        "",
        decryption,
        mldsa_seed,
    )


def _generate_client(
    manager: Path,
    environment: dict[str, str],
    inbound: dict,
    work: Path,
    label: str,
    public_key: str = "",
) -> dict:
    inbound_file = work / (label + "-inbound.json")
    write_json(inbound_file, inbound)
    outbound = manager_json(
        manager,
        environment,
        "generate_client",
        str(inbound_file),
        LOOPBACK,
        public_key,
        "1",
    )
    if outbound.get("protocol") != "vless" or not isinstance(outbound.get("settings"), dict):
        raise RuntimeError("Xray manager returned an invalid PQ VLESS outbound")
    return outbound


def _assert_pq_pair(inbound: dict, outbound: dict) -> None:
    server_decryption = inbound.get("settings", {}).get("decryption")
    client_encryption = outbound.get("settings", {}).get("encryption")
    if not isinstance(server_decryption, str) or not server_decryption.startswith("mlkem768x25519plus."):
        raise RuntimeError("server VLESS Encryption was not enabled")
    if not isinstance(client_encryption, str) or not client_encryption.startswith("mlkem768x25519plus."):
        raise RuntimeError("client VLESS Encryption was not derived")


def _run_pq_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    label: str,
    template: str,
    extra: str,
    decryption: str,
    certificate: Path | None,
    private_key: Path | None,
    *,
    tls_name: str = "",
    wrong_decryption: str,
) -> None:
    server_port, proxy_port = free_port(), free_port()
    while server_port == proxy_port:
        proxy_port = free_port()
    inbound = _generate_inbound(
        manager,
        environment,
        template,
        "pq-" + label,
        server_port,
        UUID,
        extra,
        tls_name,
        certificate,
        private_key,
        decryption,
    )
    inbound["listen"] = LOOPBACK
    outbound = _generate_client(manager, environment, inbound, work, label)
    _assert_pq_pair(inbound, outbound)
    if certificate is not None:
        prepare_tls_outbound(outbound, certificate, label)
    if template == "vless-ws-tls":
        stream = outbound.get("streamSettings", {})
        if stream.get("method") != "websocket" or stream.get("security") != "tls":
            raise RuntimeError("PQ WebSocket TLS outbound lost its transport")
    outbound_file = work / (label + "-outbound.json")
    write_json(outbound_file, outbound)
    wrong_inbound = copy.deepcopy(inbound)
    wrong_inbound["settings"]["decryption"] = wrong_decryption
    wrong_outbound = _generate_client(manager, environment, wrong_inbound, work, label + "-wrong")
    _assert_pq_pair(wrong_inbound, wrong_outbound)
    if certificate is not None:
        prepare_tls_outbound(wrong_outbound, certificate, label + "-wrong")
    wrong_client_file = work / (label + "-wrong-client.json")
    write_json(wrong_client_file, client_config(wrong_outbound, proxy_port))
    server_file = work / (label + "-server.json")
    client_file = work / (label + "-client.json")
    target_port = free_port()
    write_json(server_file, server_config(inbound, (target_port,)))
    write_json(client_file, client_config(outbound, proxy_port))
    check_xray(core, server_file, work)
    check_xray(core, client_file, work)
    with http_target(HTTP_PAYLOAD) as http_port:
        # The route target is selected after allocating the HTTP listener so no
        # process ever receives a production/private destination.
        server_config_value = server_config(inbound, (http_port,))
        write_json(server_file, server_config_value)
        check_xray(core, server_file, work)
        check_xray(core, wrong_client_file, work)
        with running_xray(core, server_file, work, server_port, label + "-server"):
            with running_xray(core, client_file, work, proxy_port, label + "-client"):
                if fetch_through_socks_retry(proxy_port, http_port, "/" + label) != HTTP_PAYLOAD:
                    raise RuntimeError("PQ VLESS loopback payload mismatch")
            # The wrong client must start and pass core validation; only the
            # real application request may establish the negative result.
            with running_xray(core, wrong_client_file, work, proxy_port, label + "-wrong-client"):
                try:
                    fetch_through_socks(proxy_port, http_port, "/wrong")
                except Exception:
                    pass
                else:
                    raise RuntimeError("wrong PQ VLESS client was accepted")
            with running_xray(core, client_file, work, proxy_port, label + "-client-after-negative"):
                if fetch_through_socks_retry(proxy_port, http_port, "/" + label + "-after") != HTTP_PAYLOAD:
                    raise RuntimeError("PQ VLESS loopback failed after negative client")
    print(f"PASS xray PQ {label}: VLESS Encryption loopback forwarding", flush=True)


@contextmanager
def large_pq_tls_target(
    core: Path,
    certificate: Path,
    private_key: Path,
    port: int,
    work: Path,
) -> Iterator[None]:
    """Run an isolated Xray TLS target with a forced hybrid key share."""
    config = work / f"pq-tls-target-{port}.json"
    write_json(
        config,
        {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "pq-tls-target",
                "listen": LOOPBACK,
                "port": port,
                "protocol": "http",
                "settings": {},
                "streamSettings": {
                    "network": "raw",
                    "security": "tls",
                    "tlsSettings": {
                        "alpn": ["h2", "http/1.1"],
                        "curvePreferences": ["X25519MLKEM768"],
                        "certificates": [{
                            "certificateFile": str(certificate),
                            "keyFile": str(private_key),
                        }],
                    },
                },
            }],
            "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {}}],
            "routing": {"domainStrategy": "AsIs", "rules": []},
        },
    )
    check_xray(core, config, work)
    with running_xray(core, config, work, port, "pq-reality-target"):
        yield


def _large_chain_material(work: Path) -> tuple[Path, Path]:
    certificate, private_key = work / "pq-large-cert.pem", work / "pq-large-key.pem"
    san = ",".join(["DNS:localhost"] + [f"DNS:pq-{index}.invalid" for index in range(160)])
    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=localhost", "-addext", f"subjectAltName={san}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("could not create temporary PQ TLS certificate")
    der = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-outform", "DER"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
    )
    if der.returncode or len(der.stdout) <= 3500:
        raise RuntimeError("temporary PQ REALITY certificate chain is too small")
    certificate.chmod(0o600)
    private_key.chmod(0o600)
    return certificate, private_key


def _tls_ping(core: Path, port: int) -> None:
    result = subprocess.run(
        [str(core), "tls", "ping", "-ip", LOOPBACK, f"localhost:{port}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Xray TLS target ping failed")
    output = result.stdout
    without_sni = output.split("Pinging with SNI", 1)[0]
    if "Pinging without SNI" not in without_sni or "Handshake succeeded" not in without_sni:
        raise RuntimeError("Xray TLS target without-SNI handshake failed")
    match = re.search(r"Certificate chain's total length:\s*(\d+)", without_sni)
    if not match or int(match.group(1)) <= 3500:
        raise RuntimeError("Xray TLS target did not send a large certificate chain")
    if not re.search(r"TLS Post-Quantum key exchange:\s*true\s*\(X25519MLKEM768\)", without_sni, re.I):
        raise RuntimeError("Xray TLS target did not negotiate X25519MLKEM768")


def _run_reality_pq_case(
    core: Path,
    manager: Path,
    environment: dict[str, str],
    work: Path,
    mlkem_decryption: str,
    mldsa_seed: str,
    mldsa_public: str,
    wrong_mldsa_public: str,
    certificate: Path,
    private_key: Path,
    *,
    template: str = "vless-reality",
) -> None:
    private_x25519, public_x25519 = _x25519(core)
    server_port, proxy_port, camouflage_port = free_port(), free_port(), free_port()
    while len({server_port, proxy_port, camouflage_port}) != 3:
        server_port, proxy_port, camouflage_port = free_port(), free_port(), free_port()
    inbound = _generate_inbound(
        manager,
        environment,
        template,
        "pq-reality",
        server_port,
        UUID,
        "/pq-xhttp" if template == "vless-xhttp-reality-vision" else "localhost",
        "localhost",
        None,
        None,
        mlkem_decryption,
        mldsa_seed,
        private_x25519,
        "0123456789abcdef",
    )
    inbound["listen"] = LOOPBACK
    reality = inbound.get("streamSettings", {}).get("realitySettings", {})
    if not isinstance(reality, dict):
        raise RuntimeError("manager did not return REALITY settings")
    reality["target"] = f"{LOOPBACK}:{camouflage_port}"
    reality["serverNames"] = ["localhost"]
    reality["mldsa65Seed"] = mldsa_seed
    outbound = _generate_client(manager, environment, inbound, work, "pq-reality", public_x25519)
    _assert_pq_pair(inbound, outbound)
    if inbound.get("settings", {}).get("users", [{}])[0].get("flow") != "xtls-rprx-vision" or \
       outbound.get("settings", {}).get("flow") != "xtls-rprx-vision":
        raise RuntimeError("PQ REALITY Vision flow was not preserved")
    client_reality = outbound.get("streamSettings", {}).get("realitySettings", {})
    if client_reality.get("mldsa65Verify") != mldsa_public:
        raise RuntimeError("client REALITY ML-DSA verify key was not derived")
    outbound["streamSettings"]["realitySettings"]["serverName"] = "localhost"
    outbound["streamSettings"]["realitySettings"]["password"] = public_x25519
    outbound["streamSettings"]["realitySettings"]["shortId"] = "0123456789abcdef"
    outbound_file = work / "pq-reality-outbound.json"
    write_json(outbound_file, outbound)
    server_file = work / "pq-reality-server.json"
    client_file = work / "pq-reality-client.json"
    with http_target(HTTP_PAYLOAD) as http_port:
        write_json(server_file, server_config(inbound, (http_port,)))
        write_json(client_file, client_config(outbound, proxy_port))
        check_xray(core, server_file, work)
        check_xray(core, client_file, work)
        with large_pq_tls_target(core, certificate, private_key, camouflage_port, work):
            _tls_ping(core, camouflage_port)
            with running_xray(core, server_file, work, server_port, "pq-reality-server"):
                with running_xray(core, client_file, work, proxy_port, "pq-reality-client"):
                    if fetch_through_socks_retry(proxy_port, http_port, "/pq-reality-before") != HTTP_PAYLOAD:
                        raise RuntimeError("PQ REALITY loopback payload mismatch before negative")
                wrong = copy.deepcopy(outbound)
                wrong["streamSettings"]["realitySettings"]["mldsa65Verify"] = wrong_mldsa_public
                wrong_file = work / "pq-reality-wrong-client.json"
                write_json(wrong_file, client_config(wrong, proxy_port))
                check_xray(core, wrong_file, work)
                with running_xray(core, wrong_file, work, proxy_port, "pq-reality-wrong-client"):
                    try:
                        fetch_through_socks(proxy_port, http_port, "/wrong-verify")
                    except Exception:
                        pass
                    else:
                        raise RuntimeError("wrong REALITY ML-DSA verify key was accepted")
                with running_xray(core, client_file, work, proxy_port, "pq-reality-client"):
                    if fetch_through_socks_retry(proxy_port, http_port, "/pq-reality") != HTTP_PAYLOAD:
                        raise RuntimeError("PQ REALITY loopback payload mismatch")
    print(f"PASS xray PQ {template}: Vision, ML-KEM and ML-DSA verification", flush=True)


def _assert_core_version(core: Path) -> None:
    result = subprocess.run(
        [str(core), "version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode or "26.10.31" not in result.stdout:
        raise RuntimeError("Xray core 26.10.31 is required for the PQ smoke")


def run(core: Path, manager: Path) -> None:
    _assert_core_version(core)
    with tempfile.TemporaryDirectory(prefix="xray-pq-loopback-") as temporary:
        work = Path(temporary)
        root_prefix = work / "manager-prefix"
        environment = os.environ.copy()
        environment["XR_ROOT_PREFIX"] = str(root_prefix)
        _manager_root(environment, core, work)
        mlkem = pq_pair(manager, environment, "mlkem768")
        mldsa = pq_pair(manager, environment, "mldsa65")
        mlkem_seed = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
        mldsa_seed = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        mlkem_seed_pair = pq_pair(manager, environment, "mlkem768", mlkem_seed)
        mldsa_seed_pair = pq_pair(manager, environment, "mldsa65", mldsa_seed)
        wrong_mlkem_seed = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
        wrong_mldsa_seed = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        wrong_mlkem_pair = pq_pair(manager, environment, "mlkem768", wrong_mlkem_seed)
        wrong_mldsa_pair = pq_pair(manager, environment, "mldsa65", wrong_mldsa_seed)
        decryption = _decryption_value(mlkem_seed_pair["private"])
        wrong_decryption = _decryption_value(wrong_mlkem_pair["private"])
        certificate, private_key = _large_chain_material(work)
        _run_pq_case(
            core, manager, environment, work, "raw", "vless-tcp", "", decryption,
            None, None, wrong_decryption=wrong_decryption,
        )
        _run_pq_case(
            core, manager, environment, work, "ws-tls", "vless-ws-tls", "/pq-ws",
            decryption, certificate, private_key, tls_name="localhost", wrong_decryption=wrong_decryption,
        )
        _run_reality_pq_case(
            core,
            manager,
            environment,
            work,
            decryption,
            mldsa_seed_pair["private"],
            mldsa_seed_pair["public"],
            wrong_mldsa_pair["public"],
            certificate,
            private_key,
        )
        _run_reality_pq_case(
            core, manager, environment, work, decryption,
            mldsa_seed_pair["private"], mldsa_seed_pair["public"],
            wrong_mldsa_pair["public"], certificate, private_key,
            template="vless-xhttp-reality-vision",
        )
        # Keep the no-seed calls live as well: the generated key material is
        # checked above and these calls exercise the deterministic default path.
        if not mlkem.get("private") or not mldsa.get("private"):
            raise RuntimeError("PQ default key generation returned empty material")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path, help="Xray v26.10.31 executable")
    parser.add_argument("--manager", type=Path, default=MANAGER_DEFAULT)
    args = parser.parse_args()
    core = args.core.expanduser().resolve()
    manager = args.manager.expanduser().resolve()
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        print("FAIL xray PQ smoke: invalid core or manager path", file=sys.stderr)
        return 1
    try:
        run(core, manager)
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-3:])
        print(f"FAIL xray PQ smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        return 1
    print("PASS xray PQ smoke: all post-quantum loopback cases passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
