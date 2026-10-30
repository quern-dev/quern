"""`quern remote list` asks each paired remote how it is, now.

The record `quern connect` keeps said nothing about whether a server was up,
what it ran, or what it could lend. Each remote is asked with its own key and
pinned certificate; one that cannot be asked says why, and is never reported
as having no devices.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from server import remote_cli
from server.remote import client, status

FP = "SHA256:" + "ab" * 32


def _remote(name="studio", **kw):
    return client.Remote(name=name, url=f"https://{name}.lan:9130", fingerprint=FP,
                         key="qc_key", label="scorpius", **kw)


def _answers(monkeypatch, by_path):
    asked = []

    def fake_get(remote, path, timeout):
        asked.append((remote.name, path))
        got = by_path[(remote.name, path)] if (remote.name, path) in by_path else by_path[path]
        if isinstance(got, BaseException):
            raise got
        return got

    monkeypatch.setattr(status, "_get", fake_get)
    return asked


DEVICES = {"devices": [
    {"udid": "A", "name": "iPhone 17", "state": "booted", "reservation": None},
    {"udid": "B", "name": "iPhone 16", "state": "shutdown", "reservation": None},
    {"udid": "C", "name": "iPad", "state": "booted",
     "reservation": {"held_by": "s", "yours": True}},
    {"udid": "D", "name": "iPhone SE", "state": "booted",
     "reservation": {"held_by": "other", "yours": False}},
]}


def test_an_answering_remote_reports_version_and_free_devices(monkeypatch):
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": DEVICES})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] and out["version"] == "0.25.0" and out["version_matches"] is True
    assert out["free"] == 2 and len(out["devices"]) == 4
    assert [d["udid"] for d in out["devices"] if d["yours"]] == ["C"]
    assert out["error"] is None


def test_a_different_version_is_said(monkeypatch):
    _answers(monkeypatch, {"/health": {"version": "0.24.3"}, "/api/v1/device/list": DEVICES})
    assert status.probe(_remote(), "0.25.0")["version_matches"] is False


@pytest.mark.parametrize("failure, words", [
    (urllib.error.URLError(ConnectionRefusedError("refused")), "not reachable"),
    (TimeoutError(), "timed out"),
    (client.PinMismatch("expected X, got Y"), "certificate changed"),
    (urllib.error.URLError(client.PinMismatch("expected X, got Y")), "certificate changed"),
    (urllib.error.HTTPError("u", 401, "no", {}, None), "refused its key"),
    (urllib.error.HTTPError("u", 500, "boom", {}, None), "HTTP 500"),
])
def test_a_remote_that_cannot_be_asked_says_why(monkeypatch, failure, words):
    _answers(monkeypatch, {"/health": failure})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] is False and words in out["error"]
    assert out["devices"] is None and out["free"] is None     # not "no devices"


def test_devices_that_cannot_be_listed_are_not_reported_as_none(monkeypatch):
    _answers(monkeypatch, {"/health": {"version": "0.25.0"},
                           "/api/v1/device/list": urllib.error.HTTPError("u", 503, "x", {}, None)})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] is True and out["devices"] is None
    assert "devices could not be listed" in out["error"]


def test_a_broken_record_is_listed_and_the_rest_still_answer(monkeypatch):
    records = {"studio": {"url": "https://studio.lan:9130", "fingerprint": FP, "key": "k"},
               "broken": {"url": "http://plain.lan", "fingerprint": FP, "key": "k"}}
    monkeypatch.setattr(client, "_read", lambda: records)
    _answers(monkeypatch, {"/health": {"version": "0.25.0"}, "/api/v1/device/list": DEVICES})
    rows = status.statuses("0.25.0")
    assert [r["name"] for r in rows] == ["broken", "studio"]
    assert rows[0]["reachable"] is False and "https://" in rows[0]["error"]
    assert rows[1]["free"] == 2


def test_an_unreadable_remotes_file_is_said(monkeypatch):
    def unreadable():
        raise client.RemoteError("remotes.json is not readable")
    monkeypatch.setattr(client, "_read", unreadable)
    assert status.statuses("0.25.0") == [{"name": None, "error": "remotes.json is not readable"}]


def test_every_request_names_a_session_and_the_pinned_key(monkeypatch):
    """A client key must name a session, even to read."""
    seen = {}

    class Opener:
        def open(self, req, timeout):
            seen["headers"] = dict(req.header_items())
            seen["timeout"] = timeout
            raise urllib.error.URLError("stop here")

    monkeypatch.setattr(client, "opener", lambda remote: Opener())
    status.probe(_remote(), None, timeout=2.5)
    assert seen["headers"]["Authorization"] == "Bearer qc_key"
    assert seen["headers"]["X-quern-session"] == "quern-remote-status"
    assert seen["timeout"] == 2.5


# -- the CLI ---------------------------------------------------------------------


def _rows(monkeypatch, rows):
    monkeypatch.setattr(status, "statuses", lambda version, timeout=None: rows)


UP = {"name": "studio", "url": "https://studio.lan:9130", "label": "scorpius",
      "reachable": True, "version": "0.25.0", "version_matches": True, "error": None,
      "devices": [{"state": "booted"}, {"state": "shutdown"}], "free": 1}
DOWN = {"name": "romulus", "url": "https://romulus.lan:9130", "label": "scorpius",
        "reachable": False, "version": None, "version_matches": None, "devices": None,
        "free": None, "error": "not reachable (timed out)"}


def test_list_says_each_remotes_state(monkeypatch, capsys):
    _rows(monkeypatch, [UP, DOWN])
    rc = remote_cli._remote(["list"])
    out = capsys.readouterr().out
    assert "2 devices (1 booted), 1 free" in out
    assert "down: not reachable (timed out)" in out
    assert rc == 1                       # one could not be asked: a script can tell


def test_all_up_exits_0(monkeypatch, capsys):
    _rows(monkeypatch, [UP])
    assert remote_cli._remote(["list"]) == 0


def test_json_is_the_whole_answer(monkeypatch, capsys):
    _rows(monkeypatch, [UP, DOWN])
    remote_cli._remote(["list", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in data["remotes"]] == ["studio", "romulus"]
    assert "local_version" in data


def test_no_check_asks_nothing(monkeypatch, capsys):
    monkeypatch.setattr(client, "list_remotes", lambda: [_remote()])

    def must_not(*a, **kw):
        raise AssertionError("asked a remote")

    monkeypatch.setattr(status, "statuses", must_not)
    assert remote_cli._remote(["list", "--no-check"]) == 0
    assert "studio" in capsys.readouterr().out


def test_an_unknown_flag_is_usage(capsys):
    assert remote_cli._remote(["list", "--bogus"]) == 2


def test_a_remote_with_no_devices_says_none_not_unknown(monkeypatch):
    """Asked and answered "none" is not "could not ask"."""
    _answers(monkeypatch, {"/health": {"version": "0.25.0"},
                           "/api/v1/device/list": {"devices": []}})
    out = status.probe(_remote(), "0.25.0")
    assert out["devices"] == [] and out["free"] == 0 and out["error"] is None
