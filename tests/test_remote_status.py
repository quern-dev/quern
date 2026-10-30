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
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.24.3"},
                           "/api/v1/device/list": DEVICES})
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
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": urllib.error.HTTPError("u", 503, "x", {}, None)})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] is True and out["devices"] is None
    assert "devices could not be listed" in out["error"]


def test_a_broken_record_is_listed_and_the_rest_still_answer(monkeypatch):
    records = {"studio": {"url": "https://studio.lan:9130", "fingerprint": FP, "key": "k"},
               "broken": {"url": "http://plain.lan", "fingerprint": FP, "key": "k"}}
    monkeypatch.setattr(client, "_read", lambda: records)
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": DEVICES})
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
    monkeypatch.setattr(status, "statuses", lambda version, **kw: rows)


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
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": {"devices": []}})
    out = status.probe(_remote(), "0.25.0")
    assert out["devices"] == [] and out["free"] == 0 and out["error"] is None


# -- review: answers that are not quern's, and remotes that never finish --------


@pytest.mark.parametrize("listing", [
    None, {"error": "x"}, [1, 2], {"devices": "ab"}, {"devices": [1]},
    {"devices": [{"udid": "A", "reservation": "held"}]},
])
def test_a_device_list_that_is_not_one_is_not_none(monkeypatch, listing):
    """A 200 with the wrong shape read as "no devices" and exited 0, or
    crashed the whole list (review)."""
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": listing})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] and out["devices"] is None and out["free"] is None
    assert "not quern's" in out["error"]


@pytest.mark.parametrize("health", [None, [], {}, {"status": "ok"}, {"version": "1"},
                                    {"status": "error", "version": "1"}])
def test_a_health_answer_that_is_not_querns_is_not_up(monkeypatch, health):
    _answers(monkeypatch, {"/health": health, "/api/v1/device/list": DEVICES})
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] is False and "not quern's" in out["error"]


def test_a_body_that_is_not_json_says_so(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>proxy error</html>"

    class Opener:
        def open(self, req, timeout):
            return Resp()

    monkeypatch.setattr(client, "opener", lambda remote: Opener())
    out = status.probe(_remote(), "0.25.0")
    assert out["reachable"] is False and "not JSON" in out["error"]


def test_one_remotes_odd_answer_does_not_cost_the_others(monkeypatch):
    records = {"good": {"url": "https://good.lan:9130", "fingerprint": FP, "key": "k"},
               "odd": {"url": "https://odd.lan:9130", "fingerprint": FP, "key": "k"}}
    monkeypatch.setattr(client, "_read", lambda: records)
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           ("good", "/api/v1/device/list"): DEVICES,
                           ("odd", "/api/v1/device/list"): [1, 2]})
    rows = {r["name"]: r for r in status.statuses("0.25.0")}
    assert rows["good"]["free"] == 2 and rows["odd"]["devices"] is None


def test_a_remote_that_never_finishes_is_reported_at_the_deadline(monkeypatch):
    """A body dripping a byte a second beats every per-read timeout; the list
    stops waiting at its deadline (review)."""
    import threading
    import time as time_mod

    records = {"slow": {"url": "https://slow.lan:9130", "fingerprint": FP, "key": "k"},
               "quick": {"url": "https://quick.lan:9130", "fingerprint": FP, "key": "k"}}
    monkeypatch.setattr(client, "_read", lambda: records)
    release = threading.Event()
    real_probe = status.probe

    def probe(remote, version, timeout):
        if remote.name == "slow":
            release.wait(10)                       # stuck mid-answer
        return real_probe(remote, version, timeout)

    monkeypatch.setattr(status, "probe", probe)
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": DEVICES})
    started = time_mod.monotonic()
    try:
        rows = {r["name"]: r for r in status.statuses("0.25.0", deadline=0.3)}
    finally:
        release.set()
    assert time_mod.monotonic() - started < 2
    assert rows["quick"]["free"] == 2
    assert rows["slow"]["reachable"] is False and "not answering" in rows["slow"]["error"]


def test_up_but_devices_not_listed_exits_1(monkeypatch, capsys):
    """Asked, and could not list: not "all answered"."""
    _rows(monkeypatch, [{**UP, "devices": None, "free": None,
                         "error": "devices could not be listed: HTTP 503"}])
    assert remote_cli._remote(["list"]) == 1
    assert "devices could not be listed" in capsys.readouterr().out


def test_a_403_is_not_called_a_revocation(monkeypatch):
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": urllib.error.HTTPError("u", 403, "x", {}, None)})
    err = status.probe(_remote(), "0.25.0")["error"]
    assert "HTTP 403" in err and "revoked" not in err



def test_a_device_held_by_another_session_of_this_key_is_yours(monkeypatch):
    """The server's `yours` means this session; this read names its own, so a
    device the key holds through `quern session run` read as someone else's
    (found live). The key's label prefixes its session ids."""
    listing = {"devices": [
        {"udid": "A", "state": "booted",
         "reservation": {"held_by": "scorpius:status-check", "yours": False}},
        {"udid": "B", "state": "booted",
         "reservation": {"held_by": "other:job", "yours": False}},
        {"udid": "C", "state": "booted",
         "reservation": {"held_by": "scorpiusx:job", "yours": False}},   # not a prefix match
    ]}
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": listing})
    out = status.probe(_remote(), "0.25.0")          # paired as label "scorpius"
    assert [d["udid"] for d in out["devices"] if d["yours"]] == ["A"]



