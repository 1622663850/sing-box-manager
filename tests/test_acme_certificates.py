#!/usr/bin/env python3
"""Offline tests for ACME certificate discovery and TLS wizard routing."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class AcmeCertificateTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)
        self.marker = self.core.parent / ".managed"
        self.marker.write_text("sing-box-manager-v1\n", encoding="utf-8")
        self.marker.chmod(0o600)
        self.acme_root = self.root / "root/.acme.sh"
        self.acme_root.mkdir(parents=True)
        self.root_cert = self.root / "root/cert.crt"
        self.root_key = self.root / "root/private.key"

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def generate_inbound(self, *args: str, **kwargs: object) -> dict:
        return fixture.ManagerTest.generate_inbound(self, *args, **kwargs)

    def _pair(self, directory: Path, domain: str, *, ecc: bool = False) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        cert = directory / "fullchain.cer"
        key = directory / f"{domain}.key"
        if ecc:
            result = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["openssl", "req", "-x509", "-new", "-key", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={domain}"], capture_output=True, text=True, timeout=20)
        else:
            result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={domain}"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cert, key

    def _inventory(self) -> None:
        self._pair(self.acme_root / "example.com_ecc", "example.com", ecc=True)
        self._pair(self.acme_root / "ecc.example.com_ecc", "ecc.example.com", ecc=True)
        self._pair(self.acme_root / "rsa.example.com", "rsa.example.com")
        root_pair = self._pair(self.root / "root-temp", "root.example.com")
        self.root_cert.write_bytes(root_pair[0].read_bytes())
        self.root_key.write_bytes(root_pair[1].read_bytes())
        leaf = self.acme_root / "leaf.example.com_ecc"
        leaf.mkdir()
        leaf.joinpath("fullchain.cer").write_bytes((self.acme_root / "ecc.example.com_ecc/fullchain.cer").read_bytes())
        self.acme_root.joinpath("ignored.conf").write_text("PASSWORD=not-a-certificate\n$(touch malicious-marker)\n", encoding="utf-8")
        mismatch = self.acme_root / "mismatch.example.com_ecc"
        cert, _ = self._pair(mismatch, "example.com", ecc=True)
        mismatch.joinpath("mismatch.example.com.key").write_bytes(
            (self.acme_root / "rsa.example.com/rsa.example.com.key").read_bytes()
        )

    def test_discover_finds_ecc_rsa_root_and_filters_invalid_or_conf_files(self) -> None:
        self._inventory()
        hook = self.root / "malicious-hook-marker"
        (self.acme_root / "example.com_ecc/account.conf").write_text(
            f"touch {hook}\nFAKE_PASSWORD=do-not-print\n", encoding="utf-8"
        )
        result = self.shell("discover_acme_certificates")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = json.loads(result.stdout)
        paths = {(item["certificate"], item["key"]) for item in values}
        self.assertIn((str(self.acme_root / "ecc.example.com_ecc/fullchain.cer"), str(self.acme_root / "ecc.example.com_ecc/ecc.example.com.key")), paths)
        self.assertIn((str(self.acme_root / "rsa.example.com/fullchain.cer"), str(self.acme_root / "rsa.example.com/rsa.example.com.key")), paths)
        self.assertIn((str(self.root_cert), str(self.root_key)), paths)
        self.assertNotIn("not-a-certificate", result.stdout)
        self.assertNotIn("do-not-print", result.stdout)
        self.assertFalse(hook.exists())

    def test_prompt_manual_keep_and_cancel(self) -> None:
        self._inventory()
        manual_cert = self.root / "manual cert.pem"
        manual_key = self.root / "manual key.key"
        manual_cert.write_bytes(self.root_cert.read_bytes())
        manual_key.write_bytes(self.root_key.read_bytes())
        body = r'''
        prompt() { case "$1" in tls_choice) printf -v "$1" '%s' "$TEST_CHOICE";; certificate) printf -v "$1" '%s' "$TEST_CERT";; tls_key) printf -v "$1" '%s' "$TEST_KEY";; esac; }
        certificate=''; tls_key=''; TEST_CHOICE=1; TEST_CERT="$1"; TEST_KEY="$2"; prompt_tls_certificate_files example.com '' ''; printf 'ACME:%s|%s\n' "$certificate" "$tls_key"
        certificate=''; tls_key=''; TEST_CHOICE=M; TEST_CERT="$1"; TEST_KEY="$2"; prompt_tls_certificate_files example.com '' ''; printf 'M:%s|%s\n' "$certificate" "$tls_key"
        certificate="$3"; tls_key="$4"; TEST_CHOICE=0; prompt_tls_certificate_files example.com "$certificate" "$tls_key"; printf 'K:%s|%s\n' "$certificate" "$tls_key"
'''
        result = self.shell(body, str(manual_cert), str(manual_key), str(self.root_cert), str(self.root_key))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ACME:{self.acme_root / 'example.com_ecc/fullchain.cer'}|{self.acme_root / 'example.com_ecc/example.com.key'}", result.stdout)
        self.assertIn(f"M:{manual_cert}|{manual_key}", result.stdout)
        self.assertIn(f"K:{self.root_cert}|{self.root_key}", result.stdout)
        result = self.shell('prompt() { printf -v "$1" 0; }; certificate=""; tls_key=""; prompt_tls_certificate_files example.com "" ""')
        self.assertNotEqual(result.returncode, 0)

    def test_selection_filters_wrong_sni_and_key_and_rechecks_changed_file(self) -> None:
        self._inventory()
        result = self.shell(
            'prompt() { printf -v "$1" 1; }; prompt_tls_certificate_files example.com "" ""'
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("example.com_ecc", result.stdout)
        self.assertNotIn("mismatch.example.com", result.stdout)
        self.assertNotIn("ecc.example.com", result.stdout)
        changed = self.acme_root / "example.com_ecc/fullchain.cer"
        wrong = self.acme_root / "rsa.example.com/fullchain.cer"
        result = self.shell(
            'TEST_WRONG="$1"; TEST_CERT="$2"; prompt() { if [[ "$1" == tls_choice ]]; then cp "$TEST_WRONG" "$TEST_CERT"; printf -v "$1" 1; fi; }; '
            'prompt_tls_certificate_files example.com "" ""',
            str(wrong), str(changed),
        )
        self.assertNotEqual(result.returncode, 0)

    def test_illegal_choice_is_retried_without_shell_evaluation(self) -> None:
        self._inventory()
        marker = self.root / "choice-injection-marker"
        body = r'''
count=0
prompt() { if [[ "$1" == tls_choice ]]; then count=$((count + 1)); if [[ $count == 1 ]]; then printf -v "$1" '999'; elif [[ $count == 2 ]]; then printf -v "$1" '$(touch "$INJECT_MARKER")'; else printf -v "$1" 1; fi; fi; }
prompt_tls_certificate_files example.com '' ''
'''
        self.env["INJECT_MARKER"] = str(marker)
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("example.com_ecc", result.stdout)
        self.assertFalse(marker.exists())

    def test_expired_certificate_is_filtered_by_real_openssl_validation(self) -> None:
        self._inventory()
        def make_ca_signed(directory: Path, prefix: str, start: str, end: str) -> None:
            directory.mkdir(parents=True)
            key = directory / f"{prefix}.key"
            csr = directory / "request.csr"
            cert = directory / "fullchain.cer"
            ca_key = directory / "ca.key"
            ca_cert = directory / "ca.crt"
            for args in (
                ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", "/CN=example.com"],
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "2", "-subj", "/CN=test-ca"],
            ):
                result = subprocess.run(args, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
            (directory / "index.txt").write_text("", encoding="utf-8")
            (directory / "index.txt.attr").write_text("unique_subject = no\n", encoding="utf-8")
            (directory / "serial").write_text("1000\n", encoding="utf-8")
            (directory / "newcerts").mkdir()
            config = directory / "openssl.cnf"
            config.write_text("\n".join([
                "[ca]", "default_ca=default", "[default]",
                f"database={directory / 'index.txt'}", f"new_certs_dir={directory / 'newcerts'}",
                f"certificate={ca_cert}", f"private_key={ca_key}", f"serial={directory / 'serial'}",
                "default_md=sha256", "policy=policy", "[policy]", "commonName=supplied", "",
            ]), encoding="utf-8")
            result = subprocess.run(
                ["openssl", "ca", "-batch", "-config", str(config), "-in", str(csr), "-out", str(cert),
                 "-startdate", start, "-enddate", end],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        make_ca_signed(self.acme_root / "expired.example.com_ecc", "expired.example.com", "20000101000000Z", "20000102000000Z")
        make_ca_signed(self.acme_root / "future.example.com_ecc", "future.example.com", "20990101000000Z", "20990102000000Z")
        result = self.shell('prompt() { printf -v "$1" 1; }; prompt_tls_certificate_files example.com "" ""')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("expired.example.com", result.stdout)
        self.assertNotIn("future.example.com", result.stdout)

    def test_existing_hy2_edit_persists_selected_certificate_paths_and_jsonc(self) -> None:
        self._inventory()
        node = {
            "type": "hysteria2", "tag": "hy2-edit", "listen": "127.0.0.1", "listen_port": 18443,
            "users": [{"password": "edit-password"}],
            "tls": {"enabled": True, "server_name": "example.com", "certificate_path": str(self.root_cert), "key_path": str(self.root_key)},
        }
        raw = '{\n  // preserve this JSONC comment\n  "log":{"level":"info"},\n  "inbounds":[' + json.dumps(node) + '],\n  "outbounds":[{"type":"direct","tag":"direct"}]\n}\n'
        self.config.write_text(raw, encoding="utf-8")
        clients = self.state_dir / "clients"
        clients.mkdir(parents=True, exist_ok=True)
        fixture.dump(clients / "hy2-edit.json", {"server":"example.com", "public_key":"", "inbound":node})
        body = r'''
prompt() { if [[ "$1" == tls_choice ]]; then printf -v "$1" 1; else printf -v "$1" '%s' "${3:-}"; fi; }
prompt_secret() { printf -v "$1" '%s' "${3:-}"; }
confirm() { return 0; }
edit_wizard_locked hy2-edit
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = json.loads(self.shell('config_jq .').stdout)
        inbound = updated["inbounds"][0]
        self.assertEqual(inbound["tls"]["certificate_path"], str(self.acme_root / "example.com_ecc/fullchain.cer"))
        self.assertEqual(inbound["tls"]["key_path"], str(self.acme_root / "example.com_ecc/example.com.key"))
        self.assertIn("preserve this JSONC comment", self.config.read_text(encoding="utf-8"))
        metadata = json.loads((clients / "hy2-edit.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["inbound"]["tls"]["certificate_path"], str(self.acme_root / "example.com_ecc/fullchain.cer"))

    def test_wildcard_certificate_matches_specific_sni(self) -> None:
        directory = self.acme_root / "wildcard.example.com_ecc"
        cert, key = self._pair(directory, "wildcard.example.com", ecc=True)
        result = subprocess.run(
            ["openssl", "req", "-x509", "-new", "-key", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=*.example.com"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = 'prompt() { printf -v "$1" 1; }; prompt_tls_certificate_files foo.example.com "" ""'
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("wildcard.example.com_ecc", result.stdout)

    def test_missing_acme_tree_falls_back_to_manual_paths_with_spaces(self) -> None:
        prefix = self.root / "prefix with spaces"
        manual_cert = prefix / "manual cert.pem"
        manual_key = prefix / "manual key.key"
        source_cert, source_key = self._pair(self.root / "manual-material", "root.example.com")
        manual_cert.parent.mkdir(parents=True)
        manual_cert.write_bytes(source_cert.read_bytes())
        manual_key.write_bytes(source_key.read_bytes())
        body = r'''
SB_ROOT_PREFIX="$1"
TEST_CERT="$2"; TEST_KEY="$3"
prompt() { case "$1" in certificate) printf -v "$1" '%s' "$TEST_CERT";; tls_key) printf -v "$1" '%s' "$TEST_KEY";; esac; }
certificate=''; tls_key=''; prompt_tls_certificate_files root.example.com '' ''; printf '%s|%s\n' "$certificate" "$tls_key"
'''
        result = self.shell(body, str(prefix), str(manual_cert), str(manual_key))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{manual_cert}|{manual_key}", result.stdout)

    def test_tls_identity_validation_does_not_trust_old_openssl_checkhost_exit_code(self) -> None:
        real_openssl = shutil.which("openssl")
        if not real_openssl:
            self.skipTest("openssl is required")
        cert, key = self._pair(self.root / "identity-cert", "good.example.com")
        wrapper = self.bin / "openssl"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ ${1:-} == x509 && \" $* \" == *\" -checkhost \"* || \" $* \" == *\" -checkip \"* ]]; then\n"
            "  \"$REAL_OPENSSL\" \"$@\"\n"
            "  exit 0\n"
            "fi\n"
            "exec \"$REAL_OPENSSL\" \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.env["REAL_OPENSSL"] = real_openssl
        for identity in ("wrong.example.com", "127.0.0.1"):
            with self.subTest(identity=identity):
                result = self.shell('validate_tls_files "$1" "$2" "$3"', identity, str(cert), str(key))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.shell(
            'validate_tls_files "$1" "$2" "$3"',
            "good.example.com", str(cert), str(key),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_add_routes_all_nine_regular_tls_templates_to_helper(self) -> None:
        log = self.root / "tls-route.log"
        body = r'''
require_core() { return 0; }
prompt() { case "$1" in choice) printf -v "$1" '%s' "$TEST_CHOICE";; tag) printf -v "$1" '%s' "tls-$TEST_CHOICE";; port) printf -v "$1" '%s' "$((44000 + TEST_CHOICE))";; obfs_choice) printf -v "$1" 1;; extra) printf -v "$1" TunService;; tls_name|server) printf -v "$1" example.com;; esac; }
prompt_tls_certificate_files() { printf 'H:%s\n' "$1" >>"$TLS_ROUTE_LOG"; printf -v certificate '%s' "$TLS_CERT"; printf -v tls_key '%s' "$TLS_KEY"; }
prompt_quic_bbr() { printf -v "$1" 2; }
prepare_quic_bbr_wizard() { : >"$4"; cat -- "$1"; }
generate_inbound() { printf '{"type":"vless","tag":"%s","listen_port":443,"users":[{"uuid":"b7efdc96-87aa-4910-81b4-374b2eecc737"}]}' "$2"; }
generate_client() { return 0; }; add_managed_inbound() { return 0; }; show_client() { return 0; }
for TEST_CHOICE in 7 8 9 10 12 14 15 17 18; do add_wizard_locked || exit; done
'''
        self.env["TLS_ROUTE_LOG"] = str(log)
        self.env["TLS_CERT"] = str(self.root_cert)
        self.env["TLS_KEY"] = str(self.root_key)
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 9)

    def test_edit_routes_all_nine_regular_tls_templates_to_selector(self) -> None:
        self._inventory()
        cert = self.acme_root / "example.com_ecc/fullchain.cer"
        key = self.acme_root / "example.com_ecc/example.com.key"
        protocols = ["hysteria2", "tuic", "anytls", "vless-grpc-tls", "vless-ws-tls", "vless-tcp-tls", "vmess-tcp-tls", "vmess-ws-tls", "vmess-grpc-tls"]
        nodes = [fixture.ManagerTest.edit_protocol_inbound(self, protocol, f"edit-{index}", 25000 + index, cert, key) for index, protocol in enumerate(protocols)]
        config = dict(self.original)
        config["inbounds"] = nodes
        fixture.dump(self.config, config)
        clients = self.state_dir / "clients"
        clients.mkdir(parents=True, exist_ok=True)
        for node in nodes:
            fixture.dump(clients / f"{node['tag']}.json", {"server":"example.com", "public_key":"", "inbound":node})
        route_log = self.root / "tls-edit-route.log"
        body = r'''
require_core() { return 0; }
prompt() { if [[ "$1" == tls_choice ]]; then printf -v "$1" 1; else printf -v "$1" '%s' "${3:-}"; fi; }
prompt_secret() { printf -v "$1" '%s' "${3:-}"; }
prompt_quic_bbr() { printf -v "$1" 2; }
prompt_tls_certificate_files() { printf 'H:%s\n' "$1" >>"$TLS_EDIT_LOG"; printf -v certificate '%s' "$TLS_CERT"; printf -v tls_key '%s' "$TLS_KEY"; }
replace_inbound_file() { printf 'R:%s\n' "$1" >>"$TLS_EDIT_LOG"; return 0; }
confirm() { return 0; }
for tag in edit-0 edit-1 edit-2 edit-3 edit-4 edit-5 edit-6 edit-7 edit-8; do edit_wizard_locked "$tag" || exit; done
'''
        self.env["TLS_EDIT_LOG"] = str(route_log)
        self.env["TLS_CERT"] = str(cert)
        self.env["TLS_KEY"] = str(key)
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = route_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("H:") for line in lines), 9)
        self.assertEqual(sum(line.startswith("R:") for line in lines), 9)

    def test_reality_edit_does_not_call_helper_or_rewrite_jsonc(self) -> None:
        node = {"type":"vless","tag":"reality","listen":"127.0.0.1","listen_port":18443,"users":[{"uuid":"b7efdc96-87aa-4910-81b4-374b2eecc737","flow":"xtls-rprx-vision"}],"tls":{"enabled":True,"server_name":"example.com","reality":{"enabled":True,"handshake":{"server":"example.com","server_port":443},"private_key":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","short_id":["0123456789abcdef"]}}}
        raw = '{\n  // keep REALITY\n  "log":{"level":"info"},\n  "inbounds":[' + json.dumps(node) + '],\n  "outbounds":[{"type":"direct","tag":"direct"}]\n}\n'
        self.config.write_text(raw, encoding="utf-8")
        clients = self.state_dir / "clients"
        clients.mkdir(parents=True, exist_ok=True)
        fixture.dump(clients / "reality.json", {"server":"example.com","public_key":"","inbound":node})
        result = self.shell('require_core(){ return 0; }; prompt(){ printf -v "$1" "%s" "${3:-}"; }; prompt_secret(){ printf -v "$1" "%s" "${3:-}"; }; prompt_tls_certificate_files(){ printf CALLED; return 99; }; generate_client(){ return 0; }; replace_inbound_file(){ return 0; }; confirm(){ return 0; }; edit_wizard_locked reality')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("CALLED", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
