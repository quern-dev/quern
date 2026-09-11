"""Tests for the parent side of `quern start` — what it reports and what it exits with."""

from __future__ import annotations

import itertools
from unittest.mock import patch

import pytest

from server.lifecycle.daemon import _parent_wait_and_exit


def _clock():
    """A monotonic clock that advances 0.1s per read, so the 30s deadline is
    reached in a bounded number of iterations instead of real time."""
    return itertools.count(0.0, 0.1).__next__


class TestParentWaitAndExit:
    def test_exits_zero_once_the_server_is_healthy(self, capsys):
        with (
            patch("server.lifecycle.daemon.time.sleep"),
            patch("server.lifecycle.daemon.time.monotonic", side_effect=_clock()),
            patch("server.lifecycle.daemon.os.waitpid", return_value=(0, 0)),
            patch("server.lifecycle.daemon.is_server_healthy", return_value=True),
            patch(
                "server.lifecycle.daemon.read_state",
                return_value={"pid": 42, "server_port": 9100},
            ),
        ):
            with pytest.raises(SystemExit) as exc:
                _parent_wait_and_exit(42, 9100)
        assert exc.value.code == 0

    def test_a_health_check_that_never_passes_exits_nonzero(self, capsys):
        """The whole point. A printed warning followed by exit 0 tells every
        caller the opposite of what happened, and the menu-bar app gates its
        error reporting on exactly this status."""
        with (
            patch("server.lifecycle.daemon.time.sleep"),
            patch("server.lifecycle.daemon.time.monotonic", side_effect=_clock()),
            patch("server.lifecycle.daemon.os.waitpid", return_value=(0, 0)),
            patch("server.lifecycle.daemon.is_server_healthy", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc:
                _parent_wait_and_exit(42, 9100)

        assert exc.value.code != 0, "a timed-out health check must not report success"
        err = capsys.readouterr().err
        assert "health check timed out" in err

    def test_the_child_is_left_running_after_a_timeout(self):
        """It may still be coming up, and killing it would discard the logs
        that say why. The exit code means "did not finish", not "cleaned up"."""
        with (
            patch("server.lifecycle.daemon.time.sleep"),
            patch("server.lifecycle.daemon.time.monotonic", side_effect=_clock()),
            patch("server.lifecycle.daemon.os.waitpid", return_value=(0, 0)),
            patch("server.lifecycle.daemon.is_server_healthy", return_value=False),
            patch("server.lifecycle.daemon.os.kill") as kill,
        ):
            with pytest.raises(SystemExit):
                _parent_wait_and_exit(42, 9100)
        kill.assert_not_called()

    def test_a_child_that_dies_is_distinguished_from_one_that_is_slow(self, capsys):
        with (
            patch("server.lifecycle.daemon.time.sleep"),
            patch("server.lifecycle.daemon.time.monotonic", side_effect=_clock()),
            patch("server.lifecycle.daemon.os.waitpid", side_effect=ChildProcessError()),
            patch("server.lifecycle.daemon.is_server_healthy", return_value=False),
        ):
            with pytest.raises(SystemExit) as exc:
                _parent_wait_and_exit(42, 9100)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "exited unexpectedly" in err
        assert "health check timed out" not in err

    def test_healthy_but_unreadable_state_names_its_own_fault(self, capsys):
        """A server answering /health with no state file is not the same failure
        as one that never answered, and the message must not say it is: every
        consumer finds the server through that file, so "health check timed out"
        sends the reader looking for a server that is in fact running."""
        with (
            patch("server.lifecycle.daemon.time.sleep"),
            patch("server.lifecycle.daemon.time.monotonic", side_effect=_clock()),
            patch("server.lifecycle.daemon.os.waitpid", return_value=(0, 0)),
            patch("server.lifecycle.daemon.is_server_healthy", return_value=True),
            patch("server.lifecycle.daemon.read_state", return_value=None),
        ):
            with pytest.raises(SystemExit) as exc:
                _parent_wait_and_exit(42, 9100)

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "wrote no state file" in err
        assert "health check timed out" not in err
