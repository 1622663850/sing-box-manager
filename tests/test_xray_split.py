#!/usr/bin/env python3
"""Regression coverage for XHTTP upload/download split client exports.

The embedded checks are runnable on Windows because they execute the Python
fragments from ``xray-manager.sh`` directly.  The metadata tests use the
existing Linux fixture and skip when Bash/jq are unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

import test_xray_manager as fixture


UUID = fixture.UUID
PUBLIC = fixture.PUBLIC_KEY
DOWNLOAD_SECRET = "split-download-secret-must-not-echo"


def embedded(marker: str) -> str:
    source = fixture.SCRIPT.read_text(encoding="utf-8")
    start_marker = "<<'" + marker + "'\n"
    start = source.index(start_marker) + len(start_marker)
    end = source.index("\n" + marker, start)
    return source[start:end]


def run_embedded(marker: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", embedded(marker), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def upload_outbound(*, mode: str = "auto", security: str = "tls", path: str = "/xhttp") -> dict:
    stream: dict = {
        "method": "xhttp",
        "security": security,
        "xhttpSettings": {"path": path, "mode": mode},
    }
    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": "upload.example",
            "alpn": ["h2"],
            "allowInsecure": False,
        }
    else:
        stream["realitySettings"] = {
            "serverName": "upload-cover.example",
            "fingerprint": "chrome",
            "password": PUBLIC,
            "shortId": "01234567",
        }
    return {
        "tag": "split-client",
        "protocol": "vless",
        "settings": {
            "address": "upload.example",
            "port": 443,
            "id": UUID,
            "encryption": "none",
        },
        "streamSettings": stream,
    }


def download_settings(
    *, security: str = "tls", path: str = "/xhttp", alpn: str = "h2",
    fingerprint: str = "chrome", allow_insecure: bool = False,
) -> dict:
    value: dict = {
        "address": "download.example",
        "port": 443,
        "network": "xhttp",
        "security": security,
        "xhttpSettings": {"path": path, "mode": "auto"},
    }
    if security == "tls":
        value["tlsSettings"] = {
            "serverName": "download.example",
            "alpn": [alpn],
            "fingerprint": fingerprint,
            "allowInsecure": allow_insecure,
        }
    else:
        value["realitySettings"] = {
            "serverName": "download-cover.example",
            "fingerprint": "chrome",
            "password": PUBLIC,
            "shortId": "89abcdef",
        }
    return value


class XhttpEmbeddedTest(unittest.TestCase):
    """Pure link/config checks that do not require Linux or systemd."""

    def setUp(self) -> None:
        if "PY_XHTTP_DOWNLOAD" not in fixture.SCRIPT.read_text(encoding="utf-8"):
            self.skipTest("xhttp download helper is not available yet")

    def call_download(self, action: str, *paths: Path) -> subprocess.CompletedProcess[str]:
        return run_embedded("PY_XHTTP_DOWNLOAD", action, *(str(path) for path in paths))

    def attach(self, outbound: dict, download: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="xray-split-attach-") as directory:
            root = Path(directory)
            out_path = write_json(root / "outbound.json", outbound)
            download_path = write_json(root / "download.json", download)
            result = self.call_download("attach", out_path, download_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

    def test_download_normalizes_and_attaches_tls_split(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-download-") as directory:
            root = Path(directory)
            inbound_path = write_json(root / "inbound.json", upload_outbound())
            download_path = write_json(root / "download.json", download_settings())
            normalized = self.call_download("download", download_path, inbound_path)
            self.assertEqual(normalized.returncode, 0, normalized.stdout + normalized.stderr)
            value = json.loads(normalized.stdout)
            self.assertEqual(value["network"], "xhttp")
            self.assertEqual(value["security"], "tls")
            self.assertEqual(value["xhttpSettings"]["path"], "/xhttp")
            self.assertFalse(value["tlsSettings"]["allowInsecure"])

        attached = self.attach(upload_outbound(mode="auto"), download_settings())
        xhttp = attached["streamSettings"]["xhttpSettings"]
        self.assertEqual(xhttp["mode"], "stream-up")
        self.assertEqual(xhttp["extra"]["downloadSettings"]["network"], "xhttp")
        self.assertEqual(xhttp["extra"]["downloadSettings"]["xhttpSettings"]["path"], "/xhttp")

    def test_packet_up_is_preserved_and_reality_download_is_supported(self) -> None:
        packet = self.attach(upload_outbound(mode="packet-up"), download_settings(security="reality"))
        self.assertEqual(packet["streamSettings"]["xhttpSettings"]["mode"], "packet-up")
        download = packet["streamSettings"]["xhttpSettings"]["extra"]["downloadSettings"]
        self.assertEqual(download["security"], "reality")
        self.assertEqual(download["realitySettings"]["fingerprint"], "chrome")

    def test_http11_download_requires_unsafe_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-http11-fingerprint-") as directory:
            root = Path(directory)
            inbound_path = write_json(root / "inbound.json", upload_outbound())
            chrome_path = write_json(root / "chrome.json", download_settings(alpn="http/1.1", fingerprint="chrome"))
            rejected = self.call_download("download", chrome_path, inbound_path)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn(DOWNLOAD_SECRET, rejected.stdout + rejected.stderr)

            unsafe_path = write_json(root / "unsafe.json", download_settings(alpn="http/1.1", fingerprint="unsafe"))
            normalized = self.call_download("download", unsafe_path, inbound_path)
            self.assertEqual(normalized.returncode, 0, normalized.stdout + normalized.stderr)
            self.assertEqual(json.loads(normalized.stdout)["tlsSettings"]["fingerprint"], "unsafe")

            attached = self.attach(upload_outbound(), download_settings(alpn="http/1.1", fingerprint="unsafe"))
            attached_path = write_json(root / "attached.json", attached)
            checked = self.call_download("check", attached_path)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

            insecure_path = write_json(
                root / "insecure.json",
                download_settings(alpn="http/1.1", fingerprint="unsafe", allow_insecure=True),
            )
            insecure = self.call_download("attach", inbound_path, insecure_path)
            self.assertNotEqual(insecure.returncode, 0)
            self.assertNotIn(DOWNLOAD_SECRET, insecure.stdout + insecure.stderr)

    def test_invalid_downloads_are_rejected_without_secret_echo(self) -> None:
        cases = []
        bad = download_settings()
        bad["network"] = "grpc"
        cases.append(bad)
        bad = download_settings()
        bad["port"] = 0
        cases.append(bad)
        bad = download_settings()
        bad["tlsSettings"]["allowInsecure"] = True
        cases.append(bad)
        bad = download_settings()
        bad["xhttpSettings"]["path"] = "/different"
        cases.append(bad)
        bad = download_settings()
        bad["extra"] = {"downloadSettings": download_settings()}
        cases.append(bad)
        bad = download_settings()
        bad["tlsSettings"]["password"] = DOWNLOAD_SECRET
        cases.append(bad)

        with tempfile.TemporaryDirectory(prefix="xray-split-invalid-") as directory:
            root = Path(directory)
            inbound_path = write_json(root / "inbound.json", upload_outbound())
            for index, value in enumerate(cases):
                with self.subTest(case=index):
                    path = write_json(root / (f"bad-{index}.json"), value)
                    result = self.call_download("download", path, inbound_path)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(DOWNLOAD_SECRET, result.stdout + result.stderr)

    def test_stream_one_is_rejected_for_split(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-stream-one-") as directory:
            root = Path(directory)
            inbound_path = write_json(root / "inbound.json", upload_outbound(mode="stream-one"))
            download_path = write_json(root / "download.json", download_settings())
            result = self.call_download("download", download_path, inbound_path)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(DOWNLOAD_SECRET, result.stdout + result.stderr)

    def test_extra_stream_one_overrides_auto_and_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-extra-mode-") as directory:
            root = Path(directory)
            outbound_path = write_json(root / "outbound.json", upload_outbound(mode="auto"))
            download_path = write_json(root / "download.json", download_settings())
            outbound = json.loads(outbound_path.read_text(encoding="utf-8"))
            outbound["streamSettings"]["xhttpSettings"]["extra"] = {"mode": "stream-one"}
            write_json(outbound_path, outbound)
            result = self.call_download("attach", outbound_path, download_path)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(DOWNLOAD_SECRET, result.stdout + result.stderr)

    def test_recursive_private_fields_are_rejected_by_check_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-sensitive-") as directory:
            root = Path(directory)
            attached = self.attach(upload_outbound(), download_settings())
            locations = (
                ("xhttpSettings", "extra", "privateKey"),
                ("tlsSettings", "keyFile"),
                ("realitySettings", "mldsa65Seed"),
            )
            for location in locations:
                with self.subTest(location=location):
                    value = json.loads(json.dumps(attached))
                    download = value["streamSettings"]["xhttpSettings"]["extra"]["downloadSettings"]
                    target = download
                    for key in location[:-1]:
                        target = target.setdefault(key, {})
                    target[location[-1]] = DOWNLOAD_SECRET
                    path = write_json(root / ("bad-" + "-".join(location) + ".json"), value)
                    checked = self.call_download("check", path)
                    self.assertNotEqual(checked.returncode, 0)
                    self.assertNotIn(DOWNLOAD_SECRET, checked.stdout + checked.stderr)

                    if shutil.which("bash"):
                        wrapped = subprocess.run(
                            ["bash", "-c", 'source "$1"; client_link "$2" split-test',
                             "xray-split-test", str(fixture.SCRIPT), str(path)],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        self.assertNotEqual(wrapped.returncode, 0)
                        self.assertNotIn(DOWNLOAD_SECRET, wrapped.stdout + wrapped.stderr)

    def test_check_rejects_duplicate_top_level_and_extra_download_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-duplicate-") as directory:
            root = Path(directory)
            attached = self.attach(upload_outbound(), download_settings())
            xhttp = attached["streamSettings"]["xhttpSettings"]
            xhttp["downloadSettings"] = json.loads(json.dumps(xhttp["extra"]["downloadSettings"]))
            path = write_json(root / "duplicate.json", attached)
            result = self.call_download("check", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(DOWNLOAD_SECRET, result.stdout + result.stderr)

    def test_share_link_extra_round_trip_preserves_download_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-link-") as directory:
            root = Path(directory)
            outbound_path = write_json(root / "outbound.json", self.attach(upload_outbound(), download_settings()))
            link = run_embedded("PY_XRAY_LINK", outbound_path, "split-link")
            self.assertEqual(link.returncode, 0, link.stdout + link.stderr)
            parsed = urlsplit(link.stdout.strip())
            query = parse_qs(parsed.query)
            self.assertIn("extra", query)
            self.assertIn("downloadSettings", unquote(query["extra"][0]))

            link_path = root / "links.txt"
            link_path.write_text(link.stdout, encoding="utf-8")
            decoded = run_embedded("PY_OUTBOUND_LINKS", link_path)
            self.assertEqual(decoded.returncode, 0, decoded.stdout + decoded.stderr)
            nodes_path = write_json(root / "nodes.json", json.loads(decoded.stdout))
            converted = run_embedded("PY_XR_LINK_CONVERT", nodes_path)
            self.assertEqual(converted.returncode, 0, converted.stdout + converted.stderr)
            native = json.loads(converted.stdout)[0]
            self.assertEqual(
                native["streamSettings"]["xhttpSettings"]["extra"]["downloadSettings"]["network"],
                "xhttp",
            )

    def test_check_accepts_single_and_array_outbound_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xray-split-check-") as directory:
            root = Path(directory)
            attached = self.attach(upload_outbound(), download_settings())
            one = write_json(root / "one.json", attached)
            many = write_json(root / "many.json", [attached, attached])
            for path in (one, many):
                with self.subTest(path=path.name):
                    result = self.call_download("check", path)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class XhttpMetadataTest(fixture.XrayFixture):
    """Isolated metadata lifecycle checks; never touch a live service."""

    def setUp(self) -> None:
        super().setUp()
        if "set_xhttp_split_locked" not in fixture.SCRIPT.read_text(encoding="utf-8"):
            self.skipTest("xhttp split metadata API is not available yet")

    def split_file(self, node: dict | None = None) -> Path:
        node = node or upload_outbound()
        return write_json(self.root / "download.json", download_settings(path=node["streamSettings"]["xhttpSettings"]["path"]))

    def test_metadata_save_close_does_not_change_config_or_restart(self) -> None:
        node = upload_outbound()
        self.write_config([node])
        self.write_metadata(node, server="example.com")
        before_config = self.config.read_bytes()
        before_calls = self.calls()
        result = self.shell(
            'set_xhttp_split_locked "$1" "$2"', node["tag"], str(self.split_file(node)), check=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((self.clients_dir / f"{node['tag']}.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["xhttp_split"]["format"], "xray-manager-xhttp-split-v1")
        self.assertEqual(metadata["xhttp_split"]["download"]["network"], "xhttp")
        self.assertEqual(self.config.read_bytes(), before_config)
        calls = self.calls()[len(before_calls):]
        self.assertFalse(any(call and call[0] in {"restart", "start", "stop"} for call in calls))

        closed = self.shell('set_xhttp_split_locked "$1" -', node["tag"], check=True)
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        metadata = json.loads((self.clients_dir / f"{node['tag']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("xhttp_split", metadata)
        self.assertEqual(self.config.read_bytes(), before_config)

    def test_invalid_metadata_does_not_persist_or_echo_secret(self) -> None:
        node = upload_outbound()
        self.write_config([node])
        self.write_metadata(node, server="example.com")
        invalid = download_settings(path="/different")
        invalid["tlsSettings"]["password"] = DOWNLOAD_SECRET
        path = write_json(self.root / "invalid-download.json", invalid)
        result = self.shell('set_xhttp_split_locked "$1" "$2"', node["tag"], str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(DOWNLOAD_SECRET, result.stdout + result.stderr)
        metadata = json.loads((self.clients_dir / f"{node['tag']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("xhttp_split", metadata)

    def test_path_changed_after_save_is_not_exported(self) -> None:
        node = upload_outbound()
        self.write_config([node])
        self.write_metadata(node, server="example.com")
        self.shell('set_xhttp_split_locked "$1" "$2"', node["tag"], str(self.split_file(node)), check=True)
        changed = json.loads(self.config.read_text(encoding="utf-8"))
        changed["inbounds"][0]["streamSettings"]["xhttpSettings"]["path"] = "/changed"
        write_json(self.config, changed)
        exported = self.shell('show_client "$1" "$2" full 1', node["tag"], "example.com")
        self.assertNotIn("downloadSettings", exported.stdout)


if __name__ == "__main__":
    unittest.main()
