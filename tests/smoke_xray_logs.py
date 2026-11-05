#!/usr/bin/env python3
"""Loopback systemd/journald smoke test for the Xray live log menu."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import time
import traceback
import uuid

# Optional fixture scope avoids unrelated archives on busy test hosts.
# CI leaves it unset and exercises journalctl's normal source selection.
JOURNAL_WRAPPER = r'''journalctl() {
    if [[ -n ${XR_TEST_JOURNAL_FILE:-} ]]; then
        command journalctl --file "$XR_TEST_JOURNAL_FILE" "$@"
    else
        command journalctl "$@"
    fi
}; '''

def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(args: list[str], *, check: bool = True, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError("system operation failed")
    return result


def wait_for(fd: int, data: bytearray, token: bytes, *, offset: int = 0,
             timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while token not in data[offset:]:
        if time.monotonic() >= deadline:
            raise RuntimeError("live log marker did not arrive")
        if select.select([fd], [], [], 0.2)[0]:
            try:
                chunk = os.read(fd, 65536)
            except OSError as error:
                raise RuntimeError("PTY closed before expected log marker") from error
            if not chunk:
                raise RuntimeError("PTY reached EOF before expected log marker")
            data.extend(chunk)


def journalctl_pids(service: str) -> set[int]:
    result = run(["ps", "-eo", "pid=,args="], check=True)
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and "journalctl" in fields[1] and service in fields[1]:
            pids.add(int(fields[0]))
    return pids


def run_menu(manager: Path, service: str, gate: Path, *, live: bool) -> bytes:
    """Drive the real installed logs_menu through a controlling PTY."""
    pid, master = pty.fork()
    if pid == 0:
        command = 'source "$1"; SERVICE="$2"; ' + JOURNAL_WRAPPER + 'logs_menu'
        child_env = dict(os.environ, XR_LOG_GATE=str(gate))
        os.execve("/bin/bash", ["bash", "-c", command, "xray-log-smoke", str(manager), service], child_env)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 240, 0, 0))
    output = bytearray()
    try:
        log_prompt = "请选择日志操作 [0]: ".encode()
        wait_for(master, output, log_prompt)
        if live:
            follow_offset = len(output)
            os.write(master, b"2\n")
            wait_for(master, output, b"XRAY_LOG_INITIAL", offset=follow_offset)
            gate.touch()
            live_offset = len(output)
            wait_for(master, output, b"XRAY_LOG_LIVE", offset=live_offset)
            follower_pids = journalctl_pids(service)
            require(follower_pids, "journalctl follower process was not observed")
            returned_offset = len(output)
            os.write(master, b"\x03")
            wait_for(master, output, log_prompt, offset=returned_offset)
            os.write(master, b"0\n")
        else:
            os.write(master, b"1\n")
            wait_for(master, output, b"XRAY_LOG_INITIAL")
            wait_for(master, output, log_prompt)
            os.write(master, b"0\n")
        deadline = time.monotonic() + 10
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                require(os.WIFEXITED(status) and os.waitstatus_to_exitcode(status) == 0,
                        "log menu did not exit cleanly")
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("log menu did not exit")
            if select.select([master], [], [], 0.2)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    pass  # EIO is the normal PTY EOF after the final 0 exits the menu.
        if live:
            deadline = time.monotonic() + 5
            while not journalctl_pids(service).isdisjoint(follower_pids):
                require(time.monotonic() < deadline,
                        "journalctl follower process remained after Ctrl-C")
                time.sleep(0.1)
        return bytes(output)
    finally:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(master)


def run_test(manager: Path, core: Path) -> None:
    del core  # Kept as a stable CLI argument alongside the other Xray smokes.
    require(os.geteuid() == 0 and Path("/run/systemd/system").is_dir(),
            "requires root with running systemd")
    require(manager.is_file() and not manager.is_symlink(), "manager is not a regular file")
    systemctl = "/usr/bin/systemctl"
    service = "xray-log-smoke-" + uuid.uuid4().hex + ".service"
    unit = Path("/run/systemd/system") / service
    require(not unit.exists() and not unit.is_symlink(), "temporary service already exists")
    script = Path("/tmp") / ("xray-log-smoke-" + uuid.uuid4().hex + ".sh")
    gate = Path("/tmp") / ("xray-log-gate-" + uuid.uuid4().hex)
    script_bytes = b"#!/bin/sh\nprintf 'XRAY_LOG_INITIAL\\n'\nwhile [ ! -e \"$XR_LOG_GATE\" ]; do sleep 0.1; done\nprintf 'XRAY_LOG_LIVE\\n'\nsleep 60\n"
    with script.open("xb") as stream:
        stream.write(script_bytes)
    script.chmod(0o700)
    unit_bytes = f"""[Unit]
Description=Xray log menu isolated smoke

[Service]
Type=simple
Environment=XR_LOG_GATE={gate}
ExecStart={script}
Restart=no
StandardOutput=journal
StandardError=journal
""".encode()
    created = False
    try:
        with unit.open("xb") as stream:
            stream.write(unit_bytes)
        unit.chmod(0o644)
        created = True
        run([systemctl, "daemon-reload"])
        run([systemctl, "start", service])
        result = None
        for _ in range(50):
            result = run(["bash", "-c", 'source "$1"; SERVICE="$2"; ' + JOURNAL_WRAPPER + 'show_logs',
                          "xray-log-smoke", str(manager), service])
            if "XRAY_LOG_INITIAL" in result.stdout:
                break
            time.sleep(0.2)
        require("XRAY_LOG_INITIAL" in result.stdout,
                "finite log view missed the initial journal entry")
        live = run_menu(manager, service, gate, live=True)
        require(b"XRAY_LOG_LIVE" in live, "live menu did not receive the new journal entry")
        print("PASS finite and live log views", flush=True)
    finally:
        if created:
            run([systemctl, "stop", service], check=False)
            run([systemctl, "reset-failed", service], check=False)
            active = run([systemctl, "is-active", "--quiet", service], check=False)
            require(active.returncode != 0, "temporary journal unit remained active")
            require(not unit.is_symlink(), "temporary journal unit became a symlink")
            require(unit.is_file() and unit.read_bytes() == unit_bytes, "temporary unit changed")
            unit.unlink()
            run([systemctl, "daemon-reload"])
        script.unlink(missing_ok=True)
        gate.unlink(missing_ok=True)
        require(not unit.exists(), "temporary unit cleanup failed")
        require(not script.exists(), "temporary log script cleanup failed")
        require(not gate.exists(), "temporary log gate cleanup failed")
    print("PASS temporary journal unit cleanup", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=False, default=Path("/usr/local/lib/xray-manager/xray"))
    args = parser.parse_args()
    try:
        import termios
        run_test(args.manager.resolve(), args.core.resolve())
    except Exception as error:
        frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                  if Path(frame.filename).resolve() == Path(__file__).resolve()]
        locations = ",".join(Path(frame.filename).name + ":" + str(frame.lineno)
                               for frame in frames[-3:])
        print(f"FAIL log smoke: {type(error).__name__} at {locations}", file=sys.stderr)
        raise SystemExit(1)
