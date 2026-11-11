"""The adb helpers behind proxy configuration, driven against real device output.

These parse `dumpsys wifi`, `ip route get` and `settings get`. Every one of
them was reached only through an `AsyncMock` before, which is a way of testing
that the endpoint calls them, not that they work -- so any mutation inside
these function bodies survived. The strings below are captured from a physical
Pixel 3 XL (`8BAY0WCL7`) and an emulator, except where a format is named as
coming from another Android build.

See #265.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.android.adb import AdbBackend


@pytest.fixture
def adb():
    return AdbBackend()


def _returns(text: str):
    return AsyncMock(return_value=(text, ""))


class TestReadingTheSsid:
    """The SSID is the *storage key*, so a stray quote files one network under
    two records and matches nothing."""

    async def test_the_unquoted_form_this_pixel_emits(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "mWifiInfo SSID: MonaLisaOverdrive, BSSID: f4:92:bf:9c:63:eb, "
            "MAC: 42:e6:7d:09:b3:19, Supplicant state: COMPLETED"
        ))
        assert await adb.get_wifi_ssid("X") == "MonaLisaOverdrive"

    async def test_the_quoted_form_other_builds_emit(self, adb, monkeypatch):
        """`WifiInfo.getSSID()` wraps a valid UTF-8 SSID in double quotes."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            'mWifiInfo SSID: "MonaLisaOverdrive", BSSID: f4:92:bf:9c:63:eb, MAC: x'
        ))
        assert await adb.get_wifi_ssid("X") == "MonaLisaOverdrive"

    async def test_an_ssid_containing_a_comma_survives(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            'mWifiInfo SSID: "Hello, World", BSSID: f4:92:bf:9c:63:eb, MAC: x'
        ))
        assert await adb.get_wifi_ssid("X") == "Hello, World"

    async def test_an_unquoted_ssid_containing_a_comma_survives(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "mWifiInfo SSID: Hello, World, BSSID: f4:92:bf:9c:63:eb, MAC: x"
        ))
        assert await adb.get_wifi_ssid("X") == "Hello, World"

    async def test_unassociated_reports_none(self, adb, monkeypatch):
        """Captured verbatim from the phone with Wi-Fi up but not joined."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "mWifiInfo SSID: <unknown ssid>, BSSID: <none>, "
            "MAC: 02:00:00:00:00:00, Supplicant state: DISCONNECTED"
        ))
        assert await adb.get_wifi_ssid("X") is None

    async def test_no_wifi_section_reports_none(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns("Wi-Fi is disabled"))
        assert await adb.get_wifi_ssid("X") is None

    async def test_an_adb_failure_reports_none_rather_than_raising(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device",
                            AsyncMock(side_effect=RuntimeError("device offline")))
        assert await adb.get_wifi_ssid("X") is None


class TestReadingTheLanAddress:
    async def test_the_phone_route(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "8.8.8.8 via 192.168.1.1 dev wlan0 table 1030 src 192.168.1.244 uid 2000 \n    cache"
        ))
        assert await adb.get_lan_ip("X") == "192.168.1.244"

    async def test_the_emulator_nat_route(self, adb, monkeypatch):
        """An emulator answers with its NAT address, which is the right answer
        to the question asked: what it would use to reach off-device."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "8.8.8.8 via 10.0.2.2 dev eth0 src 10.0.2.15"
        ))
        assert await adb.get_lan_ip("X") == "10.0.2.15"

    async def test_output_truncated_at_src_does_not_raise(self, adb, monkeypatch):
        """Indexing past the last token raised IndexError straight out into a
        500 rather than reporting that it could not tell."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "8.8.8.8 via 192.168.1.1 dev wlan0 src"
        ))
        assert await adb.get_lan_ip("X") is None

    async def test_no_route_reports_none(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(
            "RTNETLINK answers: Network is unreachable"
        ))
        assert await adb.get_lan_ip("X") is None


class TestReadingTheProxy:
    async def test_a_set_proxy(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns("192.168.1.189:9177\r\n"))
        assert await adb.get_http_proxy("X") == "192.168.1.189:9177"

    async def test_the_literal_null_is_unset(self, adb, monkeypatch):
        """`settings get` prints the string `null` for an absent key."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns("null\n"))
        assert await adb.get_http_proxy("X") is None

    async def test_empty_output_is_unset(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(""))
        assert await adb.get_http_proxy("X") is None

    async def test_strict_takes_empty_output_for_a_failed_read(self, adb, monkeypatch):
        """What a clear later deletes on the strength of (#645)."""
        from server.models import DeviceError

        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(""))
        with pytest.raises(DeviceError):
            await adb.get_http_proxy("X", strict=True)

    async def test_strict_still_reads_null_as_unset(self, adb, monkeypatch):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns("null\n"))
        assert await adb.get_http_proxy("X", strict=True) is None

    async def test_a_zero_row_is_no_proxy(self, adb, monkeypatch):
        """`:0` is how a proxy is switched off (#652)."""
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(":0\n"))
        assert await adb.get_http_proxy("X") is None

    @staticmethod
    def _device(copies):
        """adb as a device whose copy of the proxy reads each of `copies` in
        turn, the last one from then on."""
        calls, reads = [], list(copies)

        async def run(serial, *args, timeout=None):
            calls.append(args)
            if args[1].startswith("echo h="):
                return (reads.pop(0) if len(reads) > 1 else reads[0]), ""
            return "", ""
        return run, calls

    async def test_clearing_writes_zero_and_then_deletes(self, adb, monkeypatch):
        """A delete alone leaves Android's copy, which the device goes on
        using (#652)."""
        run, calls = self._device(["h= p=0"])
        monkeypatch.setattr(adb, "_run_adb_for_device", run)
        await adb.clear_http_proxy("X")
        writes = [c for c in calls if not c[1].startswith("echo h=")]
        assert writes == [
            ("shell", "settings", "put", "global", "http_proxy", ":0"),
            ("shell", "settings", "delete", "global", "http_proxy"),
        ]

    async def test_the_delete_waits_for_the_copy_to_empty(self, adb, monkeypatch):
        """Android reads the row when it gets to the change: a delete that
        lands first leaves the copy (review)."""
        monkeypatch.setattr("server.device.android.adb.asyncio.sleep", AsyncMock())
        run, calls = self._device(["h=192.168.1.189 p=9101", "h=192.168.1.189 p=9101", "h= p=0"])
        monkeypatch.setattr(adb, "_run_adb_for_device", run)
        await adb.clear_http_proxy("X")
        kinds = ["read" if c[1].startswith("echo h=") else c[2] for c in calls]
        assert kinds == ["put", "read", "read", "read", "delete"]

    async def test_a_failed_delete_after_zero_is_not_raised(self, adb, monkeypatch):
        from server.models import DeviceError

        run = AsyncMock(side_effect=[("", ""), ("h= p=0", ""), DeviceError("gone", tool="adb")])
        monkeypatch.setattr(adb, "_run_adb_for_device", run)
        await adb.clear_http_proxy("X")

    @pytest.mark.parametrize("out, expected", [
        ("h=192.168.1.189 p=9101\n", (True, "192.168.1.189:9101")),
        ("h= p=0\n", (True, None)),            # emptied by `:0`
        ("h=null p=null\n", (False, None)),    # never set: no copy kept
    ])
    async def test_the_proxy_in_use(self, adb, monkeypatch, out, expected):
        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(out))
        assert await adb.get_proxy_in_use("X") == expected

    @pytest.mark.parametrize("out", ["", "h= p=\n", "error: closed\n"])
    async def test_a_proxy_in_use_read_that_says_nothing_raises(self, adb, monkeypatch, out):
        """`h= p=` is two failed reads, not an emptied copy, which prints port
        0 (review)."""
        from server.models import DeviceError

        monkeypatch.setattr(adb, "_run_adb_for_device", _returns(out))
        with pytest.raises(DeviceError):
            await adb.get_proxy_in_use("X")

    async def test_a_value_read_is_written_back_as_it_was(self, adb, monkeypatch):
        run = _returns("")
        monkeypatch.setattr(adb, "_run_adb_for_device", run)
        await adb.put_http_proxy("X", "proxy.corp:3128")
        run.assert_awaited_once_with(
            "X", "shell", "settings", "put", "global", "http_proxy", "proxy.corp:3128")