def test_each_device_carries_what_the_remote_says_about_it(monkeypatch):
    listing = {"devices": [
        {"udid": "A", "name": "iPhone 15 Pro", "state": "booted", "device_type": "simulator",
         "os_version": "iOS 17.5", "device_family": "iPhone", "is_available": True,
         "reservation": None},
        {"udid": "B", "name": "Studio iPad", "state": "shutdown", "device_type": "simulator",
         "os_version": "iOS 26.1", "device_family": "iPad", "model": "iPad Pro 13-inch (M5)",
         "reservation": {"held_by": "other:ci", "yours": False}},
    ]}
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": listing})
    a, b = status.probe(_remote(), "0.25.0")["devices"]
    assert (a["os_version"], a["device_family"], a["model"], a["held_by"]) == (
        "iOS 17.5", "iPhone", None, None)
    assert (b["model"], b["held_by"]) == ("iPad Pro 13-inch (M5)", "other:ci")


def test_devices_lists_each_one_free_and_booted_first(monkeypatch, capsys):
    row = {**UP, "devices": [
        {"udid": "H", "name": "iPhone SE", "device_family": "iPhone", "os_version": "iOS 18.0",
         "device_type": "simulator", "state": "booted", "free": False, "yours": False,
         "held_by": "other:ci"},
        {"udid": "S", "name": "iPhone 16", "device_family": "iPhone", "os_version": "iOS 18.0",
         "device_type": "simulator", "state": "shutdown", "free": True, "yours": False},
        {"udid": "B", "name": "iPhone 15 Pro", "device_family": "iPhone",
         "os_version": "iOS 17.5", "device_type": "simulator", "state": "booted",
         "free": True, "yours": False},
        {"udid": "Y", "name": "iPad", "device_family": "iPad", "os_version": "iOS 26.1",
         "device_type": "simulator", "state": "booted", "free": False, "yours": True,
         "model": "iPad Pro 13-inch (M5)"},
    ]}
    _rows(monkeypatch, [row])
    remote_cli._remote(["list", "--devices"])
    lines = capsys.readouterr().out.splitlines()[1:]
    assert [line.split()[-1] for line in lines] == ["B", "S", "Y", "H"]
    assert "iOS 17.5" in lines[0] and "iPhone" in lines[0] and "free" in lines[0]
    assert "iPad (iPad Pro 13-inch (M5))" in lines[2] and "yours" in lines[2]
    assert "held by other:ci" in lines[3]


def test_without_devices_the_list_stays_one_line_each(monkeypatch, capsys):
    _rows(monkeypatch, [UP])
    remote_cli._remote(["list"])
    assert len(capsys.readouterr().out.splitlines()) == 1


# -- this Mac, listed first -------------------------------------------------------


def test_this_mac_is_listed_first_when_asked(monkeypatch):
    records = {"studio": {"url": "https://studio.lan:9130", "fingerprint": FP, "key": "k"}}
    monkeypatch.setattr(client, "_read", lambda: records)
    monkeypatch.setattr(status, "_local", lambda: client.Remote(
        name=status.LOCAL, url="http://127.0.0.1:9110", fingerprint="", key="owner",
        label="this Mac"))
    _answers(monkeypatch, {"/health": {"status": "ok", "version": "0.25.0"},
                           "/api/v1/device/list": DEVICES})
    rows = status.statuses("0.25.0", include_local=True)
    assert [(r["name"], r["local"]) for r in rows] == [("local", True), ("studio", False)]
    assert status.statuses("0.25.0")[0]["name"] == "studio"         # only when asked


def test_this_macs_server_down_is_said_and_does_not_fail_the_list(monkeypatch, capsys):
    local = {**status._blank("local", None, "this Mac"), "local": True,
             "error": "not running here (quern start)"}
    _rows(monkeypatch, [local, UP])
    assert remote_cli._remote(["list", "--with-local"]) == 0
    assert "not running here" in capsys.readouterr().out


def test_this_mac_talks_plain_http_on_loopback_with_no_pin(monkeypatch):
    used = {}

    class Opener:
        def open(self, req, timeout):
            used["url"] = req.full_url
            raise urllib.error.URLError("stop")

    monkeypatch.setattr(status.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(client, "opener", lambda r: (_ for _ in ()).throw(AssertionError("pinned")))
    status.probe(client.Remote(name="local", url="http://127.0.0.1:9110", fingerprint="",
                               key="owner", label="this Mac"), None)
    assert used["url"] == "http://127.0.0.1:9110/health"


def test_an_unknown_local_version_is_not_compared(monkeypatch, capsys):
    # get_version's "unknown" is not a version: compared, every remote read as
    # differing from this Mac (CodeRabbit). JSON still says what it returned.
    import server

    seen = []
    monkeypatch.setattr(server, "get_version", lambda: "unknown")
    monkeypatch.setattr(status, "statuses",
                        lambda version, **kw: seen.append(version) or [])
    remote_cli._remote(["list", "--json"])
    assert seen == [None]
    assert json.loads(capsys.readouterr().out)["local_version"] == "unknown"
