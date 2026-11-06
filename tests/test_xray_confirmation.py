"""Exercise the shared confirmation through a real terminal, without live writes."""
from pathlib import Path
import os
import shutil
import subprocess
import time
import unittest

if os.name != "nt":
    import pty
    import select

SCRIPT = Path(__file__).resolve().parents[1] / "xray-manager.sh"
DRIVER = 'source "$1"; confirm "保存测试操作？"'


@unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "Linux PTY required")
class XrayConfirmationTest(unittest.TestCase):
    def test_default_yes_explicit_answers_and_eof(self):
        for answer, status in ((b"\n", 0), (b"y\n", 0), (b"Y\n", 0),
                               (b"n\n", 1), (b"N\n", 1), (b"invalid\n", 1), (b"\x04", 1)):
            with self.subTest(answer=answer):
                master, slave = pty.openpty()
                process = subprocess.Popen(["bash", "-c", DRIVER, "confirmation-test", str(SCRIPT)],
                                           stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                output = b""
                deadline = time.monotonic() + 10
                try:
                    while b"[Y/n]: " not in output:
                        self.assertLess(time.monotonic(), deadline, "confirmation prompt missing")
                        if select.select([master], [], [], 0.1)[0]:
                            output += os.read(master, 65536)
                    self.assertIn("保存测试操作？（默认 Y） [Y/n]: ".encode(), output)
                    os.write(master, answer)
                    self.assertEqual(process.wait(timeout=5), status)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    os.close(master)

    def test_noninteractive_input_does_not_confirm(self):
        for answer in ("\n", "y\n", ""):
            with self.subTest(answer=answer):
                result = subprocess.run(["bash", "-c", DRIVER, "confirmation-test", str(SCRIPT)],
                                        input=answer, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("此操作需要交互确认", result.stderr)


if __name__ == "__main__":
    unittest.main()
