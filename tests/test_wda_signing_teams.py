"""Signing teams when Xcode's account list is empty (#480).

Xcode 27 left IDEProvisioningTeamByIdentifier empty on a machine signed in and
able to sign WDA, so setup_wda told its user to sign in. The installed
provisioning profiles say which teams can sign an iOS development build.
Every path here is a tmp_path: never this machine's own profiles.
"""

from __future__ import annotations

import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server.device.ios import wda

NOW = datetime(2026, 10, 19, tzinfo=UTC)


def _prefs(tmp_path: Path, **values) -> Path:
    path = tmp_path / "com.apple.dt.Xcode.plist"
    path.write_bytes(plistlib.dumps(values))
    return path


def _profile(d: Path, name: str, *, team="TEAMA", team_name="Team A",
             platform=("iOS",), expires=NOW + timedelta(days=90), dev=True,
             suffix=".mobileprovision") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    body = plistlib.dumps({"Name": name, "TeamIdentifier": [team], "TeamName": team_name,
                           "Platform": list(platform),
                           "ExpirationDate": expires.replace(tzinfo=None),
                           "Entitlements": {"get-task-allow": dev}})
    path = d / f"{name}{suffix}"
    # As a real one is: CMS bytes around the plist.
    path.write_bytes(b"0\x82\x13\x8a\x06\x09*\x86H\x86\xf7\r\x01\x07\x02" + body + b"\x00\xa0\x82")
    return path


def test_xcodes_account_list_is_used_when_it_has_teams(tmp_path):
    prefs = _prefs(tmp_path, IDEProvisioningTeamByIdentifier={
        "acct": [{"teamID": "LISTED", "teamName": "Listed", "teamType": "Company"}]})
    _profile(tmp_path / "profiles", "p", team="FROMPROFILE")
    ids = wda.discover_signing_identities(prefs, (tmp_path / "profiles",), NOW)
    assert [i["team_id"] for i in ids] == ["LISTED"]


def test_an_empty_account_list_falls_back_to_profiles(tmp_path):
    """The Xcode 27 case, as measured: the list empty, the profiles there."""
    prefs = _prefs(tmp_path, IDEProvisioningTeamByIdentifier={},
                   DVTDeveloperAccountManagerAppleIDLists={"IDE.Prod": []})
    _profile(tmp_path / "profiles", "iOS Team Provisioning Profile- *", team="3QUH73KW5Q",
             team_name="Jerimiah Ham")
    ids = wda.discover_signing_identities(prefs, (tmp_path / "profiles",), NOW)
    assert len(ids) == 1
    assert ids[0]["team_id"] == "3QUH73KW5Q" and ids[0]["team_name"] == "Jerimiah Ham"
    assert ids[0]["source"] == "provisioning profile"


def test_expired_mac_only_and_distribution_profiles_do_not_count(tmp_path):
    prefs = _prefs(tmp_path)
    d = tmp_path / "profiles"
    _profile(d, "expired", team="OLD", expires=NOW - timedelta(days=1))
    _profile(d, "mac", team="MAC", platform=("OSX",), suffix=".provisionprofile")
    _profile(d, "store", team="DIST", dev=False)
    (d / "garbage.mobileprovision").write_bytes(b"not a profile")
    assert wda.discover_signing_identities(prefs, (d,), NOW) == []


def test_profiles_are_read_from_both_places_and_teams_counted_once(tmp_path):
    prefs = _prefs(tmp_path)
    a, b = tmp_path / "new-place", tmp_path / "old-place"
    _profile(a, "one", team="TEAMA")
    _profile(a, "two", team="TEAMA")
    _profile(b, "three", team="TEAMB", team_name="Team B")
    ids = wda.discover_signing_identities(prefs, (a, b), NOW)
    assert sorted(i["team_id"] for i in ids) == ["TEAMA", "TEAMB"]


