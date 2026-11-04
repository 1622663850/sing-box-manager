#!/usr/bin/env python3
"""Real XHTTP + REALITY multi-user export smoke with missing metadata public key."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
import uuid

import smoke_core as smoke
import smoke_xray as xray_smoke
import smoke_xray_management as management
import smoke_xray_pq as pq


LOOPBACK = "127.0.0.1"
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
PAYLOAD = b"XRAY_REALITY_MULTI_USER_OK"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def write_json(path: Path, value: dict) -> None:
    smoke.write_json(path, value)


def manager_call(manager: Path, core: Path, root: Path, service: str,
                 function: str, *args: str, locked: bool,
                 environment: dict[str, str]):
    return management.xray_manager_call(
        manager, core, root, service, function, *args,
        locked=locked, environment=environment,
    )


def manager_json(manager: Path, core: Path, root: Path, service: str,
                 function: str, *args: str, locked: bool,
                 environment: dict[str, str]) -> dict:
    result = manager_call(manager, core, root, service, function, *args,
                          locked=locked, environment=environment)
    require(result.returncode == 0, function + " failed")
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(function + " returned invalid JSON") from error
    require(isinstance(value, dict), function + " returned a non-object")
    return value


def add_second_user(manager: Path, core: Path, root: Path, service: str,
                    tag: str, environment: dict[str, str]) -> None:
    """Drive the real menu-18 backend while keeping prompts deterministic."""
    command = r'''
source "$1" || exit 1
CORE_BIN="$2"
SERVICE="$3"
shift 3
prompt() {
    case "$1" in
        name) printf -v "$1" '%s' second-user;;
        *) printf -v "$1" '%s' "${3:-}";;
    esac
}
prompt_secret() { printf -v "$1" '%s' '22222222-2222-4222-8222-222222222222'; }
confirm() { return 0; }
with_lock change_inbound_user add "$@"
'''
    result = management.run_command(
        ["bash", "-c", command, "xray-reality-user-smoke", str(manager),
         str(core), service, tag],
        "isolated user add", env=dict(environment, XR_ROOT_PREFIX=str(root)),
        check=False, timeout=90,
    )
    require(result.returncode == 0, "menu-18 user add failed")


def show_client(manager: Path, core: Path, root: Path, service: str,
                tag: str, index: int, environment: dict[str, str]) -> dict:
    return manager_json(manager, core, root, service, "show_client", tag,
                        LOOPBACK, "json-only", str(index), locked=False,
                        environment=environment)


def request_user(core: Path, outbound: dict, target_port: int, work: Path,
                 label: str) -> bytes:
    proxy_port = smoke.free_port()
    client_file = work / (label + "-client.json")
    write_json(client_file, xray_smoke.client_config(outbound, proxy_port))
    xray_smoke.check_xray(core, client_file, work)
    with xray_smoke.running_xray(core, client_file, work, proxy_port, label + "-client"):
        return smoke.fetch_through_socks_retry(proxy_port, target_port, "/" + label)


def run(core: Path, manager: Path) -> None:
    require(os.geteuid() == 0 and Path("/run/systemd/system").is_dir(),
            "requires root with running systemd")
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    service = "xray-reality-users-" + uuid.uuid4().hex + ".service"
    unit = Path("/run/systemd/system") / service
    require(not unit.exists() and not unit.is_symlink(), "temporary unit already exists")
    created = False
    unit_bytes = b""

    with tempfile.TemporaryDirectory(prefix="xray-reality-users-") as temporary:
        work = Path(temporary)
        root = work / "root"
        state = root / "var/lib/xray-manager"
        clients = state / "clients"
        config = root / "etc/xray/config.json"
        lib = root / "usr/local/lib/xray-manager"
        for directory in (clients, state / "backups", config.parent, lib,
                          root / "etc/systemd/system"):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (lib / ".managed").write_text("xray-manager-v1\n", encoding="utf-8")
        (lib / ".managed").chmod(0o600)
        environment = dict(os.environ, XR_ROOT_PREFIX=str(root), CORE_BIN=str(core))
        pq._manager_root(environment, core, work)

        try:
            private_x25519, public_x25519 = xray_smoke._x25519(core)
            mlkem = pq.pq_pair(manager, environment, "mlkem768")
            mldsa = pq.pq_pair(manager, environment, "mldsa65")
            decryption = pq._decryption_value(mlkem["private"])
            camouflage_port = smoke.free_port()
            server_port = smoke.free_port()
            while server_port == camouflage_port:
                server_port = smoke.free_port()
            certificate, private_key = pq._large_chain_material(work)
            with smoke.http_target(PAYLOAD) as target_port, pq.large_pq_tls_target(
                core, certificate, private_key, camouflage_port, work
            ):
                inbound = pq._generate_inbound(
                    manager, environment, "vless-xhttp-reality-vision",
                    "reality-users", server_port, UUID_A, "/xhttp", "localhost",
                    None, None, decryption, mldsa["private"], private_x25519,
                    "0123456789abcdef",
                )
                inbound["listen"] = LOOPBACK
                reality = inbound["streamSettings"]["realitySettings"]
                reality["target"] = f"{LOOPBACK}:{camouflage_port}"
                reality["serverNames"] = ["localhost"]
                inbound_file = work / "inbound.json"
                write_json(inbound_file, inbound)
                config_value = xray_smoke.server_config(inbound, (target_port,))
                config_value["inbounds"] = []
                write_json(config, config_value)
                xray_smoke.check_xray(core, config, work)
                unit_bytes = management.unit_source(core, state, config)
                with unit.open("xb") as stream:
                    stream.write(unit_bytes)
                    created = True
                unit.chmod(0o644)
                management.systemd(systemctl, "daemon-reload")

                added = manager_call(manager, core, root, service,
                                     "add_managed_inbound", str(inbound_file),
                                     LOOPBACK, public_x25519, locked=True,
                                     environment=environment)
                require(added.returncode == 0, "initial REALITY inbound add failed")
                management.active_pid(systemctl, service)
                metadata_path = clients / "reality-users.json"
                before_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                require(before_meta.get("public_key") == public_x25519,
                        "initial REALITY metadata public key missing")
                metadata_missing = dict(before_meta)
                metadata_missing.pop("public_key")
                metadata_path.write_text(json.dumps(metadata_missing) + "\n",
                                         encoding="utf-8")
                metadata_path.chmod(0o600)
                original_inbound = json.loads(config.read_text(encoding="utf-8"))["inbounds"][0]
                original_mldsa = original_inbound["streamSettings"]["realitySettings"]["mldsa65Seed"]
                original_private = original_inbound["streamSettings"]["realitySettings"]["privateKey"]

                add_second_user(manager, core, root, service, "reality-users", environment)
                after_add = json.loads(config.read_text(encoding="utf-8"))["inbounds"][0]
                users = after_add["settings"]["users"]
                require(len(users) == 2 and users[0]["id"] == UUID_A and
                        users[1]["id"] == UUID_B, "multi-user credentials changed")
                require(after_add["streamSettings"]["realitySettings"]["privateKey"] == original_private and
                        after_add["streamSettings"]["realitySettings"]["mldsa65Seed"] == original_mldsa,
                        "REALITY shared keys changed")
                metadata_after_add = json.loads(metadata_path.read_text(encoding="utf-8"))
                require("public_key" not in metadata_after_add,
                        "test metadata unexpectedly regained cached public key")
                metadata_before_exports = metadata_path.read_bytes()

                outbound_a = show_client(manager, core, root, service, "reality-users", 1, environment)
                outbound_b = show_client(manager, core, root, service, "reality-users", 2, environment)
                for outbound, expected_uuid in ((outbound_a, UUID_A), (outbound_b, UUID_B)):
                    require(outbound["settings"]["id"] == expected_uuid,
                            "export user credential mismatch")
                    export_reality = outbound["streamSettings"]["realitySettings"]
                    require(export_reality["password"] == public_x25519 and
                            export_reality.get("mldsa65Verify") == mldsa["public"],
                            "export REALITY public key mismatch")
                    require(private_x25519 not in json.dumps(outbound) and
                            mldsa["private"] not in json.dumps(outbound),
                            "server key leaked into export")
                require(metadata_path.read_bytes() == metadata_before_exports,
                        "export changed metadata")
                require(not list((state / "backups").iterdir()),
                        "user add/export created automatic backup")
                require(request_user(core, outbound_a, target_port, work, "user-a") == PAYLOAD,
                        "first REALITY user failed HTTP forwarding")
                require(request_user(core, outbound_b, target_port, work, "user-b") == PAYLOAD,
                        "second REALITY user failed HTTP forwarding")
                print("PASS missing-metadata REALITY multi-user export and forwarding", flush=True)
        finally:
            if created:
                management.systemd(systemctl, "stop", service, check=False)
                require(management.systemd(systemctl, "is-active", "--quiet", service,
                                            check=False).returncode != 0,
                        "temporary service remained active")
                management.systemd(systemctl, "reset-failed", service, check=False)
                require(unit.is_file() and not unit.is_symlink() and
                        unit.read_bytes() == unit_bytes, "temporary unit changed")
                unit.unlink()
                management.systemd(systemctl, "daemon-reload")
        require(not unit.exists(), "temporary unit cleanup failed")
    print("PASS REALITY multi-user smoke cleanup", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(args.core.resolve(), args.manager.resolve())
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno)
                               for frame in frames[-3:])
        print(f"FAIL REALITY multi-user smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
