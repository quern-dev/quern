"""Network phone Screen Mirror: the bridge (phone_bridge.py) and its preview plumbing."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import deque

import pytest

from server.device.media import phone_bridge as bridge
from server.device.media import preview
from server.device.media.preview import NetworkPhone, PreviewManager
from server.models import DeviceInfo, DeviceState, DeviceType


def _message(kind: int, body: bytes) -> bytes:
    return (len(body) + 1).to_bytes(4, "big") + bytes([kind]) + body


def _chunked(*parts: bytes) -> bytes:
    out = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in parts)
    return out + b"0\r\n\r\n"


# MARK: - the bridge's reading of /stream.bin


class TestChunkedDecoder:
    def test_whole_and_a_byte_at_a_time_agree(self):
        wire = _chunked(b"hello ", b"world")
        whole = bridge.ChunkedDecoder()
        assert whole.feed(wire) == b"hello world"
        assert whole.finished

        trickle = bridge.ChunkedDecoder()
        got = b"".join(trickle.feed(bytes([b])) for b in wire)
        assert got == b"hello world"
        assert trickle.finished

    def test_a_chunk_extension_is_ignored(self):
        assert bridge.ChunkedDecoder().feed(b"3;name=x\r\nabc\r\n") == b"abc"

    @pytest.mark.parametrize("wire", [b"zz\r\nabc\r\n", b"3\r\nabcXY", b"9" * 2000])
    def test_a_stream_out_of_step_is_refused(self, wire):
        with pytest.raises(bridge.StreamError):
            bridge.ChunkedDecoder().feed(wire)


class TestMessages:
    def test_only_pictures_go_to_quern_media_in_its_framing(self):
        """Types 0-2 are pictures. 3 (decoder configuration), 4 and 5 (device
        status) would reach quern-media as garbage NAL units."""
        au = (2).to_bytes(4, "big") + b"\x26\x01"
        reader = bridge.MessageReader()
        wire = b"".join(_message(k, au) for k in range(6))
        frames = [bridge.hevc_stdin_frame(k, b) for k, b in reader.feed(wire)]
        picture = len(au).to_bytes(4, "big") + au
        assert frames == [picture, picture, picture, None, None, None]

    def test_an_empty_picture_is_not_sent(self):
        assert bridge.hevc_stdin_frame(0, b"") is None

    def test_a_split_message_waits_for_its_rest(self):
        reader = bridge.MessageReader()
        wire = _message(1, b"\x00\x00\x00\x01\x02")
        assert list(reader.feed(wire[:6])) == []
        assert list(reader.feed(wire[6:])) == [(1, b"\x00\x00\x00\x01\x02")]

    @pytest.mark.parametrize("length", [0, bridge.MAX_MESSAGE + 1])
    def test_an_implausible_length_is_refused(self, length):
        with pytest.raises(bridge.StreamError):
            list(bridge.MessageReader().feed(length.to_bytes(4, "big") + b"\x00"))


class _Out:
    """quern-media's stdin, as the bridge's writer sees it."""

    def __init__(self, broken: bool = False):
        self.data = bytearray()
        self.broken = broken

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        if self.broken:
            raise BrokenPipeError("quern-media has gone")


def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


class TestRelay:
    def test_pictures_are_relayed_and_the_end_is_an_error(self):
        """The stream ending is the phone's stream stopping, never a clean end:
        the server restarts the pair on it."""
        au = (2).to_bytes(4, "big") + b"\x26\x01"
        wire = _chunked(_message(0, au), _message(5, b"{}"), _message(1, au))
        out = _Out()

        async def run():
            await bridge.relay(_reader(wire), out)

        with pytest.raises(bridge.StreamError, match="ended"):
            asyncio.run(run())
        frame = len(au).to_bytes(4, "big") + au
        assert bytes(out.data) == frame + frame

    def test_quern_media_gone_ends_the_relay(self):
        wire = _chunked(_message(0, (1).to_bytes(4, "big") + b"\x26"))

        async def run():
            await bridge.relay(_reader(wire), _Out(broken=True))

        with pytest.raises(BrokenPipeError):
            asyncio.run(run())


class TestOpenStream:
    """`/stream.bin` refuses before the server binds and answers 503 until the
    first keyframe; both are waited out, and nothing else is."""

    @staticmethod
    def _serve(monkeypatch, answers: list[bytes]):
        sent: list[bytes] = []

        class _Writer:
            def write(self, b):
                sent.append(b)

            async def drain(self):
                pass

            def close(self):
                pass

        async def _connect(host, port):
            assert host == "127.0.0.1"
            answer = answers.pop(0)
            if answer is None:
                raise ConnectionRefusedError("not yet")
            return _reader(answer), _Writer()

        monkeypatch.setattr(bridge.asyncio, "open_connection", _connect)
        return sent

    @staticmethod
    async def _forever():
        await asyncio.sleep(3600)

    def test_a_refusal_and_a_503_are_waited_out(self, monkeypatch):
        ok = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + _chunked(b"x")
        sent = self._serve(monkeypatch, [
            None, b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n", ok,
        ])

        async def run():
            server = asyncio.create_task(self._forever())
            try:
                reader = await bridge.open_stream(8123, "pw", server)
                return await reader.read()
            finally:
                server.cancel()

        assert asyncio.run(run()).startswith(b"1\r\nx")
        # The password goes with every request, as Basic credentials.
        assert all(b"Authorization: Basic cXVlcm46cHc=" in r for r in sent)

    def test_a_refusal_other_than_503_is_reported(self, monkeypatch):
        self._serve(monkeypatch, [b"HTTP/1.1 401 Unauthorized\r\n\r\n"])

        async def run():
            server = asyncio.create_task(self._forever())
            try:
                await bridge.open_stream(8123, "pw", server)
            finally:
                server.cancel()

        with pytest.raises(bridge.StreamError, match="401"):
            asyncio.run(run())

    def test_a_server_that_stopped_is_not_waited_for(self, monkeypatch):
        """Its reason -- the phone was not found, the service is missing -- is
        the report, not a 40 s timeout."""
        self._serve(monkeypatch, [None] * 1000)

        async def fails():
            raise RuntimeError("DisplayService is not available")

        async def run():
            server = asyncio.create_task(fails())
            await asyncio.sleep(0)
            await bridge.open_stream(8123, "pw", server)

        with pytest.raises(bridge.StreamError, match="DisplayService is not available"):
            asyncio.run(run())


# MARK: - which phones the menu shows


def _phone(udid, os_version, transport="localNetwork", name="iPhone 11"):
    return DeviceInfo(
        udid=udid, name=name, state=DeviceState.BOOTED, device_type=DeviceType.DEVICE,
        os_version=os_version, connection_type=transport,
    )


class TestNetworkPhones:
    @staticmethod
    def _list(monkeypatch, devices, *, missing=None):
        from server.device.ios import devicectl

        async def _devices(self):
            return devices

        monkeypatch.setattr(devicectl.DevicectlBackend, "list_devices", _devices)
        monkeypatch.setattr(preview, "phone_streaming_missing", lambda: missing)

    def test_network_phones_are_listed_and_older_ones_say_why_not(self, monkeypatch):
        from server.device.ios import devicectl

        devicectl._remember_identity("CD-27", "00008030-27")
        devicectl._remember_identity("CD-26", "00008030-26")
        devicectl._remember_identity("CD-USB", "00008030-USB")
        self._list(monkeypatch, [
            _phone("CD-27", "iOS 27.2.1"),
            _phone("CD-26", "iOS 26.5", name="Old"),
            # A USB phone is a capture device: the app lists it itself.
            _phone("CD-USB", "iOS 27.2", transport="usb"),
        ])

        phones, error = asyncio.run(preview.network_phones())
        assert error is None
        assert [(p.key, p.hardware_udid) for p in phones] == [
            ("CD-27", "00008030-27"), ("CD-26", "00008030-26"),
        ]
        assert phones[0].unavailable is None
        assert "iOS 27" in phones[1].unavailable and "26.5" in phones[1].unavailable
        entry = phones[1].menu_entry()
        assert entry["available"] is False and entry["reason"] == phones[1].unavailable

    def test_a_pymobiledevice3_without_streaming_greys_every_phone(self, monkeypatch):
        from server.device.ios import devicectl

        devicectl._remember_identity("CD-A", "00008030-A")
        self._list(monkeypatch, [_phone("CD-A", "iOS 27.2")], missing="needs a newer one")
        phones, _ = asyncio.run(preview.network_phones())
        assert phones[0].unavailable == "needs a newer one"

    def test_a_phone_without_a_hardware_udid_cannot_be_named(self, monkeypatch):
        self._list(monkeypatch, [_phone("CD-NOALIAS", "iOS 27.2")])
        phones, _ = asyncio.run(preview.network_phones())
        assert "hardware UDID" in phones[0].unavailable

    def test_could_not_ask_is_not_no_phones(self, monkeypatch):
        from server.device.ios import devicectl

        async def _broken(self):
            raise OSError("devicectl is missing")

        monkeypatch.setattr(devicectl.DevicectlBackend, "list_devices", _broken)
        phones, error = asyncio.run(preview.network_phones())
        assert phones == [] and "devicectl is missing" in error

    def test_this_pymobiledevice3_has_what_the_bridge_imports(self):
        """The venv's copy is the one the bridge runs on."""
        assert preview.phone_streaming_missing() is None


