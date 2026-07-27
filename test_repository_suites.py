import subprocess
import sys
import unittest


class RepositorySuiteDiscoveryTests(unittest.TestCase):
    def test_every_repository_suite_runs(self):
        commands = [
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            [sys.executable, "-m", "unittest", "discover", "-s", "plugins/my-journal/tests", "-v"],
            [sys.executable, "-m", "unittest", "discover", "-s", "skills/note-taking/my-journal/tests", "-v"],
        ]
        for command in commands:
            completed = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("Ran ", completed.stderr)


if __name__ == "__main__":
    unittest.main()
