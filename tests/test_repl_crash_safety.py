"""REPL crash-safety contracts.

The interactive REPL (`sc/run/repl.py`) must survive the things real users do
mid-task: hit Ctrl-C / Ctrl-D at a prompt, or have a file change on disk while
a patch is in flight (an editor autosave, a concurrent process). None of these
may unwind the whole session with a traceback — they return to the prompt.

`run_repl` itself needs a live Bedrock client + a tty, so it isn't unit
-drivable. Instead we pin the two contracts the loop depends on:

  1. `_apply_updates_and_verify` raises `typer.Exit` when a touched file
     changed since the model read it — the loop catches that and continues.
  2. `_confirm_create_files` propagates KeyboardInterrupt / EOFError from the
     underlying Rich prompt — which is exactly why the loop wraps the call.

If either contract changes, the REPL's except-clauses need re-checking, so
these tests guard the assumption rather than the loop body directly.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from sc.config import SAConfig
from sc.run.apply_stage import _apply_updates_and_verify
from sc.run.ui import _confirm_create_files
from sc.schema import IntentDeclaration
from sc.trust_db import TrustDB


class ApplyFileRaceTests(unittest.TestCase):
    """A file that changes on disk between read and write must abort the apply
    with typer.Exit (a skip-this-patch signal), NOT silently overwrite the
    user's concurrent change and NOT crash. The REPL catches the Exit."""

    def test_apply_aborts_with_typer_exit_when_file_changed_underneath(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            db = TrustDB(repo_root / "trust.db")
            target = repo_root / "calc.py"
            target.write_text("def add(a, b):\n    return a + b\n")

            # The hash the loop captured reflects some EARLIER content, so the
            # current on-disk file looks "changed since model response".
            stale_hash = hashlib.sha256(b"old different content").hexdigest()

            decl = IntentDeclaration(
                task_summary="t",
                planned_files=["calc.py"],
                planned_actions=["edit_code"],
                planned_commands=[],
                expected_change_types=["general_change"],
            )
            cfg = SAConfig(model_id="model", verification_enabled=False)

            with self.assertRaises(typer.Exit) as ctx:
                _apply_updates_and_verify(
                    repo_root=repo_root,
                    config=cfg,
                    trust_db=db,
                    repo_root_str=str(repo_root),
                    run_session_id="s1",
                    declaration=decl,
                    updates={"calc.py": "def add(a, b):\n    return a - b\n"},
                    touched_files=["calc.py"],
                    file_hashes={"calc.py": stale_hash},
                )
            self.assertEqual(ctx.exception.exit_code, 1)
            # The user's concurrent content is untouched — we aborted, not wrote.
            self.assertIn("return a + b", target.read_text())


class ConfirmCreateFilesInterruptTests(unittest.TestCase):
    """_confirm_create_files is built on Rich's Prompt.ask, which re-raises
    KeyboardInterrupt and EOFError. The REPL relies on that propagation to turn
    a Ctrl-C / Ctrl-D at the create-files prompt into a clean 'patch skipped'.
    If this prompt were ever changed to swallow them, the loop's guard would be
    silently dead — so pin the propagation here."""

    def test_keyboard_interrupt_propagates(self) -> None:
        with patch("sc.run.ui.Prompt.ask", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                _confirm_create_files(["new.py"])

    def test_eof_propagates(self) -> None:
        with patch("sc.run.ui.Prompt.ask", side_effect=EOFError()):
            with self.assertRaises(EOFError):
                _confirm_create_files(["new.py"])


if __name__ == "__main__":
    unittest.main()
