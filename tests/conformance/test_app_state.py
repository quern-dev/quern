"""App state checkpoints and plist editing, driven against QuernProbe's State tab.

The State tab is the probe app's one persistent surface: three UserDefaults
keys of three types, each mirrored in a label. That gives every test here two
witnesses -- the preferences file, read through the API, and what the app
shows -- and they are not interchangeable. The app reads its preferences
through cfprefsd, which caches them, so "the file says X" and "the app sees X"
are separate claims, and only the second is what anyone calling these tools
actually wants.

Simulator only, by design: on Android every operation here refuses with a 400
pointing at #314, and the last section asserts exactly that.

Checkpoints land in the server's own state directory, so each test labels its
checkpoints uniquely and deletes only those -- the same by-difference cleanup
`mock_sandbox` uses, so a developer's own checkpoints for this bundle survive a
run.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import pytest

from tests.conformance.probe import Ids

STATE = "/api/v1/device/app/state"

GREETING = "probe.greeting"
COUNTER = "probe.counter"
FLAG = "probe.flag"


def _unique_label() -> str:
    return f"conformance-{uuid.uuid4().hex[:12]}"


class StateHarness:
    """The plist API and the app's own readout of the same three keys."""

    def __init__(self, client, probe):
        self.client = client
        self.probe = probe
        self.udid = probe.udid
        self.bundle_id = probe.bundle_id
        self.plist_path = f"Library/Preferences/{probe.bundle_id}.plist"
        self.checkpoints: list[str] = []

    # -- the file --------------------------------------------------------

    def _where(self) -> dict:
        return {
            "udid": self.udid, "bundle_id": self.bundle_id,
            "container": "data", "plist_path": self.plist_path,
        }

    def read(self, key: str | None = None):
        params = self._where()
        if key is not None:
            params["key"] = key
        return self.client.get(f"{STATE}/plist", params=params, timeout=60.0)

    def plist(self) -> dict:
        resp = self.read()
        assert resp.status_code == 200, (
            f"reading {self.plist_path} -> {resp.status_code}: {resp.text[:300]}"
        )
        return resp.json()["data"]

    def set(self, key: str, value):
        return self.client.json_ok(
            "POST", f"{STATE}/plist",
            json={**self._where(), "key": key, "value": value}, timeout=60.0,
        )

    def wait_for_file(self, predicate, what: str, timeout_s: float = 15.0) -> dict:
        """Poll the file until `predicate(data)` holds.

        cfprefsd writes the app's changes to disk on its own schedule, not when
        `UserDefaults.set` returns, so a read straight after a tap is a race.
        """
        deadline = time.monotonic() + timeout_s
        data: dict = {}
        while time.monotonic() < deadline:
            resp = self.read()
            if resp.status_code == 200:
                data = resp.json()["data"]
                if predicate(data):
                    return data
            time.sleep(0.5)
        raise AssertionError(
            f"{self.plist_path} never showed {what} within {timeout_s:.0f}s; "
            f"last read: {data}"
        )

    # -- the app ---------------------------------------------------------

    def terminate(self) -> None:
        self.client.post(
            "/api/v1/device/app/terminate",
            json={"udid": self.udid, "bundle_id": self.bundle_id}, timeout=90.0,
        )

    def launch(self) -> None:
        self.client.json_ok(
            "POST", "/api/v1/device/app/launch",
            json={"udid": self.udid, "bundle_id": self.bundle_id}, timeout=180.0,
        )
        self.probe.wait_until_ready()
        self.probe.goto("state")

    def tap(self, logical: str) -> None:
        self.probe.tap(self.probe.contract.id_for(logical))

    def shown(self, logical: str) -> str | None:
        """What the app displays for a key, re-read from UserDefaults first."""
        self.tap(Ids.STATE_RELOAD)
        return self.probe.text_of(self.probe.contract.id_for(logical))

    # -- checkpoints -----------------------------------------------------

    def save(self, label: str | None = None) -> str:
        label = label or _unique_label()
        self.checkpoints.append(label)
        self.client.json_ok(
            "POST", f"{STATE}/save",
            json={"udid": self.udid, "bundle_id": self.bundle_id, "label": label},
            timeout=120.0,
        )
        return label

    def labels(self) -> set[str]:
        body = self.client.json_ok(
            "GET", f"{STATE}/list", params={"bundle_id": self.bundle_id},
            timeout=30.0,
        )
        return {s.get("label") for s in body.get("states") or []}


