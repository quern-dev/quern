"""Who asked, and why that must not decide what is possible.

Two questions get tangled whenever a command needs a terminal:

* *Can I prompt?* — a capability, answered by looking for a terminal.
* *Who called?* — an identity, which only the caller knows.

Using identity where capability is meant is wrong in both directions, and these
tests pin that separation rather than trusting the comment that states it.
"""

from __future__ import annotations

import pytest

from server.lifecycle.invocation import INVOKED_BY, MENUBAR, invoked_by, run_it_yourself

COMMAND = ["sudo", "pipx", "upgrade", "--global", "pymobiledevice3"]


@pytest.fixture(autouse=True)
def _no_inherited_identity(monkeypatch):
    """The developer's own shell must not decide these."""
    monkeypatch.delenv(INVOKED_BY, raising=False)


def test_a_caller_that_says_nothing_is_unknown():
    assert invoked_by() is None


def test_an_empty_value_is_not_an_identity():
    """`QUERN_INVOKED_BY=` exported but unset reads as nobody, not as a caller
    named the empty string."""
    import os

    os.environ[INVOKED_BY] = "   "
    try:
        assert invoked_by() is None
    finally:
        del os.environ[INVOKED_BY]


class TestTheAdviceSuitsTheCaller:
    def test_the_menu_bar_is_told_where_to_go(self, monkeypatch):
        """"There is no terminal to ask on" is true and useless to someone who
        clicked a menu item. They need the next step, not the diagnosis."""
        monkeypatch.setenv(INVOKED_BY, MENUBAR)

        lines = run_it_yourself(COMMAND)

        assert "Open a terminal" in lines[0]
        assert " ".join(COMMAND) in "\n".join(lines)

    def test_an_unidentified_caller_gets_the_bare_command(self):
        """Most likely a script or CI. Advice about opening terminals it may
        not have is noise in a log."""
        lines = run_it_yourself(COMMAND)

        assert lines == [" ".join(COMMAND)]

    def test_an_unrecognised_caller_is_not_treated_as_the_menu_bar(self, monkeypatch):
        monkeypatch.setenv(INVOKED_BY, "something-else")

        lines = run_it_yourself(COMMAND)

        assert "Open a terminal" not in "\n".join(lines)
        assert " ".join(COMMAND) in "\n".join(lines)

    def test_every_caller_is_told_the_command(self, monkeypatch):
        """Whatever the wording, the command has to survive it -- that is the
        only part the reader cannot reconstruct."""
        for who in (None, MENUBAR, "ci"):
            monkeypatch.delenv(INVOKED_BY, raising=False)
            if who:
                monkeypatch.setenv(INVOKED_BY, who)
            assert " ".join(COMMAND) in "\n".join(run_it_yourself(COMMAND))


def test_identity_does_not_decide_whether_sudo_is_attempted(monkeypatch):
    """The separation, asserted rather than described.

    A caller identifying itself as the menu bar changes the wording and nothing
    else; whether the command runs is decided by looking for a terminal. Were
    identity to leak into that decision, a script that forgot to identify
    itself would hang on a password prompt.
    """
    from server.lifecycle import updater

    monkeypatch.setenv(INVOKED_BY, MENUBAR)
    monkeypatch.setattr(updater.sys.stdin, "isatty", lambda: True)

    assert updater._can_ask_for_a_password() is True
