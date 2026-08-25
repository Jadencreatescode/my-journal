from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "plugins" / "my-journal" / "descriptor_exec.py"


@unittest.skipUnless(os.name == "posix", "descriptor execution requires POSIX descriptors")
class DescriptorExecTests(unittest.TestCase):
    def test_inherited_directory_descriptor_becomes_child_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            descriptor = os.open(tmp, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(WRAPPER),
                        str(descriptor),
                        "--",
                        sys.executable,
                        "-c",
                        "import os; print(os.getcwd())",
                    ],
                    pass_fds=(descriptor,),
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                os.close(descriptor)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(Path(completed.stdout.strip()).samefile(tmp))

    def test_regular_file_descriptor_is_rejected(self):
        with tempfile.NamedTemporaryFile() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    str(temporary.fileno()),
                    "--",
                    sys.executable,
                    "-c",
                    "print('must not run')",
                ],
                pass_fds=(temporary.fileno(),),
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("must not run", completed.stdout)
        self.assertIn("not a directory", completed.stderr)


if __name__ == "__main__":
    unittest.main()