def test_xcodes_last_choice_decides_between_teams_and_only_between_them(tmp_path):
    d = tmp_path / "profiles"
    _profile(d, "a", team="TEAMA")
    _profile(d, "b", team="TEAMB")
    prefs = _prefs(tmp_path, IDEProvisioningTeamManagerLastSelectedTeamID="TEAMB")
    ids = wda.discover_signing_identities(prefs, (d,), NOW)
    assert ids[0]["team_id"] == "TEAMB" and ids[0].get("last_selected") is True
    # A last choice no profile vouches for is not a team.
    prefs = _prefs(tmp_path, IDEProvisioningTeamManagerLastSelectedTeamID="GHOST")
    ids = wda.discover_signing_identities(prefs, (d,), NOW)
    assert "GHOST" not in [i["team_id"] for i in ids]
    assert not any(i.get("last_selected") for i in ids)


def test_no_prefs_at_all_still_reads_the_profiles(tmp_path):
    _profile(tmp_path / "profiles", "p", team="TEAMA")
    ids = wda.discover_signing_identities(tmp_path / "missing.plist",
                                          (tmp_path / "profiles",), NOW)
    assert [i["team_id"] for i in ids] == ["TEAMA"]


async def test_setup_wda_takes_the_last_selected_team_without_asking(monkeypatch):
    """Past choosing, it fetches the source: made to fail here, so a raise
    proves it chose -- asking returns needs_identity_selection instead."""
    import pytest

    monkeypatch.setattr(wda, "discover_signing_identities", lambda: [
        {"team_id": "TEAMB", "team_name": "B", "team_type": "", "last_selected": True},
        {"team_id": "TEAMA", "team_name": "A", "team_type": ""}])
    monkeypatch.setattr(wda, "read_wda_state", lambda: {})

    async def stop_at_the_source():
        raise RuntimeError("reached the source fetch")

    monkeypatch.setattr(wda, "clone_wda", stop_at_the_source)
    with pytest.raises(RuntimeError, match="reached the source fetch"):
        await wda.setup_wda("UDID", "18.0")


async def test_setup_wda_still_asks_when_nothing_decides(monkeypatch):
    monkeypatch.setattr(wda, "discover_signing_identities", lambda: [
        {"team_id": "TEAMA", "team_name": "A", "team_type": ""},
        {"team_id": "TEAMB", "team_name": "B", "team_type": ""}])
    monkeypatch.setattr(wda, "read_wda_state", lambda: {})
    result = await wda.setup_wda("UDID", "18.0")
    assert result["status"] == "needs_identity_selection"


def test_a_malformed_profile_is_skipped_not_fatal(tmp_path):
    """Mismatched tags raise expat's error, not a ValueError (review)."""
    d = tmp_path / "profiles"
    d.mkdir()
    mismatched = b"<?xml version='1.0'?><plist><dict><key>a</dict></plist>"
    (d / "bad.mobileprovision").write_bytes(mismatched)
    _profile(d, "good", team="TEAMA")
    weird = plistlib.dumps({"TeamIdentifier": "TEAMB", "Platform": ["iOS"],
                            "Entitlements": "not a dict"})
    (d / "weird.mobileprovision").write_bytes(weird)
    ids = wda.discover_signing_identities(_prefs(tmp_path), (d,), NOW)
    assert [i["team_id"] for i in ids] == ["TEAMA"], "one team per character, before"


def test_a_free_teams_week_long_profile_marks_it_free(tmp_path):
    """The free-account warnings -- weekly re-sign, 3 apps -- need saying."""
    d = tmp_path / "profiles"
    d.mkdir()
    body = plistlib.dumps({"TeamIdentifier": ["FREE1"], "TeamName": "Me (Personal Team)",
                           "Platform": ["iOS"], "Entitlements": {"get-task-allow": True},
                           "CreationDate": (NOW - timedelta(days=1)).replace(tzinfo=None),
                           "ExpirationDate": (NOW + timedelta(days=6)).replace(tzinfo=None)})
    (d / "free.mobileprovision").write_bytes(body)
    _profile(d, "paid", team="PAID1")
    ids = {i["team_id"]: i for i in wda.discover_signing_identities(_prefs(tmp_path), (d,), NOW)}
    assert ids["FREE1"]["team_type"] == "Free" and wda._is_free_account("Free")
    assert ids["PAID1"]["team_type"] == ""
