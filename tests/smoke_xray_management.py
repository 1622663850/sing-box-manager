#!/usr/bin/env python3
"""Real systemd transaction smoke test for the standalone Xray manager.

This test is intentionally separate from the protocol smoke test.  It creates
one uniquely named transient unit under ``/run/systemd/system`` and gives the
manager a private ``XR_ROOT_PREFIX``.  Only that unit is started, restarted
and stopped; the production Xray/sing-box units are never addressed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

import smoke_core as smoke
import smoke_xray as xray_smoke


LOOPBACK = smoke.LOOPBACK
HTTP_PAYLOAD = b"XRAY_MANAGEMENT_LOOPBACK_OK"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_command(
    args: list[str],
    label: str,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 45,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        # Never include service/core diagnostics: they may contain credentials.
        raise RuntimeError(label + " failed")
    return result


def systemd(systemctl: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command([systemctl, *args], "systemd operation", check=check, timeout=45)


def wait_listener(port: int, *, present: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.2):
                connected = True
        except OSError:
            connected = False
        if connected == present:
            return
        time.sleep(0.1)
    raise RuntimeError("isolated Xray listener state did not settle")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("isolated JSON state is invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("isolated JSON state is not an object")
    return value


def write_json(path: Path, value: dict) -> None:
    smoke.write_json(path, value)


def unit_source(core: Path, state: Path, config: Path) -> bytes:
    # WorkingDirectory must remain an unquoted systemd path.  ExecStart is
    # quoted defensively because systemd accepts quoted executable arguments.
    return (
        "[Unit]\n"
        "Description=Xray manager isolated smoke unit\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={state}\n"
        f'ExecStart="{core}" run -config "{config}"\n'
        "Restart=on-failure\n"
        "RestartSec=1s\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "StandardOutput=null\n"
        "StandardError=null\n"
        "\n[Install]\n"
        "WantedBy=multi-user.target\n"
    ).encode("utf-8")


def xray_manager_call(
    manager: Path,
    core: Path,
    root: Path,
    service: str,
    function: str,
    *args: str,
    locked: bool,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    if locked:
        body = 'with_lock "$@"'
    else:
        body = '"$@"'
    command = (
        'source "$1" || exit 1; CORE_BIN="$2"; SERVICE="$3"; shift 3; '
        'confirm() { return 0; }; '
        + body
    )
    env = dict(environment, XR_ROOT_PREFIX=str(root))
    return run_command(
        [
            "bash", "-c", command, "xray-management-smoke", str(manager),
            str(core), service, function, *args,
        ],
        "Xray manager operation",
        env=env,
        check=False,
        timeout=60,
    )


def manager_json(
    manager: Path,
    core: Path,
    root: Path,
    service: str,
    function: str,
    *args: str,
    locked: bool,
    environment: dict[str, str],
) -> dict:
    result = xray_manager_call(
        manager, core, root, service, function, *args,
        locked=locked, environment=environment,
    )
    require(result.returncode == 0, "Xray manager operation failed")
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Xray manager returned invalid JSON") from error
    require(isinstance(value, dict), "Xray manager returned a non-object")
    return value


def active_pid(systemctl: str, service: str) -> int:
    systemd(systemctl, "is-active", "--quiet", service)
    state = run_command(
        [systemctl, "show", service, "--property=ActiveState", "--value"],
        "isolated service state",
    ).stdout.strip()
    require(state == "active", "isolated service is not active")
    value = run_command(
        [systemctl, "show", service, "--property=MainPID", "--value"],
        "isolated service PID",
    ).stdout.strip()
    try:
        pid = int(value)
    except ValueError as error:
        raise RuntimeError("isolated service PID is invalid") from error
    require(pid > 0, "isolated service has no main process")
    return pid


def node(tag: str, port: int, credential: str) -> dict:
    return {
        "protocol": "vless",
        "tag": tag,
        "listen": LOOPBACK,
        "port": port,
        "settings": {
            "clients": [{"id": credential}],
            "decryption": "none",
        },
        "streamSettings": {"method": "raw", "security": "none"},
    }


def client_config(outbound: dict, port: int) -> dict:
    return xray_smoke.client_config(outbound, port)


def probe_client(
    core: Path,
    manager: Path,
    root: Path,
    service: str,
    environment: dict[str, str],
    work: Path,
    node_file: Path,
    http_port: int,
    label: str,
) -> None:
    outbound = manager_json(
        manager, core, root, service, "generate_client", str(node_file), LOOPBACK, "", "1",
        locked=False, environment=environment,
    )
    require(outbound.get("tag") == "proxy", "generated Xray client tag is invalid")
    require(outbound.get("protocol") == "vless", "generated Xray client protocol is invalid")
    proxy_port = smoke.free_port()
    client_file = work / (label + "-client.json")
    write_json(client_file, client_config(outbound, proxy_port))
    xray_smoke.check_xray(core, client_file, work)
    with xray_smoke.running_xray(core, client_file, work, proxy_port, label + "-client"):
        response = xray_smoke.fetch_through_socks_retry(proxy_port, http_port, "/" + label)
    require(response == HTTP_PAYLOAD, "isolated Xray client forwarding mismatch")


def make_systemctl_wrapper(directory: Path, real_systemctl: str, service: str, marker: Path) -> Path:
    wrapper = directory / "systemctl"
    text = """#!/usr/bin/env bash
