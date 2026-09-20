#!/usr/bin/env python3
"""Record what `pr-review-status.py` saw for one PR, as a replayable fixture.

    scripts/capture-review-state.py <pr> <scenario> ["what was really true"]

Writes `tests/fixtures/review-state/<scenario>.json`: every `gh` call the
status check made, in order, with its exact stdout, plus the verdict it
reached. A test replays the calls and asserts on the verdict, so a change to
the check can be run against states that actually occurred rather than against
states someone imagined.

The captures are taken by *running the real check* with its `gh` wrapped,
rather than by writing out the queries by hand. Hand-written fixtures agree
with whatever the author believed the check asks for, which is exactly the
belief under test -- and the check's queries have changed twice already.

**Nothing here posts a comment.** Capture runs with `ask=False`, so a rate
limited PR stays rate limited and a reviewed one is not re-asked.

The third argument is the point of the file: what was *actually* true when the
capture was taken, in one line, written by whoever took it. The check's verdict
is recorded next to it, and where the two disagree the fixture is a bug report.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review-state"
CHECK = REPO_ROOT / "scripts" / "pr-review-status.py"


def _load_check():
    """Import pr-review-status.py, whose name is not a module name."""
    spec = importlib.util.spec_from_file_location("pr_review_status", CHECK)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load {CHECK}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The check only detects the repo when it is `__main__`, so an import gets
    # the placeholder ("unknown/unknown") and every `gh --repo` call fails with
    # "error connecting to unknown". Detect it here, the same way it would.
    module.HOST, module.REPO = module.detect_repo()
    return module


def capture(number: int, scenario: str, truth: str) -> Path:
    check = _load_check()
    calls: list[dict] = []

    real_gh = check.gh

    def recording_gh(*args: str) -> str:
        out = real_gh(*args)
        calls.append({"args": list(args), "stdout": out})
        return out

    check.gh = recording_gh

    # The check shells out to git directly for the head comparison; that is a
    # real input too, and a fixture without it cannot be replayed off-machine.
    real_run = subprocess.run
    git_calls: list[dict] = []

    def recording_run(cmd, *a, **k):
        result = real_run(cmd, *a, **k)
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            git_calls.append({
                "args": list(cmd),
                "stdout": getattr(result, "stdout", "") or "",
                "returncode": result.returncode,
            })
        return result

    subprocess.run = recording_run
    try:
        verdict, line = check.status(number)
    finally:
        subprocess.run = real_run

    record = {
        "scenario": scenario,
        "pr": number,
        "repo": check.REPO,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # What was really going on, in the capturer's words. The whole value of
        # these files is this line disagreeing with `verdict` where it should.
        "ground_truth": truth,
        "verdict": verdict,
        "line": line,
        "gh_calls": calls,
        "git_calls": git_calls,
    }

    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{scenario}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n")
    return path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    number = int(sys.argv[1])
    scenario = sys.argv[2]
    truth = sys.argv[3] if len(sys.argv) > 3 else ""
    path = capture(number, scenario, truth)
    record = json.loads(path.read_text())
    print(f"{path.relative_to(REPO_ROOT)}")
    print(f"  verdict: {record['verdict']} — {record['line']}")
    if truth:
        print(f"  truth:   {truth}")
    print(f"  {len(record['gh_calls'])} gh call(s), {len(record['git_calls'])} git call(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
