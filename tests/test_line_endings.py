"""The suite runs on Linux: no Windows line endings or BOMs, script executable.

A CRLF in perfmon.sh makes it fail with "/usr/bin/env: 'bash\\r': No such
file or directory"; .gitattributes keeps Windows clones from adding them.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".state", "data", "__pycache__"}
BINARY = (".png", ".pyc")


def repo_text_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(BINARY):
                yield os.path.join(dirpath, name)


class LineEndingTest(unittest.TestCase):
    def test_no_crlf_or_bom(self):
        bad = []
        for path in repo_text_files():
            with open(path, "rb") as f:
                data = f.read()
            rel = os.path.relpath(path, ROOT)
            if b"\r" in data:
                bad.append("%s: contains CR (Windows line endings)" % rel)
            if data.startswith(b"\xef\xbb\xbf"):
                bad.append("%s: starts with a UTF-8 BOM" % rel)
        self.assertEqual(bad, [], "fix with: sed -i 's/\\r$//' <file>")

    def test_script_is_runnable(self):
        script = os.path.join(ROOT, "perfmon.sh")
        with open(script, "rb") as f:
            self.assertEqual(f.readline(), b"#!/usr/bin/env bash\n")
        self.assertTrue(os.access(script, os.X_OK), "perfmon.sh lost its executable bit: chmod +x perfmon.sh")

    def test_gitattributes_forces_lf(self):
        with open(os.path.join(ROOT, ".gitattributes")) as f:
            rules = f.read()
        self.assertIn("eol=lf", rules)
        self.assertIn("*.sh", rules)


if __name__ == "__main__":
    unittest.main()
