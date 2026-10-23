"""scripts/hinge-pin.py: is the hinge helper behind baguette's? (#537)

The weekly workflow reads its exit code, so the line that matters is between
"nothing new" (0), "something to review" (1) and "could not ask" (2): the last
must never read as the first. GitHub is faked; nothing here reaches it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "hinge-pin.py"
PINNED = "a" * 40
NEWER = "b" * 40
NEWEST = "c" * 40


def _load():
    spec = importlib.util.spec_from_file_location("hinge_pin", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pin(tmp_path, monkeypatch):
    mod = _load()
    path = tmp_path / "hinge-pin.json"
    path.write_text(json.dumps({"repo": "tddworks/baguette", "path": "Injected/HingeControl",
                                "commit": PINNED, "reviewed": "2026-10-01", "note": "kept"}))
    monkeypatch.setattr(mod, "PIN_FILE", path)

    def no_network(url):
        raise AssertionError(f"reached the network: {url}")

    monkeypatch.setattr(mod, "_get", no_network)
    return mod, path


def _commit(sha, message="Fix the hinge\n\nDetails"):
    return {"sha": sha, "commit": {"message": message,
                                   "committer": {"date": "2026-10-20T10:00:00Z"}}}


def _github(history, commits=None):
    """A fake GitHub: the path's history (newest first, one page) and the
    single-commit endpoint."""
    asked = []

    def get(url):
        asked.append(url)
        if "/commits?" in url:
            assert "path=Injected/HingeControl" in url
            return history if url.endswith("&page=1") else []
        sha = url.rsplit("/", 1)[1]
        for c in (commits if commits is not None else history):
            if c["sha"].startswith(sha):
                return {"sha": c["sha"]}
        raise _load().CouldNotAsk("404")

    get.asked = asked
    return get


def test_nothing_new_is_0(pin, capsys):
    mod, _ = pin
    assert mod.check(False, _github([_commit(PINNED)])) == 0
    assert "has not changed" in capsys.readouterr().out


def test_new_commits_are_1_and_listed(pin, capsys):
    mod, _ = pin
    code = mod.check(True, _github([_commit(NEWEST), _commit(NEWER), _commit(PINNED)]))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["behind"] is True
    assert [c["sha"] for c in out["commits"]] == [NEWEST, NEWER]
    assert out["commits"][0]["message"] == "Fix the hinge"
    assert out["head"] == NEWEST
    assert out["compare"] == f"https://github.com/tddworks/baguette/compare/{PINNED}...{NEWEST}"


def test_the_json_has_what_the_workflow_reads_when_current(pin, capsys):
    mod, _ = pin
    mod.check(True, _github([_commit(PINNED)]))
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"behind", "pinned", "head", "commits", "compare"}
    assert out["behind"] is False and out["head"] == PINNED


@pytest.mark.parametrize("answer", [
    {"message": "API rate limit exceeded"},       # an error body, not a list
    [{"no": "sha"}],                              # a list of something else
    [{"sha": NEWER, "commit": None}],             # a commit it cannot read
])
def test_an_answer_it_cannot_read_is_2(pin, answer, capsys):
    mod, _ = pin
    assert mod.check(False, lambda _url: answer) == 2
    assert "could not check" in capsys.readouterr().err


def test_github_unreachable_is_2(pin, capsys):
    mod, _ = pin

    def down(url):
        raise mod.CouldNotAsk(f"{url}: connection refused")

    assert mod.check(False, down) == 2


def test_a_pin_missing_from_upstream_is_2_not_current(pin):
    """A rewritten upstream or a typo in the pin must not read as nothing new."""
    mod, _ = pin
    assert mod.check(False, _github([_commit(NEWEST), _commit(NEWER)])) == 2


def test_history_is_read_past_the_first_page(pin):
    mod, _ = pin
    page1 = [_commit(f"{i:040x}") for i in range(1, 101)]

    def get(url):
        if url.endswith("&page=1"):
            return page1
        if url.endswith("&page=2"):
            return [_commit(PINNED)]
        return []

    assert len(mod.commits_since(json.loads(mod.PIN_FILE.read_text()), get)) == 100


def test_the_real_getter_turns_a_network_failure_into_could_not_ask(monkeypatch):
    mod = _load()

    def refuse(*_a, **_kw):
        raise mod.urllib.error.URLError("offline")

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse)
    with pytest.raises(mod.CouldNotAsk):
        mod._get("https://api.github.com/x")


def test_bump_defaults_to_the_newest_and_keeps_the_rest(pin):
    mod, path = pin
    history = [_commit(NEWEST), _commit(NEWER), _commit(PINNED)]
    assert mod.bump(None, _github(history), today="2026-10-23") == 0
    saved = json.loads(path.read_text())
    assert saved["commit"] == NEWEST and saved["reviewed"] == "2026-10-23"
    assert saved["note"] == "kept" and saved["repo"] == "tddworks/baguette"


def test_bump_records_a_review_that_took_nothing(pin):
    """Reviewed, nothing to take, is a bump like any other."""
    mod, path = pin
    history = [_commit(NEWEST), _commit(NEWER), _commit(PINNED)]
    assert mod.bump(NEWER[:10], _github(history), today="2026-10-23") == 0
    assert json.loads(path.read_text())["commit"] == NEWER, "the full sha, not the prefix"


def test_bump_with_nothing_new_changes_nothing(pin):
    mod, path = pin
    before = path.read_text()
    assert mod.bump(None, _github([_commit(PINNED)])) == 0
    assert path.read_text() == before


def test_bump_refuses_a_commit_outside_the_path(pin):
    """Pinning it would make every later check exit 2."""
    mod, path = pin
    before = path.read_text()
    elsewhere = "d" * 40
    get = _github([_commit(NEWER), _commit(PINNED)],
                  commits=[_commit(NEWER), _commit(PINNED), _commit(elsewhere)])
    assert mod.bump(elsewhere, get) == 2
    assert path.read_text() == before


def test_bump_that_could_not_ask_writes_nothing(pin):
    mod, path = pin
    before = path.read_text()

    def down(url):
        raise mod.CouldNotAsk("offline")

    assert mod.bump(None, down) == 2
    assert path.read_text() == before


def test_the_committed_pin_is_well_formed():
    mod = _load()
    saved = json.loads(mod.PIN_FILE.read_text())
    assert saved["repo"] == "tddworks/baguette" and saved["path"] == "Injected/HingeControl"
    assert len(saved["commit"]) == 40 and int(saved["commit"], 16) >= 0
