"""How messages spell the command they are telling you to run.

`./quern doctor` has not been necessary since `quern setup` began writing
`~/.local/bin/quern`, and for a tarball install it is actively wrong -- the
reader is not necessarily standing in the extracted directory.

But it cannot simply be replaced. The same strings are printed in two different
situations. `quern doctor` runs after a successful install, where `./quern` is
awkward; most of `setup.py`'s checks run *before* `install_wrapper_script()`,
where `quern` names a command the reader does not have yet. A static choice is
wrong in one of the two, so the answer is decided when the message is built.

See #192.
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.config import quern_cmd

SERVER = Path(__file__).resolve().parent.parent / "server"

#: Files allowed to contain a literal `./quern`, and why.
#:
#: These name the wrapper *script* rather than instructing anyone to run a
#: command -- prose about how `./quern` re-execs into the venv, and the
#: provenance comment written into the generated wrapper. Replacing them would
#: make them inaccurate, not modern.
ALLOWED = {
    # The header comment baked into the *generated* wrapper, naming the command
    # that produced the file. Provenance, not an instruction.
    "lifecycle/setup.py",
    # Defines the fallback that every other site now goes through.
    "config.py",
}


class TestTheCommandIsResolvedNotAssumed:
    """Three situations, three answers. Only one tolerates a guess."""

    def test_it_is_bare_when_the_wrapper_is_on_path(self, monkeypatch):
        monkeypatch.setattr(
            "server.config.shutil.which", lambda _n: "/home/u/.local/bin/quern",
        )
        assert quern_cmd() == "quern"

    def test_it_is_relative_only_when_the_reader_is_standing_in_the_project(
        self, monkeypatch,
    ):
        monkeypatch.setattr("server.config.shutil.which", lambda _n: None)
        root = Path(__file__).resolve().parent.parent
        monkeypatch.chdir(root)
        assert quern_cmd() == "./quern"

    def test_it_is_absolute_when_the_reader_is_somewhere_else(
        self, monkeypatch, tmp_path,
    ):
        """The case that makes this more than cosmetic.

        An install script drops quern in `~/.local/share/quern` and leaves the
        terminal wherever it was. Telling that user to run `./quern setup`
        points at a file that is not in their directory -- and they are the
        least likely person to work out why.
        """
        monkeypatch.setattr("server.config.shutil.which", lambda _n: None)
        monkeypatch.chdir(tmp_path)
        answer = quern_cmd()
        assert answer.startswith("/"), f"{answer!r} is not runnable from here"
        assert answer.endswith("/quern")
        assert Path(answer).exists()

    def test_it_asks_path_rather_than_the_wrapper_file(self, monkeypatch):
        """`which`, not `WRAPPER_PATH.exists()`.

        The wrapper can exist while `~/.local/bin` is absent from PATH, which
        CONTRIBUTING documents as a live failure -- zsh caches its first
        resolution, so `type -a` and what actually runs can disagree.
        """
        asked = []
        monkeypatch.setattr(
            "server.config.shutil.which", lambda n: asked.append(n) or None,
        )
        quern_cmd()
        assert asked == ["quern"]

    def test_the_answer_is_not_frozen_for_the_process(self, monkeypatch, tmp_path):
        """It depends on PATH, on the wrapper existing, and on the directory --
        and all three move. Setup installs the wrapper partway through its own
        run; uninstall removes it; anything may chdir. A cached answer would
        need invalidating at each, which is three chances to miss one.
        """
        on_path = {"yes": False}
        monkeypatch.setattr(
            "server.config.shutil.which",
            lambda _n: "/home/u/.local/bin/quern" if on_path["yes"] else None,
        )
        monkeypatch.chdir(tmp_path)
        before = quern_cmd()
        on_path["yes"] = True
        assert before != quern_cmd(), (
            "the wrapper appeared mid-process and the advice did not change"
        )


class TestNoMessageHardcodesTheRelativeForm:
    """Without this, it reaccumulates: every new check detail is a fresh chance
    to type `./quern`, and nothing would notice."""

    def _offenders(self) -> list[str]:
        found = []
        for path in sorted(SERVER.rglob("*.py")):
            rel = str(path.relative_to(SERVER))
            if rel in ALLOWED:
                continue
            tree = ast.parse(path.read_text())
            # Docstrings are prose about the wrapper script, not advice to run
            # it, and rewriting them would make them inaccurate rather than
            # modern. Only strings that end up in a message matter here.
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = node.body
                    if body and isinstance(body[0], ast.Expr) and \
                            isinstance(body[0].value, ast.Constant) and \
                            isinstance(body[0].value.value, str):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "./quern" in node.value and id(node) not in docstrings:
                        found.append(f"{rel}:{node.lineno}")
        return found

    def test_no_string_literal_says_dot_slash_quern(self):
        offenders = self._offenders()
        assert not offenders, (
            "these build a message with a hardcoded './quern'; use "
            f"quern_cmd() so it is right in both contexts: {offenders}"
        )

    def test_the_guard_can_actually_see_one(self, tmp_path, monkeypatch):
        """A scanner that finds nothing because it looks nowhere passes too."""
        module = tmp_path / "decoy.py"
        module.write_text('msg = "run ./quern doctor"\n')
        tree = ast.parse(module.read_text())
        hits = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "./quern" in n.value
        ]
        assert hits, "the detection this guard relies on does not work"

    def test_the_allowlist_is_not_silently_over_broad(self):
        """Every allowed file must actually contain one, or the entry is stale
        and quietly exempting a file nobody meant to exempt."""
        for rel in ALLOWED:
            text = (SERVER / rel).read_text()
            assert "./quern" in text, (
                f"{rel} is allow-listed but no longer contains './quern' — "
                "drop it, or it exempts that file for nothing"
            )