# MARK: - the bridge and quern-media, joined by a pipe


class _Proc:
    def __init__(self, stderr_lines=(), exit_code=None):
        self.stderr = asyncio.StreamReader()
        for line in stderr_lines:
            self.stderr.feed_data(line.encode() + b"\n")
        self.returncode = exit_code
        self.terminated = False
        if exit_code is not None:
            self.stderr.feed_eof()

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
            self.stderr.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


class TestPhoneStream:
    def test_the_bridge_feeds_quern_media_through_a_pipe_the_server_lets_go_of(
        self, monkeypatch,
    ):
        """The server's copies of the pipe's ends would keep either process
        from ever seeing the other go."""
        calls = []
        joined = []

        async def _spawn(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 2:
                # Both ends are still open while the children are spawned:
                # what the bridge writes, quern-media reads.
                os.write(calls[0][1]["stdout"], b"!")
                joined.append(os.read(kwargs["stdin"], 1))
            return _Proc()

        monkeypatch.setattr(preview.asyncio, "create_subprocess_exec", _spawn)

        async def run():
            mgr = PreviewManager()
            mgr._network_phones["CD-1"] = "00008030-1"
            stream = await mgr._start_stream("CD-1", "/tmp/quern-media", 8500)
            await mgr._stop_stream("CD-1")
            return stream

        stream = asyncio.run(run())
        (bridge_args, bridge_kw), (media_args, media_kw) = calls
        assert bridge_args[:2] == (sys.executable, str(preview.PHONE_BRIDGE))
        assert bridge_args[2:] == ("--udid", "00008030-1")
        assert media_args[:5] == (
            "/tmp/quern-media", "--hevc-stdin", "--source-id", "CD-1", "--serve",
        )
        assert joined == [b"!"], "the bridge's stdout is not quern-media's stdin"
        for fd in (media_kw["stdin"], bridge_kw["stdout"]):
            with pytest.raises(OSError):
                os.fstat(fd)
        assert stream.feeder.terminated and stream.process.terminated

    def test_a_quern_media_that_cannot_start_takes_its_bridge_with_it(self, monkeypatch):
        bridges = []

        async def _spawn(*args, **kwargs):
            if args[0] == sys.executable:
                bridges.append(_Proc())
                return bridges[-1]
            raise OSError("quern-media is missing")

        monkeypatch.setattr(preview.asyncio, "create_subprocess_exec", _spawn)

        async def run():
            mgr = PreviewManager()
            mgr._network_phones["CD-1"] = "00008030-1"
            await mgr._start_stream("CD-1", "/tmp/quern-media", 8500)

        with pytest.raises(OSError):
            asyncio.run(run())
        assert bridges[0].terminated

    def test_the_bridge_says_why_the_phone_went_off_air(self, monkeypatch):
        """quern-media only knows its stdin closed; the bridge knows the phone
        left the network, and that is what the window and status say."""
        async def run():
            mgr = PreviewManager()
            mgr._active["CD-1"] = preview.ActivePreview(name="CD-1", position=0, kind="phone")
            feeder = _Proc(["phone: could not reach the phone over the network: gone"], exit_code=2)
            media = _Proc(["[hevc] the stream ended (stdin closed)"], exit_code=0)
            stream = preview._StreamProcess(
                process=media, port=8500, log=deque(maxlen=20), feeder=feeder,
            )
            stream.feeder_drain = asyncio.create_task(mgr._drain_feeder("CD-1", stream))
            mgr._streams["CD-1"] = stream
            # Not wanted any more by the time it would restart.
            monkeypatch.setattr(preview, "STREAM_RESTART_DELAYS", (0.0,))

            async def _never(*_a, **_k):
                mgr._active.pop("CD-1", None)
                raise OSError("stopped")

            async def _build():
                return "/tmp/quern-media"

            monkeypatch.setattr(mgr, "_start_stream", _never)
            monkeypatch.setattr(preview, "build_media_engine", _build)
            preview_record = mgr._active["CD-1"]
            await mgr._drain_stream("CD-1", stream)
            return preview_record

        record = asyncio.run(run())
        assert record.on_air is False
        assert record.off_air_reason == "could not reach the phone over the network: gone"

    def test_status_reports_a_phone_off_air(self):
        mgr = PreviewManager()
        mgr._active["CD-1"] = preview.ActivePreview(
            name="CD-1", position=0, kind="phone", stream_port=8500,
            on_air=False, off_air_reason="the phone's stream ended",
        )
        entry = mgr.status()["active"]["CD-1"]
        assert entry["on_air"] is False
        assert entry["off_air_reason"] == "the phone's stream ended"


# MARK: - the app's Devices menu


class TestMenu:
    @staticmethod
    def _manager(monkeypatch, phones):
        mgr = PreviewManager()
        sent = []

        async def _send(cmd):
            sent.append(cmd)

        async def _phones():
            return phones, None

        monkeypatch.setattr(mgr, "_send", _send)
        monkeypatch.setattr(preview, "network_phones", _phones)
        return mgr, sent

    @staticmethod
    async def _settle():
        for _ in range(5):
            await asyncio.sleep(0)

    def test_an_opening_menu_is_sent_the_phones(self, monkeypatch):
        old = NetworkPhone("CD-26", "00008030-26", "Old", "iOS 26.5", unavailable="needs iOS 27")
        mgr, sent = self._manager(monkeypatch, [old])

        async def run():
            mgr._dispatch_event({"event": "network_phones_wanted"})
            await self._settle()

        asyncio.run(run())
        assert sent == [{"cmd": "network_phones", "phones": [old.menu_entry()], "error": None}]

    def test_picking_a_phone_that_cannot_stream_says_why(self, monkeypatch):
        old = NetworkPhone("CD-26", "00008030-26", "Old", "iOS 26.5", unavailable="needs iOS 27")
        mgr, sent = self._manager(monkeypatch, [old])
        mgr._phone_list = {old.key: old}

        async def run():
            mgr._dispatch_event({"event": "open_network_phone", "key": "CD-26"})
            await self._settle()

        asyncio.run(run())
        assert sent[-1]["cmd"] == "open_failed"
        assert "needs iOS 27" in sent[-1]["error"]
        assert mgr._streams == {}

    def test_picking_a_phone_opens_its_stream(self, monkeypatch):
        phone = NetworkPhone("CD-27", "00008030-27", "iPhone 11", "iOS 27.2.1")
        mgr, sent = self._manager(monkeypatch, [phone])
        opened = []

        async def _add(p, **_kw):
            opened.append(p)

        monkeypatch.setattr(mgr, "add_network_phone", _add)

        async def run():
            mgr._dispatch_event({"event": "open_network_phone", "key": "CD-27"})
            await self._settle()

        asyncio.run(run())
        assert opened == [phone]
        assert not [c for c in sent if c["cmd"] == "open_failed"]

    def test_a_phone_that_left_the_network_says_so(self, monkeypatch):
        mgr, sent = self._manager(monkeypatch, [])

        async def run():
            mgr._dispatch_event({"event": "open_network_phone", "key": "CD-GONE"})
            await self._settle()

        asyncio.run(run())
        assert sent[-1] == {
            "cmd": "open_failed", "key": "CD-GONE",
            "error": "the phone is no longer on the network",
        }
