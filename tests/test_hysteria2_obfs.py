#!/usr/bin/env python3
"""Offline coverage for Hysteria 2 Salamander obfuscation."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, urlsplit

import test_manager as fixture


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "bash and jq are required")
class Hysteria2ObfsTest(unittest.TestCase):
    """Reuse the isolated manager fixture without inheriting its full test suite."""

    def setUp(self) -> None:
        fixture.ManagerTest.setUp(self)

    def shell(self, body: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return fixture.ManagerTest.shell(self, body, *args, check=check)

    def tls_material(self, common_name: str = "example.com") -> tuple[Path, Path]:
        return fixture.ManagerTest.tls_material(self, common_name)

    def generate_inbound(
        self,
        password: str = "hy-password",
        *,
        obfs_password: str = "",
        tag: str = "hy2-obfs",
        port: int = 18443,
    ) -> dict:
        certificate, private_key = self.tls_material()
        return fixture.ManagerTest.generate_inbound(
            self,
            "hysteria2",
            password,
            tls_server_name="example.com",
            certificate=certificate,
            key=private_key,
            obfs_password=obfs_password,
        ) | {"tag": tag, "listen_port": port}

    def _write_inbound(self, inbound: dict, *, comment: bool = False) -> str:
        value = dict(self.original)
        value["inbounds"] = [inbound]
        if not comment:
            fixture.dump(self.config, value)
            return self.config.read_text(encoding="utf-8")
        raw = (
            '{\n'
            '  // keep Hysteria 2 comments\n'
            '  "log": {"level": "info"},\n'
            '  "inbounds": [' + json.dumps(inbound, ensure_ascii=False) + '],\n'
            '  "outbounds": [{"type": "direct", "tag": "direct"}]\n'
            '}\n'
        )
        self.config.write_text(raw, encoding="utf-8")
        return raw

    def _write_metadata(self, inbound: dict) -> Path:
        path = self.state_dir / "clients" / (inbound["tag"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture.dump(path, {"server": "example.com", "public_key": "", "inbound": inbound})
        return path

    def test_generate_hysteria2_obfs_and_minimum_utf8_bytes(self) -> None:
        plain = self.generate_inbound()
        self.assertNotIn("obfs", plain)

        for value in ("sala", "密a"):
            with self.subTest(value=value):
                inbound = self.generate_inbound(obfs_password=value, tag="hy2-" + value)
                self.assertEqual(inbound["obfs"], {"type": "salamander", "password": value})

        certificate, private_key = self.tls_material()
        short = self.shell(
            'generate_inbound "$@"',
            "hysteria2", "short-obfs", "18443", "hy-password", "", "", "", "example.com",
            str(certificate), str(private_key), "", "abc",
        )
        self.assertNotEqual(short.returncode, 0, short.stdout + short.stderr)

        non_hy2 = self.shell(
            'generate_inbound "$@"',
            "vless-tcp", "wrong-obfs", "18443",
            "b7efdc96-87aa-4910-81b4-374b2eecc737", "", "", "", "", "", "", "", "salamander",
        )
        self.assertNotEqual(non_hy2.returncode, 0, non_hy2.stdout + non_hy2.stderr)

    def test_hysteria2_obfs_export_and_link_preserve_special_utf8_password(self) -> None:
        certificate, private_key = self.tls_material()
        plain = self.generate_inbound(tag="plain-hy2")
        obfs_password = "密& pwd"
        tail_password = "tail-obfs\n"
        obfuscated = self.generate_inbound(obfs_password=obfs_password, tag="obfs-hy2")
        tailed = self.generate_inbound(obfs_password=tail_password, tag="obfs-hy2-tail")
        for inbound, expected_obfs in (
            (plain, None),
            (obfuscated, obfs_password),
            (tailed, tail_password),
        ):
            with self.subTest(obfs=expected_obfs):
                source = self.root / (inbound["tag"] + ".json")
                fixture.dump(source, inbound)
                result = self.shell('generate_client "$1" "example.com"', str(source), check=True)
                outbound = json.loads(result.stdout)
                if expected_obfs is None:
                    self.assertNotIn("obfs", outbound)
                else:
                    self.assertEqual(outbound["obfs"], {"type": "salamander", "password": expected_obfs})
                outbound_path = self.root / (inbound["tag"] + "-outbound.json")
                outbound_path.write_text(result.stdout, encoding="utf-8")
                link = self.shell('client_link "$1" "hy2"', str(outbound_path), check=True).stdout.strip()
                query = parse_qs(urlsplit(link).query)
                if expected_obfs is None:
                    self.assertNotIn("obfs", query)
                    self.assertNotIn("obfs-password", query)
                else:
                    self.assertEqual(query["obfs"], ["salamander"])
                    self.assertEqual(query["obfs-password"], [expected_obfs])
                    self.assertNotIn(expected_obfs, link)
        self.assertTrue(certificate.is_file())
        self.assertTrue(private_key.is_file())

    def test_unknown_or_invalid_obfs_is_rejected_by_detection_and_export(self) -> None:
        base = self.generate_inbound(obfs_password="valid-obfs", tag="invalid-obfs")
        variants = {
            "unknown-type": {"type": "other", "password": "valid-obfs"},
            "unknown-field": {"type": "salamander", "password": "valid-obfs", "extra": True},
            "short-password": {"type": "salamander", "password": "abc"},
            "wrong-value-type": "salamander",
        }
        for name, obfs in variants.items():
            with self.subTest(case=name):
                inbound = dict(base)
                inbound["tag"] = name
                inbound["obfs"] = obfs
                path = self.root / (name + ".json")
                fixture.dump(path, inbound)
                detected = self.shell('detect_inbound_template "$1"', str(path))
                self.assertNotEqual(detected.returncode, 0, detected.stdout + detected.stderr)
                self._write_inbound(inbound)
                edited = self.shell('edit_wizard_locked "$1"', name)
                self.assertNotEqual(edited.returncode, 0, edited.stdout + edited.stderr)
                exported = self.shell('generate_client "$1" "example.com"', str(path))
                self.assertNotEqual(exported.returncode, 0, exported.stdout + exported.stderr)
                self.assertEqual(exported.stdout.strip(), "")
                self.assertNotIn("valid-obfs", exported.stdout + exported.stderr)

    def test_prompt_hysteria2_obfs_enable_keep_rotate_disable_and_cancel(self) -> None:
        core = self.root / "obfs-core"
        core.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ $1 == generate && $2 == rand ]]; then printf '%s\\n' generated-obfs-password; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        core.chmod(0o755)

        def run_helper(choice: str, current: str, secret: str, *, cancel: bool = False) -> subprocess.CompletedProcess[str]:
            body = r'''
                TEST_OBFS_CHOICE="$1"
                TEST_OBFS_CURRENT="$2"
                TEST_OBFS_INPUT="$3"
                TEST_CANCEL="$5"
                CORE_BIN="$4"
                prompt() {
                    [[ $1 == obfs_choice ]] || return 97
                    [[ $TEST_CANCEL == cancel ]] && return 1
                    printf -v "$1" '%s' "$TEST_OBFS_CHOICE"
                }
                prompt_secret() {
                    if [[ -n $TEST_OBFS_INPUT ]]; then
                        printf -v "$1" '%s' "$TEST_OBFS_INPUT"
                    else
                        printf -v "$1" '%s' "$3"
                    fi
                }
                obfs_password=sentinel
                prompt_hysteria2_obfs obfs_password "$TEST_OBFS_CURRENT"
                status=$?
                printf '%s|%s\n' "$status" "$obfs_password"
                exit "$status"
            '''
            return self.shell(body, choice, current, secret, str(core), "cancel" if cancel else "")

        enabled = run_helper("2", "", "")
        self.assertEqual(enabled.returncode, 0, enabled.stdout + enabled.stderr)
        self.assertEqual(enabled.stdout.strip(), "0|generated-obfs-password")
        generated = run_helper("2", "", "@new")
        self.assertEqual(generated.stdout.strip(), "0|generated-obfs-password")
        kept = run_helper("2", "old-obfs", "")
        self.assertEqual(kept.stdout.strip(), "0|old-obfs")
        rotated = run_helper("2", "old-obfs", "new-obfs")
        self.assertEqual(rotated.stdout.strip(), "0|new-obfs")
        disabled = run_helper("1", "old-obfs", "")
        self.assertEqual(disabled.stdout.strip(), "0|")
        menu_canceled = run_helper("0", "old-obfs", "")
        self.assertNotEqual(menu_canceled.returncode, 0)
        self.assertTrue(menu_canceled.stdout.endswith("|sentinel\n"), menu_canceled.stdout)
        canceled = run_helper("2", "old-obfs", "new-obfs", cancel=True)
        self.assertNotEqual(canceled.returncode, 0)
        self.assertTrue(canceled.stdout.endswith("|sentinel\n"), canceled.stdout)

    def test_edit_hysteria2_obfs_lifecycle_preserves_jsonc_and_metadata(self) -> None:
        certificate, private_key = self.tls_material()
        cases = (
            ("enable", "", "2", "new-obfs", True, "new-obfs"),
            ("keep", "old-obfs", "2", "", True, "old-obfs"),
            ("keep-tail", "old-obfs\n", "2", "", True, "old-obfs\n"),
            ("rotate", "old-obfs", "2", "rotated-obfs", True, "rotated-obfs"),
            ("disable", "old-obfs", "1", "", True, None),
            ("cancel", "", "2", "cancelled-obfs", False, None),
        )
        for name, original_obfs, choice, secret, confirm, expected in cases:
            with self.subTest(case=name):
                inbound = self.generate_inbound(
                    obfs_password=original_obfs,
                    tag="edit-hy2-obfs",
                    port=23000,
                )
                before = self._write_inbound(inbound, comment=True)
                metadata_path = self._write_metadata(inbound)
                before_metadata = metadata_path.read_bytes()
                body = r'''
                    SECRET_CALLS=0
                    prompt() {
                        local variable=$1
                        case "$variable" in
                            obfs_choice) printf -v "$variable" '%s' "$TEST_OBFS_CHOICE" ;;
                            tls_name) printf -v "$variable" '%s' example.com ;;
                            server) printf -v "$variable" '%s' example.com ;;
                            *) printf -v "$variable" '%s' "${3-}" ;;
                        esac
                    }
                    prompt_secret() {
                        SECRET_CALLS=$((SECRET_CALLS + 1))
                        if [[ $SECRET_CALLS == 1 ]]; then
                            printf -v "$1" '%s' hy-password
                        else
                            if [[ -n $TEST_OBFS_SECRET ]]; then
                                printf -v "$1" '%s' "$TEST_OBFS_SECRET"
                            else
                                printf -v "$1" '%s' "$3"
                            fi
                        fi
                    }
                    prompt_tls_certificate_files() {
                        printf -v certificate '%s' "$TEST_CERTIFICATE"
                        printf -v tls_key '%s' "$TEST_PRIVATE_KEY"
                    }
                    confirm() [[ $TEST_CONFIRM == 1 ]]
                    edit_wizard_locked edit-hy2-obfs
                '''
                env = dict(
                    self.env,
                    TEST_CERTIFICATE=str(certificate),
                    TEST_PRIVATE_KEY=str(private_key),
                    TEST_OBFS_CHOICE=choice,
                    TEST_OBFS_SECRET=secret,
                    TEST_CONFIRM="1" if confirm else "0",
                )
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; shift; ' + body, "edit-hy2-obfs", str(fixture.SCRIPT), choice, choice, secret],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                if not confirm:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.config.read_text(encoding="utf-8"), before)
                    self.assertEqual(metadata_path.read_bytes(), before_metadata)
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = json.loads(self.shell('config_jq .', check=True).stdout)
                saved_inbound = saved["inbounds"][0]
                self.assertIn("// keep Hysteria 2 comments", self.config.read_text(encoding="utf-8"))
                if expected is None:
                    self.assertNotIn("obfs", saved_inbound)
                else:
                    self.assertEqual(saved_inbound["obfs"], {"type": "salamander", "password": expected})
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["inbound"], saved_inbound)

    def test_add_hysteria2_obfs_wizard_saves_enabled_and_default_disabled(self) -> None:
        integration_core = self.root / "integration-core"
        integration_core.write_text(
            fixture.MOCK_CORE.replace(
                "args = sys.argv[1:]\n",
                "args = sys.argv[1:]\n"
                "if args[:2] == ['generate', 'rand']:\n"
                "    print('integration-password')\n"
                "    sys.exit(0)\n",
            ),
            encoding="utf-8",
        )
        integration_core.chmod(0o755)
        certificate, private_key = self.tls_material()
        for choice, secret, expected in (("1", "unused-obfs", None), ("2", "wizard-obfs", "wizard-obfs")):
            with self.subTest(choice=choice):
                self.config.write_bytes(self.original_bytes)
                tag = "wizard-hy2-" + choice
                env = dict(
                    self.env,
                    TEST_OBFS_CHOICE=choice,
                    TEST_OBFS_SECRET=secret,
                    TEST_TAG=tag,
                    TEST_CERTIFICATE=str(certificate),
                    TEST_PRIVATE_KEY=str(private_key),
                    TEST_WIZARD_CORE=str(integration_core),
                )
                body = r'''
                    require_core() { return 0; }
                    prompt() {
                        local variable=$1
                        case "$variable" in
                            choice) printf -v "$variable" '%s' 7 ;;
                            tag) printf -v "$variable" '%s' "$TEST_TAG" ;;
                            port) printf -v "$variable" '%s' 24007 ;;
                            obfs_choice) printf -v "$variable" '%s' "$TEST_OBFS_CHOICE" ;;
                            tls_name) printf -v "$variable" '%s' example.com ;;
                            certificate) printf -v "$variable" '%s' "$TEST_CERTIFICATE" ;;
                            tls_key) printf -v "$variable" '%s' "$TEST_PRIVATE_KEY" ;;
                            server) printf -v "$variable" '%s' example.com ;;
                            *) printf -v "$variable" '%s' '' ;;
                        esac
                    }
                    prompt_secret() { printf -v "$1" '%s' "$TEST_OBFS_SECRET"; }
                    prompt_tls_certificate_files() {
                        printf -v certificate '%s' "$TEST_CERTIFICATE"
                        printf -v tls_key '%s' "$TEST_PRIVATE_KEY"
                    }
                    CORE_BIN="$TEST_WIZARD_CORE"
                    add_wizard_locked
                '''
                result = subprocess.run(
                    ["bash", "-c", 'source "$1"; ' + body, "add-hy2-obfs", str(fixture.SCRIPT)],
                    env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = json.loads(self.config.read_text(encoding="utf-8"))["inbounds"][0]
                self.assertEqual(saved["tag"], tag)
                if expected is None:
                    self.assertNotIn("obfs", saved)
                else:
                    self.assertEqual(saved["obfs"], {"type": "salamander", "password": expected})


if __name__ == "__main__":
    unittest.main()
