#!/usr/bin/env python3
"""One-command setup so Hedwig's learned classifier ALWAYS runs.

Claude Code launches the hooks under a bare `python3` that usually lacks
numpy / scikit-learn, so the online PolicyClassifier can't load and Hedwig
falls back to the heuristic. This script builds a dedicated virtualenv at a
fixed path the hooks auto-discover:

    ~/.hedwig/venv

with the learned-scorer dependencies installed. Once it exists, every hook
re-execs itself under it (see _hedwig_common.ensure_learned_interpreter), so
the learned scorer runs regardless of which interpreter Claude Code used — no
shell-profile edit, works in any terminal, survives plugin updates.

Run it once per machine (e.g. the booth laptop):

    python3 plugin/bin/hedwig-setup.py

Idempotent: re-running upgrades the deps in place. Use --recreate to rebuild
from scratch, --python PATH to base the venv on a specific interpreter.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path

# Must match _hedwig_common._BOOTH_VENV.
VENV_DIR = Path.home() / ".hedwig" / "venv"
# numpy + scikit-learn power the classifier; fastembed powers semantic rule
# retrieval. None pull torch / GPU. Pinned floors match pyproject.toml.
DEPS = ["numpy>=1.26.0", "scikit-learn>=1.4.0", "fastembed>=0.3.0"]


def _venv_python(venv_dir: Path) -> Path:
    # Windows lays the interpreter out under Scripts/; POSIX under bin/.
    win = venv_dir / "Scripts" / "python.exe"
    return win if win.exists() else venv_dir / "bin" / "python"


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up Hedwig's learned-scorer interpreter.")
    parser.add_argument("--recreate", action="store_true", help="rebuild the venv from scratch")
    parser.add_argument("--python", default=sys.executable, help="base interpreter for the venv")
    args = parser.parse_args()

    if args.recreate and VENV_DIR.exists():
        print(f"removing existing venv at {VENV_DIR}")
        shutil.rmtree(VENV_DIR)

    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)

    py = _venv_python(VENV_DIR)
    if not py.exists():
        print(f"creating venv at {VENV_DIR} (base: {args.python})")
        # Build the venv with the chosen base interpreter so it can differ from
        # the one running this script; fall back to the stdlib venv module when
        # base == current interpreter.
        if Path(args.python).resolve() == Path(sys.executable).resolve():
            venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
        else:
            subprocess.run([args.python, "-m", "venv", str(VENV_DIR)], check=True)
        py = _venv_python(VENV_DIR)
    else:
        print(f"venv already exists at {VENV_DIR} — upgrading deps")

    print(f"installing learned-scorer deps: {', '.join(DEPS)}")
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", *DEPS], check=True)

    # Self-verify: the classifier's deps must actually import in the new venv.
    probe = subprocess.run(
        [str(py), "-c", "import numpy, sklearn; print(numpy.__version__, sklearn.__version__)"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print("ERROR: deps did not import in the new venv:", file=sys.stderr)
        print(probe.stderr, file=sys.stderr)
        return 1

    versions = probe.stdout.strip()
    print()
    print("✓ Hedwig learned-scorer interpreter ready.")
    print(f"  interpreter: {py}")
    print(f"  numpy / scikit-learn: {versions}")
    print("  The plugin hooks will auto-discover it — no shell config needed.")
    print("  The online classifier now runs on every governed edit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
