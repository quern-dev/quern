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

if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
