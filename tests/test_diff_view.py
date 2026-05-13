from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.run.diff_view import render_proposed_patch


class DiffViewSmokeTests(unittest.TestCase):
    """Not checking exact terminal output — just that the renderer doesn't
    raise on realistic inputs and terminates on pathological ones."""

    def test_edit_renders_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def hi():\n    return 1\n")
            render_proposed_patch(
                root, {"a.py": "def hi():\n    return 2\n"}, ["a.py"]
            )

    def test_new_file_renders_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            render_proposed_patch(
                root, {"new.py": "def f(): return 0\n"}, ["new.py"]
            )

    def test_no_change_renders_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            render_proposed_patch(root, {"a.py": "x = 1\n"}, ["a.py"])

    def test_large_diff_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = "\n".join(f"old line {i}" for i in range(200)) + "\n"
            updated = "\n".join(f"new line {i}" for i in range(200)) + "\n"
            (root / "big.py").write_text(original)
            # Should not raise or hang.
            render_proposed_patch(root, {"big.py": updated}, ["big.py"])

    def test_missing_path_in_updates_renders_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            render_proposed_patch(root, {}, ["a.py"])


if __name__ == "__main__":
    unittest.main()