@pytest.fixture
def ios_state(quern, ios_probe):
    """The State tab with a known starting point: only `probe.counter = 1`.

    Simulator only: a physical iPhone's containers are not on this Mac, and
    the tools refuse there -- `test_a_physical_iphone_refuses_clearly` checks
    how.

    Set up through the app rather than the API under test, for the reason
    `ProbeDriver.relaunch` gives: a setup step that uses the tool being tested
    turns one defect into a failure in every test. The increment is what makes
    the preferences file exist at all -- a fresh install has none, and every
    plist tool answers 404 for a file that is not there.
    """
    if ios_probe.physical:
        pytest.skip("app-state tools work on simulators only")
    harness = StateHarness(quern, ios_probe)
    ios_probe.relaunch()
    ios_probe.goto("state")
    harness.tap(Ids.STATE_RESET)
    harness.tap(Ids.STATE_INCREMENT)
    try:
        harness.wait_for_file(
            lambda d: d.get(COUNTER) == 1 and GREETING not in d and FLAG not in d,
            f"{{{COUNTER}: 1}} alone",
        )
    except AssertionError as exc:
        # Seen about once per full run while reads restarted cfprefsd under
        # the running app; they now go through it instead. If it recurs, the
        # first thing to know is whether the write was late or lost.
        shown = harness.shown(Ids.STATE_COUNTER)
        ios_probe.relaunch()
        ios_probe.goto("state")
        persisted = harness.shown(Ids.STATE_COUNTER)
        raise AssertionError(
            f"{exc} -- the app showed {shown!r}; after a relaunch it shows "
            f"{persisted!r} and the file holds {harness.plist()}"
        ) from exc
    yield harness

    for label in harness.checkpoints:
        quern.delete(
            f"{STATE}/{label}", params={"bundle_id": harness.bundle_id},
            timeout=30.0,
        )
    # Leave the app's preferences empty, so the rest of the suite sees the
    # stateless app it was written against.
    ios_probe.relaunch()
    ios_probe.goto("state")
    harness.tap(Ids.STATE_RESET)


# -- reading -----------------------------------------------------------------


def test_the_apps_own_write_reaches_the_plist(ios_state) -> None:
    """The fixture already proved this once; assert the typed value directly.

    An integer must come back as an integer. A read that stringified it would
    still "contain 1" in a loose comparison.
    """
    resp = ios_state.read(COUNTER)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["key"] == COUNTER
    assert body["value"] == 1 and type(body["value"]) is int, (
        f"{COUNTER} read back as {body['value']!r} ({type(body['value']).__name__})"
    )


def test_a_read_sees_the_apps_write_without_waiting(ios_state) -> None:
    """cfprefsd writes to disk on its own schedule -- measured 3s to over 15s
    behind the app. The read goes through cfprefsd, so it answers with what
    the app has, not with what the daemon has got round to saving."""
    ios_state.launch()
    for _ in range(3):
        ios_state.tap(Ids.STATE_INCREMENT)
    # Asked of the app, not assumed: a tap that never landed reads exactly
    # like a flush that never happened.
    shown = ios_state.probe.text_of(ios_state.probe.contract.id_for(Ids.STATE_COUNTER))
    assert shown == "counter: 4", f"the taps did not all land; the app shows {shown!r}"
    resp = ios_state.read(COUNTER)
    assert resp.status_code == 200, resp.text[:300]
    if resp.json()["value"] != 4:
        # Seen 2 runs in about 9 while reads restarted cfprefsd; they now go
        # through it. If it recurs, say whether the write was late or lost.
        later = []
        for _ in range(4):
            time.sleep(1.5)
            later.append(ios_state.read(COUNTER).json().get("value"))
        ios_state.terminate()
        final = ios_state.read(COUNTER).json().get("value")
        raise AssertionError(
            f"the app shows {shown!r} and the read returned {resp.json()['value']!r}; "
            f"reads every 1.5s after: {later}; after terminating the app: {final!r}"
        )


def test_a_read_does_not_disturb_the_running_app(ios_state) -> None:
    """A read under a running app must not cost the app its next write --
    the first fix restarted cfprefsd here, and intermittently did."""
    ios_state.launch()
    assert ios_state.read(COUNTER).status_code == 200
    ios_state.tap(Ids.STATE_INCREMENT)
    assert ios_state.read(COUNTER).json()["value"] == 2
    ios_state.launch()
    assert ios_state.shown(Ids.STATE_COUNTER) == "counter: 2"


