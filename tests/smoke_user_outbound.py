#!/usr/bin/env python3
"""Real-core per-user outbound binding smoke test on loopback only.

The test keeps two authenticated users on each temporary inbound and verifies
that a managed rule containing ``auth_user`` affects only the selected user.
All cores, configs, certificates and listeners are created below one temporary
directory.  Nothing is installed or sent to a production service.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Iterator

from smoke_core import (
    LOOPBACK,
    check,
    client_config,
    command,
    fetch_through_socks,
    free_port,
    http_target,
    server_config,
    tls_material,
    write_json,
)
from smoke_multiusers import quiet_core, request_with_retries
from smoke_outbound_routes import manager_call, peer_http_target


PROJECT = Path(__file__).resolve().parents[1]
MANAGER_DEFAULT = PROJECT / "sing-box-manager.sh"
REMOTE_UUID = "b7efdc96-87aa-4910-81b4-374b2eecc737"
USER_NAMES = ("alice", "bob")


def manager_result(
    manager: Path,
    core: Path,
    root: Path,
    function: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash", "-c",
            'source "$1"; CORE_BIN="$2"; shift 2; confirm() { return 0; }; '
            'service_is_active() { return 1; }; "$@"',
            "user-outbound-smoke", str(manager), str(core), function, *args,
        ],
        env={**os.environ, "SB_ROOT_PREFIX": str(root)},
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=30,
    )


def manager_json(manager: Path, core: Path, root: Path, function: str, *args: str) -> dict:
    result = manager_result(manager, core, root, function, *args)
    if result.returncode:
        raise RuntimeError("manager JSON operation failed")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("manager returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("manager returned a non-object")
    return value


def _uuid(core: Path) -> str:
    value = command([str(core), "generate", "uuid"]).strip()
    if not value or len(value) != 36:
        raise RuntimeError("core returned an invalid UUID")
    return value


def _password(core: Path) -> str:
    value = command([str(core), "generate", "rand", "--base64", "16"]).strip()
    if not value:
        raise RuntimeError("core returned an empty password")
    return value


def _prepare_manager_root(root: Path, config: Path, core: Path) -> None:
    state = root / "var/lib/sing-box-manager"
    for directory in (
        config.parent,
        state / "clients",
        state / "backups",
        root / "usr/local/lib/sing-box-manager",
    ):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    (root / "usr/local/lib/sing-box-manager/.managed").write_text(
        "sing-box-manager-v1\n", encoding="utf-8",
    )
    # The locked manager helpers use the supplied CORE_BIN override.  A real
    # core path is nevertheless kept in the private manager tree so all path
    # checks see a normal, non-symlink runtime layout.
    private_core = root / "usr/local/lib/sing-box-manager/sing-box"
    try:
        private_core.symlink_to(core)
    except OSError:
        private_core.write_bytes(core.read_bytes())
        private_core.chmod(0o755)
    state.chmod(0o700)


def _remote_config(core: Path, work: Path, port: int) -> tuple[dict, Path]:
    remote = {
        "type": "vless",
        "tag": "remote-vless",
        "listen": LOOPBACK,
        "listen_port": port,
        "users": [{"uuid": REMOTE_UUID}],
    }
    config = work / "remote-vless.json"
    write_json(
        config,
        {
            "log": {"level": "warn"},
            "inbounds": [remote],
            "outbounds": [{
                "type": "direct",
                "tag": "remote-direct",
                "inet4_bind_address": "127.0.0.2",
            }],
            "route": {"final": "remote-direct"},
        },
    )
    check(core, config, work)
    return remote, config


def _direct(tag: str, address: str) -> dict:
    return {"type": "direct", "tag": tag, "inet4_bind_address": address}


def _proxy_outbound(remote_port: int) -> dict:
    return {
        "type": "vless",
        "tag": "proxy",
        "server": LOOPBACK,
        "server_port": remote_port,
        "uuid": REMOTE_UUID,
    }


def _vless_inbounds(core: Path, port: int) -> tuple[dict, dict, list[str]]:
    first, second, other = _uuid(core), _uuid(core), _uuid(core)
    main = {
        "type": "vless",
        "tag": "main-vless",
        "listen": LOOPBACK,
        "listen_port": port,
        "users": [
            {"name": USER_NAMES[0], "uuid": first},
            {"name": USER_NAMES[1], "uuid": second},
        ],
    }
    secondary = {
        "type": "vless",
        "tag": "other-vless",
        "listen": LOOPBACK,
        "listen_port": free_port(),
        "users": [{"name": USER_NAMES[0], "uuid": other}],
    }
    return main, secondary, [first, second, other]


def _hysteria_inbounds(
    core: Path,
    manager: Path,
    root: Path,
    work: Path,
    port: int,
    certificate: Path,
    private_key: Path,
) -> tuple[dict, dict, list[str]]:
    first, second, other = _password(core), _password(core), _password(core)
    generated = manager_json(
        manager,
        core,
        root,
        "generate_inbound",
        "hysteria2",
        "main-hysteria2",
        str(port),
        first,
        "",
        "",
        "",
        "localhost",
        str(certificate),
        str(private_key),
        "",
    )
    generated["listen"] = LOOPBACK
    # Exercise the real Hy2 behavior: the first user is intentionally unnamed
    # and the server-side bind helper must assign a stable unique name only
    # when the per-user rule is created.
    generated["users"][0].pop("name", None)
    generated["users"].append({"name": USER_NAMES[1], "password": second})
    secondary = copy.deepcopy(generated)
    secondary["tag"] = "other-hysteria2"
    secondary["listen_port"] = free_port()
    secondary["users"] = [{"name": USER_NAMES[0], "password": other}]
    return generated, secondary, [first, second, other]


def _client_outbound(
    protocol: str,
    server_port: int,
    credential: str,
    *,
    certificate: Path | None = None,
    manager: Path | None = None,
    core: Path | None = None,
    root: Path | None = None,
    inbound_file: Path | None = None,
    user_index: int = 1,
) -> dict:
    if protocol == "vless":
        return {
            "type": "vless",
            "tag": "proxy",
            "server": LOOPBACK,
            "server_port": server_port,
            "uuid": credential,
        }
    if not all((manager, core, root, inbound_file, certificate)):
        raise RuntimeError("Hy2 client helper is missing temporary inputs")
    result = manager_json(
        manager, core, root, "generate_client", str(inbound_file), LOOPBACK, "", str(user_index),
    )
    tls = result.get("tls", {})
    if tls.get("insecure") is True:
        raise RuntimeError("Hy2 client disabled certificate verification")
    tls["certificate"] = certificate.read_text(encoding="utf-8")
    result["tls"] = tls
    return result


def _write_client(
    work: Path,
    protocol: str,
    tag: str,
    outbound: dict,
    port: int,
) -> Path:
    path = work / f"{tag}-client.json"
    write_json(path, client_config(outbound, port))
    return path


@contextmanager
def _running_phase(
    core: Path,
    work: Path,
    server_config_path: Path,
    server_port: int,
    server_tcp: bool,
    client_paths: list[tuple[Path, int]],
) -> Iterator[None]:
    with quiet_core(core, server_config_path, server_port, work, "user-route-server", tcp_probe=server_tcp):
        contexts = []
        try:
            for index, (client_path, port) in enumerate(client_paths):
                context = quiet_core(core, client_path, port, work, f"user-route-client-{index}")
                contexts.append(context)
                context.__enter__()
            yield
        finally:
            for context in reversed(contexts):
                context.__exit__(None, None, None)


def _route_rule(inbound: str, user: str, outbound: str) -> dict:
    return {"inbound": [inbound], "auth_user": [user], "action": "route", "outbound": outbound}


def _bulk_rule(inbound: str, outbound: str) -> dict:
    return {"inbound": [inbound], "action": "route", "outbound": outbound}


def _node(value: dict, tag: str) -> dict:
    for candidate in value.get("inbounds", []):
        if candidate.get("tag") == tag:
            return candidate
    raise RuntimeError("managed inbound disappeared")


def _auth_values(node: dict) -> list[str]:
    return [str(user.get("uuid") or user.get("password") or "") for user in node.get("users", [])]


def _assert_peer(
    proxy_port: int,
    target_port: int,
    expected: bytes,
    path: str,
) -> None:
    actual = request_with_retries(proxy_port, target_port, path)
    if actual != expected:
        raise RuntimeError("per-user outbound peer did not match expected route")


def run_protocol(
    core: Path,
    manager: Path,
    root: Path,
    work: Path,
    protocol: str,
    certificate: Path | None,
    private_key: Path | None,
) -> None:
    remote_port = free_port()
    server_port, other_port = free_port(), free_port()
    while len({remote_port, server_port, other_port}) != 3:
        remote_port, server_port, other_port = free_port(), free_port(), free_port()
    _, remote_config = _remote_config(core, work, remote_port)
    if protocol == "vless":
        main, secondary, credentials = _vless_inbounds(core, server_port)
    else:
        if certificate is None or private_key is None:
            raise RuntimeError("Hy2 smoke has no temporary certificate")
        main, secondary, credentials = _hysteria_inbounds(
            core, manager, root, work, server_port, certificate, private_key,
        )
    secondary["listen_port"] = other_port
    main_tag, secondary_tag = main["tag"], secondary["tag"]
    original_main_auth = _auth_values(main)
    unnamed_primary = protocol == "hysteria2" and "name" not in main["users"][0]
    server = {
        "log": {"level": "warn"},
        "inbounds": [main, secondary],
        "outbounds": [
            _direct("direct-default", "127.0.0.1"),
            _direct("bulk", "127.0.0.3"),
            _proxy_outbound(remote_port),
        ],
        "route": {
            "rules": [{"domain_suffix": ["custom.invalid"], "action": "route", "outbound": "direct-default"}],
            "final": "direct-default",
        },
    }
    config = root / "etc/sing-box/config.json"
    write_json(config, server)
    check(core, config, work)

    client_ports = [free_port(), free_port(), free_port()]
    while len(set(client_ports)) != len(client_ports):
        client_ports = [free_port(), free_port(), free_port()]
    inbound_file = work / f"{protocol}-main-inbound.json"
    write_json(inbound_file, main)
    clients: list[tuple[Path, int]] = []
    for label, index, target_port in (
        ("main-alice", 0, client_ports[0]),
        ("main-bob", 1, client_ports[1]),
        ("other-alice", 2, client_ports[2]),
    ):
        if protocol == "vless":
            outbound = _client_outbound("vless", server_port if label != "other-alice" else other_port, credentials[index])
        else:
            current_inbound_file = inbound_file
            current_index = index + 1
            if label == "other-alice":
                current_inbound_file = work / f"{protocol}-other-inbound.json"
                write_json(current_inbound_file, secondary)
                current_index = 1
            outbound = _client_outbound(
                "hysteria2",
                server_port if label != "other-alice" else other_port,
                credentials[index],
                certificate=certificate,
                manager=manager,
                core=core,
                root=root,
                inbound_file=current_inbound_file,
                user_index=current_index,
            )
        path = _write_client(work, protocol, label, outbound, target_port)
        check(core, path, work)
        clients.append((path, target_port))

    def call(function: str, *args: str) -> str:
        return manager_call(manager, core, root, function, *args)

    # Verify the API's expected-bundle CAS path and keep the failure diagnostic
    # credential-free.  This does not depend on the metadata representation.
    expected_bundle = work / f"{protocol}-expected-bundle.json"
    expected_bundle.write_text(manager_call(manager, core, root, "snapshot_bundle"), encoding="utf-8")
    before = config.read_bytes()
    bad = manager_result(manager, core, root, "bind_user_outbound_locked", main_tag, "99", "proxy")
    if bad.returncode == 0 or config.read_bytes() != before:
        raise RuntimeError("invalid user binding was accepted or changed config")
    if any(secret in bad.stdout + bad.stderr for secret in set(credentials + [REMOTE_UUID])):
        raise RuntimeError("user binding error echoed a credential")

    proxy_ports = [port for _, port in clients]
    bulk = _bulk_rule(main_tag, "bulk")
    custom = server["route"]["rules"][0]
    route_final = server["route"]["final"]

    with quiet_core(core, remote_config, remote_port, work, f"{protocol}-remote"):
        with peer_http_target() as target_port:
            # Bulk first, then user-specific binding: the user-specific rule
            # must be inserted before bulk and override it for Alice only.
            call("bind_outbound_locked", main_tag, "bulk")
            check(core, config, work)
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.3", protocol + "/bulk-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.3", protocol + "/bulk-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/bulk-other")

            expected_bundle.write_text(manager_call(manager, core, root, "snapshot_bundle"), encoding="utf-8")
            call("bind_user_outbound_locked", main_tag, "1", "proxy", str(expected_bundle))
            check(core, config, work)
            value = json.loads(config.read_text(encoding="utf-8"))
            bound_main = _node(value, main_tag)
            per_user_name = bound_main.get("users", [{}])[0].get("name", "")
            if not per_user_name:
                raise RuntimeError("per-user binding did not assign a Hy2 user name")
            if not unnamed_primary and per_user_name != USER_NAMES[0]:
                raise RuntimeError("named user was unexpectedly renamed")
            if _auth_values(bound_main) != original_main_auth:
                raise RuntimeError("per-user binding changed Hy2/VLESS credentials")
            per_user = _route_rule(main_tag, per_user_name, "proxy")
            rules = value["route"]["rules"]
            if rules.index(per_user) > rules.index(bulk):
                raise RuntimeError("per-user rule was placed after bulk rule")
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.2", protocol + "/per-user-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.3", protocol + "/per-user-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/per-user-other")

            # Removing only the user binding restores bulk for Alice, while
            # the same-named user on the other inbound remains direct.
            call("bind_user_outbound_locked", main_tag, "1", "-")
            check(core, config, work)
            unbound_main = _node(json.loads(config.read_text(encoding="utf-8")), main_tag)
            if unnamed_primary and "name" in unbound_main["users"][0]:
                raise RuntimeError("Hy2 user name was not restored to its original absence")
            if _auth_values(unbound_main) != original_main_auth:
                raise RuntimeError("user unbind changed authentication credentials")
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.3", protocol + "/user-unbound-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.3", protocol + "/user-unbound-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/user-unbound-other")

            # Remove the first bulk rule, then add a per-user rule first and
            # add bulk afterwards.  The later bulk rule must not mask it.
            call("bind_outbound_locked", main_tag, "-")
            call("bind_user_outbound_locked", main_tag, "1", "proxy")
            call("bind_outbound_locked", main_tag, "bulk")
            check(core, config, work)
            rebound_main = _node(json.loads(config.read_text(encoding="utf-8")), main_tag)
            rebound_name = rebound_main.get("users", [{}])[0].get("name", "")
            if not rebound_name:
                raise RuntimeError("second Hy2 per-user binding did not assign a name")
            if unnamed_primary:
                # A fresh bind may legitimately choose a new generated name;
                # only the current config is authoritative for its rule.
                per_user_name = rebound_name
            elif rebound_name != per_user_name:
                raise RuntimeError("named user changed while rebinding")
            per_user = _route_rule(main_tag, per_user_name, "proxy")
            if per_user not in json.loads(config.read_text(encoding="utf-8"))["route"]["rules"]:
                raise RuntimeError("rebound per-user rule was not persisted")
            if _auth_values(rebound_main) != original_main_auth:
                raise RuntimeError("rebinding changed authentication credentials")
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.2", protocol + "/bulk-after-user-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.3", protocol + "/bulk-after-user-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/bulk-after-user-other")

            # Removing bulk now leaves the already-bound user on proxy while
            # Bob falls back to route.final.
            call("bind_outbound_locked", main_tag, "-")
            check(core, config, work)
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.2", protocol + "/bulk-unbound-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.1", protocol + "/bulk-unbound-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/bulk-unbound-other")

            call("bind_user_outbound_locked", main_tag, "1", "-")
            check(core, config, work)
            restored = json.loads(config.read_text(encoding="utf-8"))
            if restored["route"].get("final") != route_final or custom not in restored["route"].get("rules", []):
                raise RuntimeError("per-user unbind changed route final or custom rule")
            restored_main = _node(restored, main_tag)
            if unnamed_primary and "name" in restored_main["users"][0]:
                raise RuntimeError("final Hy2 unbind did not remove generated user name")
            if _auth_values(restored_main) != original_main_auth:
                raise RuntimeError("final unbind changed authentication credentials")
            if any(rule.get("inbound") == [main_tag] and "auth_user" in rule for rule in restored["route"].get("rules", [])):
                raise RuntimeError("per-user rule remained after unbind")
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.1", protocol + "/restored-alice")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.1", protocol + "/restored-bob")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/restored-other")
            # A missing metadata record must not turn an existing user route into a no-op.
            call("bind_user_outbound_locked", main_tag, "1", "proxy")
            bound = json.loads(config.read_text(encoding="utf-8"))
            preserved_name = _node(bound, main_tag)["users"][0]["name"]
            metadata_path = root / "var/lib/sing-box-manager/clients" / (main_tag + ".json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.pop("user_outbound_routes")
            write_json(metadata_path, metadata)
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.2", protocol + "/missing-record-before")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.1", protocol + "/missing-record-bob")
            call("bind_user_outbound_locked", main_tag, "1", "-")
            check(core, config, work)
            recovered = json.loads(config.read_text(encoding="utf-8"))
            if _node(recovered, main_tag)["users"][0]["name"] != preserved_name:
                raise RuntimeError("record-less unbind changed the original user name")
            if _auth_values(_node(recovered, main_tag)) != original_main_auth:
                raise RuntimeError("record-less unbind changed credentials")
            with _running_phase(core, work, config, server_port, protocol == "vless", clients):
                _assert_peer(proxy_ports[0], target_port, b"127.0.0.1", protocol + "/missing-record-unbound")
                _assert_peer(proxy_ports[1], target_port, b"127.0.0.1", protocol + "/missing-record-bob-unchanged")
                _assert_peer(proxy_ports[2], target_port, b"127.0.0.1", protocol + "/missing-record-other-unchanged")
    print(f"PASS user outbound {protocol}: per-user and bulk route precedence preserved", flush=True)


def run(core: Path, manager: Path) -> None:
    if "1.14.3" not in command([str(core), "version"]):
        raise RuntimeError("sing-box core 1.14.3 is required")
    with tempfile.TemporaryDirectory(prefix="sing-box-user-outbound-") as temporary:
        work = Path(temporary)
        certificate, private_key = tls_material(work)
        for protocol in ("vless", "hysteria2"):
            root = work / (protocol + "-manager-root")
            config = root / "etc/sing-box/config.json"
            _prepare_manager_root(root, config, core)
            run_protocol(
                core, manager, root, work, protocol,
                None if protocol == "vless" else certificate,
                None if protocol == "vless" else private_key,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path, help="sing-box 1.14.3 executable")
    parser.add_argument("--manager", type=Path, default=MANAGER_DEFAULT)
    args = parser.parse_args()
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        print("FAIL user outbound smoke: invalid core or manager path", file=sys.stderr)
        return 1
    try:
        run(core, manager)
    except Exception:
        print("FAIL user outbound smoke", file=sys.stderr)
        return 1
    print("PASS user outbound smoke: VLESS and Hy2 per-user routing passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
