#!/usr/bin/env python3
"""Thin wrapper around `quern capture-env`.

The command is the interface; this exists because the command needs `quern` to
run, and the situation you want a diagnostic for is one where it might not.
Imports the package directly with stdlib only, so it works on a checkout whose
venv is broken or missing entirely -- which is the case that produced the first
fixture in tests/fixtures/envs/.

    python3 scripts/capture-env.py                      # to stdout
    python3 scripts/capture-env.py env.json             # to a file
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Apple's Command Line Tools have shipped Python 3.9.6 as /usr/bin/python3 since
# Xcode 14, and still do -- so 3.9 is the floor this has to clear, well below
# the 3.11 quern itself requires. Older Xcodes shipped 3.8.9, but a macOS that
# old is outside Xcode's own support window, so nobody is on it.
#
# If that floor is ever wrong, say so plainly. A traceback about `datetime.UTC`
# on the one machine this exists for is the least useful possible output.
try:
    from server.lifecycle.capture_env import run  # noqa: E402
except (ImportError, SyntaxError) as exc:  # pragma: no cover - exercised by hand
    print(f"This needs a newer Python than {sys.version.split()[0]}: {exc}", file=sys.stderr)
    print("Try: python3.11 scripts/capture-env.py, or `quern capture-env`.", file=sys.stderr)
    raise SystemExit(1) from None

def _usage() -> None:
    print("Usage: python3 scripts/capture-env.py [FILE]")
    print()
    print("Writes an environment report for attaching to a bug report.")
    print("With no FILE, prints to stdout.")


def _main(argv: list[str]) -> int:
    # The same flag handling `quern capture-env` has. It was fixed only there,
    # leaving it live in the file the README points at for when quern is broken
    # -- so `--help` wrote a file called "--help" and exited 0.
    if any(a in ("-h", "--help") for a in argv):
        _usage()
        return 0
    if any(arg.startswith("-") for arg in argv):
        _usage()
        # The first argument that is actually a flag. Reporting argv[0] named a
        # perfectly good filename in `capture-env out.json --bogus`.
        bad = next(a for a in argv if a.startswith("-"))
        print(f"unrecognised option: {bad}", file=sys.stderr)
        return 2
    if len(argv) > 1:
        print(f"Expected at most one FILE, got {len(argv)}.", file=sys.stderr)
        return 2
    return run(argv[0] if argv else None)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
