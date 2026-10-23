import contextlib
import difflib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bump_version


class BumpVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="vikram-bump-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for place in bump_version.PLACES:
            target = self.root / place.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bump_version.ROOT / place.path, target)
        self.current = bump_version.agreed_version(self.root)
        major, minor, _ = map(int, self.current.split("."))
        self.next = f"{major}.{minor + 1}.0"

    def snapshot(self):
        return {
            place.path: (self.root / place.path).read_bytes()
            for place in bump_version.PLACES
        }

    def test_bump_changes_only_the_version_strings(self):
        before = self.snapshot()
        changed = bump_version.bump(self.next, self.root)
        self.assertEqual(len(changed), 7)
        self.assertEqual(
            set(bump_version.current_versions(self.root).values()), {self.next}
        )
        edits = []
        for path, old in before.items():
            new = (self.root / path).read_text().splitlines()
            for line in difflib.unified_diff(old.decode().splitlines(), new, n=0):
                if line[:1] in "+-" and line[:3] not in ("+++", "---"):
                    edits.append(line[1:].replace(self.next, "VERSION"))
        # Two edits per place: the removed and the added line.
        self.assertEqual(len(edits), 2 * len(bump_version.PLACES))
        for removed, added in zip(edits[::2], edits[1::2]):
            self.assertEqual(removed.replace(self.current, "VERSION"), added)

    def test_refuses_versions_that_are_not_newer(self):
        before = self.snapshot()
        for version in (self.current, "0.0.1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(SystemExit, "not newer"):
                    bump_version.bump(version, self.root)
        self.assertEqual(self.snapshot(), before)

    def test_disagreeing_or_missing_versions_change_nothing(self):
        config = self.root / "apps/desktop/src-tauri/tauri.conf.json"
        config.write_text(config.read_text().replace(self.current, "9.9.9"))
        before = self.snapshot()
        with self.assertRaisesRegex(
            SystemExit, "disagree(.|\n)*tauri.conf.json: 9.9.9"
        ):
            bump_version.bump(self.next, self.root)
        config.write_text(config.read_text().replace('"version": "9.9.9",\n', ""))
        with self.assertRaisesRegex(SystemExit, "Cannot find.*tauri.conf.json"):
            bump_version.bump(self.next, self.root)
        self.assertEqual(
            {k: v for k, v in self.snapshot().items() if "tauri.conf" not in k},
            {k: v for k, v in before.items() if "tauri.conf" not in k},
        )

    def test_check_prints_the_agreed_version_or_names_the_mismatch(self):
        output = io.StringIO()
        with (
            patch.object(bump_version, "ROOT", self.root),
            patch("sys.argv", ["bump_version.py", "--check"]),
            contextlib.redirect_stdout(output),
        ):
            bump_version.main()
        self.assertEqual(output.getvalue(), f"{self.current}\n")
        with (
            patch.object(bump_version, "ROOT", self.root),
            patch("sys.argv", ["bump_version.py", "--check", self.next]),
        ):
            with self.assertRaisesRegex(SystemExit, f"tag says {self.next}"):
                bump_version.main()
        with patch("sys.argv", ["bump_version.py", "--check", "1.2"]):
            with self.assertRaisesRegex(SystemExit, "MAJOR.MINOR.PATCH"):
                bump_version.main()