def test_a_missing_key_is_404(ios_state) -> None:
    resp = ios_state.read("probe.no_such_key")
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


def test_a_missing_plist_is_404(quern, ios_state) -> None:
    resp = quern.get(
        f"{STATE}/plist",
        params={**ios_state._where(), "plist_path": "Library/Preferences/none.plist"},
        timeout=60.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


@pytest.mark.parametrize("plist_path", [
    "../../../../../../conformance-no-such-file.plist",
    "/tmp/conformance-no-such-file.plist",
])
def test_a_plist_path_outside_the_container_is_400(
    quern, ios_state, plist_path
) -> None:
    """Probed with a path that does not exist, so a server that has the bug
    answers 404 rather than reading or writing anything."""
    resp = quern.get(
        f"{STATE}/plist", params={**ios_state._where(), "plist_path": plist_path},
        timeout=60.0,
    )
    assert resp.status_code == 400, f"{resp.status_code}: {resp.text[:300]}"


def test_an_unknown_container_is_404(quern, ios_state) -> None:
    resp = quern.get(
        f"{STATE}/plist",
        params={**ios_state._where(), "container": "group.com.quern.no-such-group"},
        timeout=60.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


# -- writing -----------------------------------------------------------------


def test_values_written_while_terminated_are_what_the_app_sees(ios_state) -> None:
    """The documented use: flip a flag, launch, observe.

    One key per type, because the tool infers the plist type from the JSON
    value and each inference is a separate way to be wrong -- a bool written as
    the string "true" reads as `true` in a careless display, which is why the
    app spells out a real boolean and shows anything else verbatim.
    """
    ios_state.terminate()
    ios_state.set(GREETING, "hello conformance")
    ios_state.set(COUNTER, 42)
    ios_state.set(FLAG, True)
    ios_state.launch()

    assert ios_state.shown(Ids.STATE_GREETING) == "greeting: hello conformance"
    assert ios_state.shown(Ids.STATE_COUNTER) == "counter: 42"
    assert ios_state.shown(Ids.STATE_FLAG) == "flag: true"


def test_a_value_set_while_the_app_runs_is_seen_on_its_next_read(ios_state) -> None:
    """Written through cfprefsd, so no relaunch is needed: the app's next read
    of its defaults sees it. Writing the file directly could not do this --
    cfprefsd kept serving its cached value until the simulator rebooted."""
    ios_state.launch()
    ios_state.set(GREETING, "while running")
    assert ios_state.shown(Ids.STATE_GREETING) == "greeting: while running"
    ios_state.terminate()
    ios_state.launch()
    assert ios_state.shown(Ids.STATE_GREETING) == "greeting: while running", (
        "seen by the running app but gone after a relaunch"
    )


def test_a_batch_write_sets_every_key(ios_state) -> None:
    ios_state.terminate()
    body = ios_state.client.json_ok(
        "POST", f"{STATE}/plist/batch",
        json={**ios_state._where(),
              "values": {GREETING: "batched", COUNTER: 7, FLAG: False}},
        timeout=60.0,
    )
    assert body["status"] == "ok" and body["keys_set"] == 3, body

    data = ios_state.plist()
    assert (data.get(GREETING), data.get(COUNTER), data.get(FLAG)) == (
        "batched", 7, False,
    ), data

    ios_state.launch()
    assert ios_state.shown(Ids.STATE_FLAG) == "flag: false", (
        "a batch-written False should read as a real boolean, not absent or 0"
    )


def test_a_deleted_key_is_gone_from_the_file_and_the_app(ios_state) -> None:
    ios_state.terminate()
    ios_state.client.json_ok(
        "DELETE", f"{STATE}/plist/key",
        json={**ios_state._where(), "key": COUNTER}, timeout=60.0,
    )
    assert ios_state.read(COUNTER).status_code == 404

    ios_state.launch()
    assert ios_state.shown(Ids.STATE_COUNTER) == "counter: —", (
        "the app still sees a key that was deleted from its preferences file"
    )


# -- checkpoints -------------------------------------------------------------


def test_a_saved_checkpoint_is_listed_and_deletable(quern, ios_state) -> None:
    label = ios_state.save()
    assert label in ios_state.labels()

    quern.json_ok(
        "DELETE", f"{STATE}/{label}",
        params={"bundle_id": ios_state.bundle_id}, timeout=30.0,
    )
    ios_state.checkpoints.remove(label)
    assert label not in ios_state.labels()


def test_restore_brings_back_what_the_app_saw(ios_state) -> None:
    """Change state through the app after saving, restore, and ask the app.

    The change is made by tapping, not by the plist API, so the restore is
    tested against the way an app's state actually diverges.
    """
    label = ios_state.save()  # counter == 1
    ios_state.launch()
    for _ in range(3):
        ios_state.tap(Ids.STATE_INCREMENT)
    ios_state.wait_for_file(lambda d: d.get(COUNTER) == 4, f"{COUNTER} = 4")

    ios_state.client.json_ok(
        "POST", f"{STATE}/restore",
        json={"udid": ios_state.udid, "bundle_id": ios_state.bundle_id,
              "label": label},
        timeout=120.0,
    )
    ios_state.launch()
    assert ios_state.shown(Ids.STATE_COUNTER) == "counter: 1", (
        "restore reported success but the app does not see the saved state"
    )


def test_a_checkpoint_taken_straight_after_a_change_includes_it(ios_state) -> None:
    """Save copies the files, and the files lag the app; the save flushes
    cfprefsd first. Saved with no wait after the taps, on purpose."""
    ios_state.launch()
    for _ in range(2):
        ios_state.tap(Ids.STATE_INCREMENT)
    label = ios_state.save()  # the app shows 3
    ios_state.terminate()
    ios_state.set(COUNTER, 99)
    ios_state.client.json_ok(
        "POST", f"{STATE}/restore",
        json={"udid": ios_state.udid, "bundle_id": ios_state.bundle_id,
              "label": label},
        timeout=120.0,
    )
    ios_state.launch()
    assert ios_state.shown(Ids.STATE_COUNTER) == "counter: 3", (
        "the checkpoint holds a preferences file older than what the app showed"
    )


def test_diff_names_exactly_what_changed_since_the_checkpoint(ios_state) -> None:
    label = ios_state.save()  # {counter: 1}
    ios_state.terminate()
    ios_state.set(COUNTER, 2)
    ios_state.set(GREETING, "added")

    body = ios_state.client.json_ok(
        "GET", f"{STATE}/plist/diff",
        params={**ios_state._where(), "checkpoint_label": label}, timeout=60.0,
    )
    assert body["changed"] == {COUNTER: {"old": 1, "new": 2}}, body
    assert body["added"] == {GREETING: "added"}, body
    assert body["removed"] == {}, body


def test_restoring_an_unknown_checkpoint_is_404(quern, ios_state) -> None:
    resp = quern.post(
        f"{STATE}/restore",
        json={"udid": ios_state.udid, "bundle_id": ios_state.bundle_id,
              "label": _unique_label()},
        timeout=60.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


def test_deleting_an_unknown_checkpoint_is_404(quern, ios_state) -> None:
    resp = quern.delete(
        f"{STATE}/{_unique_label()}",
        params={"bundle_id": ios_state.bundle_id}, timeout=30.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


@pytest.mark.parametrize("bundle_id", ["../..", ".."])
def test_checkpoint_names_cannot_leave_the_checkpoint_store(
    quern, bundle_id
) -> None:
    """A bundle id that walks out of the store must be refused.

    Checkpoints live at `<state>/app-states/<bundle_id>/<label>`, and delete is
    an `rmtree` of that path -- so `bundle_id=../..` with an existing label
    deletes a directory beside `~/.quern`. Probed only with a label that cannot
    exist, so this is safe to run against a server that has the bug: a
    vulnerable server answers 404 "not found" where a fixed one refuses the
    name. A `..` *label* is deliberately not probed: it resolves to the store
    itself, which exists, so on a vulnerable server the probe would be the
    damage.
    """
    label = _unique_label()
    resp = quern.delete(
        f"{STATE}/{label}", params={"bundle_id": bundle_id}, timeout=30.0,
    )
    assert resp.status_code in (400, 422), (
        f"bundle_id={bundle_id!r} -> {resp.status_code}: {resp.text[:300]} -- "
        "a path outside the checkpoint store was accepted as a checkpoint name"
    )


# -- watching ----------------------------------------------------------------


def test_a_plist_watch_reports_the_apps_change(quern, ios_state) -> None:
    ios_state.launch()
    since = datetime.now(UTC)
    body = quern.json_ok(
        "POST", f"{STATE}/plist/watch/start",
        json={**ios_state._where(), "poll_interval": 0.5}, timeout=60.0,
    )
    assert body["status"] in ("started", "already_running"), body
    try:
        ios_state.tap(Ids.STATE_INCREMENT)
        expected = f"{COUNTER}: 1 → 2"
        deadline = time.monotonic() + 20.0
        messages: list[str] = []
        while time.monotonic() < deadline:
            logs = quern.json_ok(
                "GET", "/api/v1/logs/query",
                params={"source": "plist_watcher", "since": since.isoformat(),
                        "limit": 200},
                timeout=30.0,
            )
            messages = [e.get("message", "") for e in logs.get("entries") or []]
            if expected in messages:
                break
            time.sleep(0.5)
        assert expected in messages, (
            f"no {expected!r} entry within 20s; the watch logged {messages}"
        )
    finally:
        quern.post(
            f"{STATE}/plist/watch/stop", json=ios_state._where(), timeout=30.0,
        )


def test_stopping_a_watch_that_is_not_running_is_404(quern, ios_state) -> None:
    resp = quern.post(
        f"{STATE}/plist/watch/stop",
        json={**ios_state._where(), "plist_path": "Library/Preferences/none.plist"},
        timeout=30.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"


# -- Android refuses, clearly ------------------------------------------------

_ANDROID_REFUSALS = [
    ("GET", "/plist", "params"),
    ("POST", "/plist", "json"),
    ("POST", "/plist/batch", "json"),
    ("DELETE", "/plist/key", "json"),
    ("GET", "/plist/diff", "params"),
    ("POST", "/plist/watch/start", "json"),
    ("POST", "/save", "json"),
    ("POST", "/restore", "json"),
]


@pytest.mark.parametrize("method, path, where", _ANDROID_REFUSALS,
                         ids=[f"{m} {p}" for m, p, _ in _ANDROID_REFUSALS])
def test_android_refuses_with_a_400_naming_the_issue(
    quern, any_android, method, path, where
) -> None:
    """Every iOS-container operation refuses on Android and says where to look.

    A 500 here would read as a server fault rather than "not on this platform",
    which is what this module's error handler did until #263; and a refusal that
    did not name #314 would leave the caller nowhere to go.
    """
    payload = {
        "udid": any_android.udid, "bundle_id": "com.quern.probe",
        "container": "data", "plist_path": "shared_prefs/probe.xml",
        "key": "probe.counter", "value": 1, "values": {"probe.counter": 1},
        "label": "conformance-android", "checkpoint_label": "conformance-android",
    }
    if where == "params":
        flat = {k: v for k, v in payload.items() if isinstance(v, str)}
        resp = quern.request(method, f"{STATE}{path}", params=flat, timeout=60.0)
    else:
        resp = quern.request(method, f"{STATE}{path}", json=payload, timeout=60.0)
    assert resp.status_code == 400, f"{resp.status_code}: {resp.text[:300]}"
    assert "#314" in resp.text, f"the refusal does not name #314: {resp.text[:300]}"


@pytest.mark.parametrize("method, path, where", _ANDROID_REFUSALS,
                         ids=[f"{m} {p}" for m, p, _ in _ANDROID_REFUSALS])
def test_a_physical_iphone_refuses_clearly(quern, ios_probe, method, path, where) -> None:
    """A 400 that says simulators only, not a 500."""
    if not ios_probe.physical:
        pytest.skip("only a physical iPhone refuses these")
    payload = {
        "udid": ios_probe.udid, "bundle_id": ios_probe.bundle_id,
        "container": "data", "plist_path": "Library/Preferences/x.plist",
        "key": "k", "value": 1, "values": {"k": 1},
        "label": "conformance-device", "checkpoint_label": "conformance-device",
    }
    if where == "params":
        flat = {k: v for k, v in payload.items() if isinstance(v, str)}
        resp = quern.request(method, f"{STATE}{path}", params=flat, timeout=60.0)
    else:
        resp = quern.request(method, f"{STATE}{path}", json=payload, timeout=60.0)
    assert resp.status_code == 400, f"{resp.status_code}: {resp.text[:300]}"
    assert "simulator" in resp.text.lower(), resp.text[:300]

