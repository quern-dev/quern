"""scripts/cr-findings.sh: findings in the review body, whatever CodeRabbit calls them.

CodeRabbit writes the count of a review-body section two ways: "Nitpick comments
(4)" and, since October 2026, "🧹 Nitpick comments: 4". The script matched only
the first, so PR #472's four nitpicks printed as an empty section -- exactly
what a clean review prints. They were found by reading the review by hand.

`gh` is stubbed on PATH, so this reaches no network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cr-findings.sh"

NEW_FORMAT = """<details>
<summary>🧹 Nitpick comments: 2</summary><blockquote>
`205-207`: **`AbortError` is treated as a timeout, but the signal only gives `TimeoutError`.**
`22-25`: **A refusal that carries a connection code can still receive the hint.**
</blockquote></details>"""

OLD_FORMAT = """<details>
<summary>⚠️ Outside diff range comments (1)</summary><blockquote>
`88-90`: **A queued value equal to the one just written is written again.**
</blockquote></details>"""

UNKNOWN_SECTION = """<details>
<summary>🤔 Speculative comments: 3</summary><blockquote>
**Something nobody parses yet.**
</blockquote></details>"""

GH_STUB = """#!/bin/bash
# Only `gh pr view ... reviews,comments` returns anything: the review body
# under test. Everything else is silent, so the other sections render empty.
for a in "$@"; do case "$a" in *reviews,comments*) BODY=1;; esac; done
if [ -n "${BODY:-}" ]; then printf '%s\\n' "$REVIEW_BODY"; fi
exit 0
"""


@pytest.fixture
def run_script(tmp_path):
    if shutil.which("bash") is None:  # pragma: no cover - bash is assumed
        pytest.skip("bash not available")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)

    def run(body: str) -> str:
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["REVIEW_BODY"] = body
        out = subprocess.run(
            ["bash", str(SCRIPT), "472"], env=env, capture_output=True, text=True,
            timeout=60,
        )
        section = out.stdout.split("── everything else", 1)[-1].split("── pre-merge", 1)[0]
        return section

    return run


def test_the_colon_count_format_is_read(run_script):
    out = run_script(NEW_FORMAT)
    assert "Nitpick comments (2):" in out, out
    assert "only gives `TimeoutError`" in out
    assert "at lines 205-207" in out


def test_the_parenthesised_count_format_still_is(run_script):
    out = run_script(OLD_FORMAT)
    assert "Outside diff range comments (1):" in out, out
    assert "written again" in out


def test_an_unrecognised_section_is_reported_not_skipped(run_script):
    """A third heading format, or a new section name, must not read as clean."""
    out = run_script(UNKNOWN_SECTION)
    assert "WARNING: an unrecognised findings section" in out, out
    assert "Speculative comments" in out