if [[ ${1:-} == restart && ${2:-} == "$XRAY_SMOKE_SERVICE" && ! -e "$XRAY_SMOKE_RESTART_MARKER" ]]; then
    : >"$XRAY_SMOKE_RESTART_MARKER"
    exit 1
fi
exec "$XRAY_SMOKE_REAL_SYSTEMCTL" "$@"
"""
    wrapper.write_text(text, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def run(core: Path, manager: Path) -> None:
    real_systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    service = "xray-manager-smoke-" + uuid.uuid4().hex + ".service"
    require(service != "xray.service" and service != "sing-box.service", "production unit name refused")
    runtime_dir = Path("/run/systemd/system")
    runtime_unit = runtime_dir / service
    require(not runtime_unit.exists() and not runtime_unit.is_symlink(), "unique runtime unit already exists")
    unit_created = False
    unit_bytes = b""

    with tempfile.TemporaryDirectory(prefix="xray-manager-smoke-") as temporary:
        work = Path(temporary)
        root = work / "root"
        state = root / "var/lib/xray-manager"
        config_dir = root / "etc/xray"
        config = config_dir / "config.json"
        clients = state / "clients"
        lib_dir = root / "usr/local/lib/xray-manager"
        unit_dir = root / "etc/systemd/system"
        for directory in (clients, state / "backups", config_dir, lib_dir, unit_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        marker = lib_dir / ".managed"
        marker.write_text("xray-manager-v1\n", encoding="utf-8")
        marker.chmod(0o600)
        environment = dict(os.environ)
        environment["XR_ROOT_PREFIX"] = str(root)
        environment["CORE_BIN"] = str(core)

        try:
            with smoke.http_target(HTTP_PAYLOAD) as http_port:
                initial = {
                    "log": {"loglevel": "warning"},
                    "inbounds": [],
                    "outbounds": [{
                        "protocol": "freedom",
                        "tag": "direct",
                        "settings": {
                            "finalRules": [{
                                "action": "allow",
                                "network": "tcp",
                                "ip": ["127.0.0.1/32"],
                                "port": str(http_port),
                            }],
                        },
                        "streamSettings": {"sockopt": {"domainStrategy": "UseIPv4"}},
                    }],
                    "routing": {"rules": []},
                }
                write_json(config, initial)
                xray_smoke.check_xray(core, config, work)

                unit_bytes = unit_source(core, state, config)
                with runtime_unit.open("xb") as stream:
                    unit_created = True
                    stream.write(unit_bytes)
                runtime_unit.chmod(0o644)
                systemd(real_systemctl, "daemon-reload")
                require(
                    systemd(real_systemctl, "is-active", "--quiet", service, check=False).returncode != 0,
                    "isolated service unexpectedly started before add",
                )

                first_port = smoke.free_port()
                first_node = node("managed-node", first_port, str(uuid.uuid4()))
                first_file = work / "first-node.json"
                write_json(first_file, first_node)
                result = xray_manager_call(
                    manager, core, root, service, "add_managed_inbound", str(first_file), LOOPBACK, "",
                    locked=True, environment=environment,
                )
                require(result.returncode == 0, "add_managed_inbound failed")
                active_pid(real_systemctl, service)
                wait_listener(first_port, present=True)
                require(
                    systemd(real_systemctl, "is-enabled", "--quiet", service, check=False).returncode != 0,
                    "isolated service was unexpectedly enabled at boot",
                )
                metadata = clients / "managed-node.json"
                require(metadata.is_file() and not metadata.is_symlink(), "managed metadata was not created")
                metadata_value = read_json(metadata)
                require(metadata_value.get("inbound") == first_node, "initial metadata inbound mismatch")
                require(metadata_value.get("server") == LOOPBACK, "initial metadata server mismatch")
                probe_client(core, manager, root, service, environment, work, first_file, http_port, "first")
                print("PASS xray management add: inactive unit auto-started and forwarded HTTP", flush=True)

                second_port = smoke.free_port()
                while second_port == first_port:
                    second_port = smoke.free_port()
                second_node = node("managed-node", second_port, str(uuid.uuid4()))
                second_file = work / "second-node.json"
                write_json(second_file, second_node)
                previous_pid = active_pid(real_systemctl, service)
                result = xray_manager_call(
                    manager, core, root, service, "replace_inbound_file", "managed-node",
                    str(second_file), LOOPBACK, "", str(first_file),
                    locked=True, environment=environment,
                )
                require(result.returncode == 0, "replace_inbound_file failed")
                replaced_pid = active_pid(real_systemctl, service)
                require(replaced_pid != previous_pid, "replace did not restart isolated service")
                wait_listener(first_port, present=False)
                wait_listener(second_port, present=True)
                replaced_metadata = read_json(metadata)
                require(replaced_metadata.get("inbound") == second_node, "replacement metadata mismatch")
                require(replaced_metadata.get("server") == LOOPBACK, "replacement server metadata mismatch")
                stable_config = config.read_bytes()
                stable_metadata = metadata.read_bytes()
                stable_pid = replaced_pid
                probe_client(core, manager, root, service, environment, work, second_file, http_port, "second")
                print("PASS xray management replace: old listener closed and new listener active", flush=True)

                fail_bin = work / "fail-bin"
                fail_bin.mkdir(mode=0o700)
                restart_marker = work / "restart-once.marker"
                make_systemctl_wrapper(fail_bin, real_systemctl, service, restart_marker)
                fail_env = dict(environment)
                fail_env["PATH"] = str(fail_bin) + os.pathsep + os.environ.get("PATH", "")
                fail_env["XRAY_SMOKE_SERVICE"] = service
                fail_env["XRAY_SMOKE_RESTART_MARKER"] = str(restart_marker)
                fail_env["XRAY_SMOKE_REAL_SYSTEMCTL"] = real_systemctl
                failed_port = smoke.free_port()
                while failed_port in {first_port, second_port}:
                    failed_port = smoke.free_port()
                failed_node = node("managed-node", failed_port, str(uuid.uuid4()))
                failed_file = work / "failed-node.json"
                write_json(failed_file, failed_node)
                result = xray_manager_call(
                    manager, core, root, service, "replace_inbound_file", "managed-node",
                    str(failed_file), LOOPBACK, "", str(second_file),
                    locked=True, environment=fail_env,
                )
                require(result.returncode != 0, "injected restart failure was accepted")
                require(restart_marker.is_file(), "private restart fault was not consumed")
                require(config.read_bytes() == stable_config, "restart failure changed config")
                require(metadata.read_bytes() == stable_metadata, "restart failure changed metadata")
                rollback_pid = active_pid(real_systemctl, service)
                require(rollback_pid != stable_pid, "rollback did not restart isolated service")
                wait_listener(second_port, present=True)
                wait_listener(failed_port, present=False)
                probe_client(core, manager, root, service, environment, work, second_file, http_port, "rollback")
                print("PASS xray management rollback: restart failure restored config, metadata and service", flush=True)
        finally:
            if unit_created:
                run_command([real_systemctl, "stop", service], "isolated service cleanup", check=False, timeout=30)
                status = run_command(
                    [real_systemctl, "is-active", "--quiet", service],
                    "isolated service cleanup state",
                    check=False,
                    timeout=15,
                )
                require(status.returncode != 0, "isolated service remained active during cleanup")
                run_command([real_systemctl, "reset-failed", service], "isolated service reset", check=False, timeout=15)
                enabled_state = run_command([real_systemctl, "show", service, "--property=UnitFileState", "--value"], "isolated boot state", check=False, timeout=15).stdout.strip()
                if enabled_state in {"enabled", "enabled-runtime"}:
                    run_command([real_systemctl, "disable", service], "isolated boot cleanup", check=False, timeout=15)
                require(runtime_unit.is_file() and not runtime_unit.is_symlink(), "runtime unit was replaced or removed")
                require(runtime_unit.read_bytes() == unit_bytes, "runtime unit changed during smoke")
                runtime_unit.unlink()
                run_command([real_systemctl, "daemon-reload"], "systemd daemon-reload cleanup")
    print("PASS xray management cleanup: unique unit stopped and removed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manager", type=Path, default=Path(__file__).resolve().parents[1] / "xray-manager.sh")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("requires root on Linux with running systemd")
    required = ("bash", "jq", "ss", "systemctl", "python3")
    if not Path("/run/systemd/system").is_dir() or not all(shutil.which(name) for name in required):
        parser.error("requires systemd, bash, jq, ss, systemctl and python3")
    core, manager = args.core.expanduser().resolve(), args.manager.expanduser().resolve()
    if not core.is_file() or not os.access(core, os.X_OK) or not manager.is_file():
        parser.error("core must be executable and manager must be a file")
    os.umask(0o077)
    try:
        run(core, manager)
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-3:])
        print(f"FAIL xray management smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