class TestTheBounceRefusesToCutItsOwnConnection:
    async def test_a_network_transport_is_not_bounced(self, adb, monkeypatch):
        """`svc wifi disable` over adb-on-Wi-Fi severs the channel carrying
        the `enable` that would undo it, leaving a possibly-remote device with
        Wi-Fi off and no way back."""
        run = AsyncMock(return_value=("", ""))
        monkeypatch.setattr(adb, "_run_adb_for_device", run)

        assert await adb.reattach_network("192.168.1.9:5555") is False
        run.assert_not_awaited()

    async def test_a_usb_device_is_bounced(self, adb, monkeypatch):
        """The positive control: without this the test above would pass on a
        function that never bounces anything."""
        calls = []

        async def fake(serial, *args):
            calls.append(args)
            if args[:2] == ("shell", "ip"):
                return ("30: wlan0    inet 192.168.1.244/24 brd ...", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is True
        assert ("shell", "svc", "wifi", "disable") in calls
        assert ("shell", "svc", "wifi", "enable") in calls

    async def test_loopback_alone_is_not_a_reattached_network(self, adb, monkeypatch):
        """`lo` always has an address, so counting any `inet` would report
        success on a device with no network at all."""
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("1: lo    inet 127.0.0.1/8 scope host lo", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        assert await adb.reattach_network("8BAY0WCL7") is False

    async def test_an_interface_that_is_not_wlan0_counts(self, adb, monkeypatch):
        """A device on `wlan1` reattached fine and was reported as failed."""
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("1: lo    inet 127.0.0.1/8 scope host lo\n"
                        "31: wlan1    inet 192.168.1.244/24 brd 192.168.1.255", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is True


class TestWirelessDebuggingSerialsCountAsNetwork:
    """Android 11+ wireless debugging found over mDNS produces a serial with no
    colon at all, so the `host:port` shape alone called it USB and cleared it
    to have its Wi-Fi turned off -- the exact failure the check exists to
    prevent."""

    def test_the_mdns_form_is_a_network_transport(self):
        from server.device.android.adb import AdbBackend

        assert AdbBackend.is_network_transport(
            "adb-8BAY0WCL7-AbCdEf._adb-tls-connect._tcp"
        ) is True
        assert AdbBackend.is_network_transport("adb-XYZ._adb._tcp") is True

    async def test_an_mdns_device_is_not_bounced(self, adb, monkeypatch):
        run = AsyncMock(return_value=("", ""))
        monkeypatch.setattr(adb, "_run_adb_for_device", run)

        assert await adb.reattach_network(
            "adb-8BAY0WCL7-AbCdEf._adb-tls-connect._tcp"
        ) is False
        run.assert_not_awaited()


class TestTheRadioIsNotLeftOff:
    """`disable` succeeding and `enable` failing left the device with no radio,
    reported as a failed reattach, with a hint telling the caller to tap a
    network on a phone whose Wi-Fi was off."""

    async def test_enable_is_retried(self, adb, monkeypatch):
        attempts = {"enable": 0}

        async def fake(serial, *args):
            if args == ("shell", "svc", "wifi", "enable"):
                attempts["enable"] += 1
                if attempts["enable"] < 3:
                    raise RuntimeError("transient adb error")
                return ("", "")
            if args[:2] == ("shell", "ip"):
                return ("30: wlan0    inet 192.168.1.244/24 brd x", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is True
        assert attempts["enable"] == 3

    async def test_enable_failing_every_time_reports_failure(self, adb, monkeypatch):
        async def fake(serial, *args):
            if args == ("shell", "svc", "wifi", "enable"):
                raise RuntimeError("adb gone")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is False

    async def test_a_failed_disable_is_ambiguous_so_enable_is_still_attempted(
        self, adb, monkeypatch,
    ):
        """A nonzero adb exit does not prove the command had no effect. If it
        disabled Wi-Fi and then failed to report back, returning here would
        leave the radio off with nothing left to turn it on."""
        calls = []

        async def fake(serial, *args):
            calls.append(args)
            if args == ("shell", "svc", "wifi", "disable"):
                raise RuntimeError("adb error after the command ran")
            if args[:2] == ("shell", "ip"):
                return ("30: wlan0    inet 192.168.1.244/24 brd x", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        result = await adb.reattach_network("8BAY0WCL7")

        assert ("shell", "svc", "wifi", "enable") in calls
        assert result is True

    async def test_a_device_left_without_an_address_still_reports_failure(
        self, adb, monkeypatch,
    ):
        """Recovery is attempted, not assumed to have worked."""
        async def fake(serial, *args):
            if args == ("shell", "svc", "wifi", "disable"):
                raise RuntimeError("adb error")
            if args[:2] == ("shell", "ip"):
                return ("1: lo    inet 127.0.0.1/8 scope host lo", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        assert await adb.reattach_network("8BAY0WCL7") is False


class TestOnlyWifiCountsAsReattached:
    """A phone that dropped off Wi-Fi and fell back to cellular still has an
    address. Counting it would confirm a reattach that did not happen, which is
    worse than the original `wlan0` bug: the caller stops looking."""

    async def test_cellular_fallback_is_not_a_reattach(self, adb, monkeypatch):
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("1: lo    inet 127.0.0.1/8 scope host lo\n"
                        "42: rmnet_data0    inet 10.144.2.55/30 scope global", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        assert await adb.reattach_network("8BAY0WCL7") is False

    async def test_emulator_eth0_is_not_wifi(self, adb, monkeypatch):
        """An emulator's network is a QEMU NAT link, not Wi-Fi, and the
        docstring already says this reports False rather than pretending."""
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("2: eth0    inet 10.0.2.15/24 brd 10.0.2.255 scope global", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        assert await adb.reattach_network("emulator-5554") is False

    async def test_wlan1_still_counts(self, adb, monkeypatch):
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("31: wlan1    inet 192.168.1.244/24 brd x scope global", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is True

    async def test_wifi_alongside_cellular_counts(self, adb, monkeypatch):
        async def fake(serial, *args):
            if args[:2] == ("shell", "ip"):
                return ("42: rmnet_data0    inet 10.144.2.55/30 scope global\n"
                        "30: wlan0    inet 192.168.1.244/24 brd x scope global", "")
            return ("", "")

        monkeypatch.setattr(adb, "_run_adb_for_device", fake)

        assert await adb.reattach_network("8BAY0WCL7") is True
