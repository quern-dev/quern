"""A network phone's screen, as HEVC on stdout, for quern-media's `--hevc-stdin`.

Run by the preview manager as its own process, one per open window:

    python phone_bridge.py --udid <hardware udid>   | quern-media --hevc-stdin ...

On iOS 27, CoreDevice's display service streams a phone's screen as HEVC over
RTP, over Wi-Fi as well as USB. pymobiledevice3 reaches it without root by
riding Apple's own tunnel to the phone (`NativeRemotedTunnel`), and its
`ScreenStreamServer` does the hard part of receiving it: it drops a picture
with a lost packet and asks for a fresh keyframe, acknowledges every frame
(the phone paces its output to those acks), and restarts a stalled stream.
That server hands each viewer the pictures on `/stream.bin`, so this runs one
on loopback, reads its own `/stream.bin`, and writes each picture to stdout in
quern-media's framing. Measured on an iPhone 11 (iOS 27.2.1) over Wi-Fi: about
32 fps, about 1 s to the first picture.

Its own process, rather than a task in the server, so that the stream and
quern-media live and die together through the pipe between them: a closed
stdout (quern-media gone) ends this, and this ending closes quern-media's stdin,
which ends it -- and the server restarts the pair as it restarts a simulator's
quern-media. The viewer's password is made here and never leaves the process:
with none, anyone on this Mac could watch and drive the phone through the port.

Imports nothing from `server`: it is run by path, where `server` would resolve
to whichever checkout the venv was installed from.

Exit status: 0 when stdout closed, 1 when the stream ended or failed, 2 when
the phone could not be reached. The last line on stderr says why.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import os
import secrets
import signal
import socket
import sys
from collections.abc import Iterator

#: Message types on `/stream.bin` that carry a picture: a keyframe, a delta,
#: and a keyframe after which a browser decoder must be rebuilt. Each carries
#: its NAL units length-prefixed, parameter sets inline on a keyframe. The
#: others (3, decoder configuration; 4 and 5, device status) are not pictures.
PICTURE_TYPES = frozenset({0, 1, 2})

#: Past any picture: a larger length means the stream is out of step.
MAX_MESSAGE = 16 * 1024 * 1024

#: Seconds to wait for `/stream.bin` to start answering. The tunnel, the
#: display service and the first keyframe all come first.
FIRST_PICTURE_TIMEOUT = 40.0


class StreamError(Exception):
    """`/stream.bin` could not be read."""


class ChunkedDecoder:
    """HTTP/1.1 chunked transfer coding, fed as it arrives."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._remaining: int | None = None  # bytes left in the current chunk
        self.finished = False

    def feed(self, data: bytes) -> bytes:
        self._buffer += data
        out = bytearray()
        while not self.finished:
            if self._remaining is None:
                end = self._buffer.find(b"\r\n")
                if end < 0:
                    if len(self._buffer) > 1024:
                        raise StreamError("a chunk size line runs past 1 KB")
                    break
                line = bytes(self._buffer[:end]).split(b";", 1)[0].strip()
                del self._buffer[: end + 2]
                try:
                    size = int(line, 16)
                except ValueError:
                    raise StreamError(f"not a chunk size: {line[:20]!r}") from None
                if size == 0:
                    self.finished = True
                    break
                self._remaining = size
            # The chunk, then its CRLF.
            if len(self._buffer) < self._remaining + 2:
                break
            out += self._buffer[: self._remaining]
            if self._buffer[self._remaining : self._remaining + 2] != b"\r\n":
                raise StreamError("a chunk is not followed by CRLF")
            del self._buffer[: self._remaining + 2]
            self._remaining = None
        return bytes(out)


class MessageReader:
    """`/stream.bin`'s messages: `[4-byte BE length L][type][L-1 bytes]`."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[tuple[int, bytes]]:
        self._buffer += data
        while len(self._buffer) >= 4:
            length = int.from_bytes(self._buffer[:4], "big")
            if length < 1 or length > MAX_MESSAGE:
                raise StreamError(f"a message of {length} bytes is not this stream")
            if len(self._buffer) < 4 + length:
                return
            kind = self._buffer[4]
            body = bytes(self._buffer[5 : 4 + length])
            del self._buffer[: 4 + length]
            yield kind, body


def hevc_stdin_frame(kind: int, body: bytes) -> bytes | None:
    """The message in quern-media's framing, or None if it is not a picture."""
    if kind not in PICTURE_TYPES or not body:
        return None
    return len(body).to_bytes(4, "big") + body


def free_port() -> int:
    """A loopback port nothing holds now. Another process can take it before
    the server binds; that start then fails and says so."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def open_stream(
    port: int, password: str, server: asyncio.Task
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to `/stream.bin`, waiting while the server comes up.

    It refuses connections before it binds and answers 503 until the phone's
    first keyframe; both are waited out. A server that has stopped is not.

    The writer is returned to be held: a StreamWriter that is collected closes
    its connection, which ended the stream after its first picture.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FIRST_PICTURE_TIMEOUT
    auth = base64.b64encode(f"quern:{password}".encode()).decode()
    last = "the stream server did not start"
    while loop.time() < deadline:
        if server.done():
            raise StreamError(_server_failure(server))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            last = f"the stream server is not accepting: {exc}"
            await asyncio.sleep(0.25)
            continue
        writer.write(
            f"GET /stream.bin HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Authorization: Basic {auth}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status = await reader.readline()
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        code = status.split(b" ", 2)[1:2]
        if code == [b"200"]:
            if "chunked" not in headers.get("transfer-encoding", "").lower():
                raise StreamError("/stream.bin did not answer chunked")
            return reader, writer
        writer.close()
        last = f"/stream.bin answered {status.decode('latin-1').strip() or 'nothing'}"
        if code != [b"503"]:
            raise StreamError(last)
        await asyncio.sleep(0.5)
    raise StreamError(f"no picture within {FIRST_PICTURE_TIMEOUT:.0f}s: {last}")


def _server_failure(server: asyncio.Task) -> str:
    if server.cancelled():
        return "the stream server was stopped"
    exc = server.exception()
    return f"the stream server stopped: {exc}" if exc else "the stream server stopped"


async def relay(reader: asyncio.StreamReader, out: asyncio.StreamWriter) -> None:
    """Copy pictures from `/stream.bin` to stdout until either side ends."""
    chunks = ChunkedDecoder()
    messages = MessageReader()
    while not chunks.finished:
        data = await reader.read(65536)
        if not data:
            break
        for kind, body in messages.feed(chunks.feed(data)):
            frame = hevc_stdin_frame(kind, body)
            if frame is not None:
                out.write(frame)
                await out.drain()  # raises once quern-media has gone
    raise StreamError("the phone's stream ended")


async def stdout_writer() -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    return asyncio.StreamWriter(transport, protocol, None, loop)


async def run(udid: str) -> int:
    try:
        from pymobiledevice3.remote.core_device.screen_stream import ScreenStreamServer
        from pymobiledevice3.remote.native_tunnel import NativeRemotedTunnel
    except ImportError as exc:
        return fail(2, f"this pymobiledevice3 cannot stream a phone's screen: {exc}")

    try:
        out = await stdout_writer()
    except ValueError as exc:  # a regular file: asyncio writes only to pipes
        return fail(2, f"stdout must be a pipe to quern-media: {exc}")
    password = secrets.token_urlsafe(24)
    port = free_port()
    try:
        async with NativeRemotedTunnel(serial=udid) as rsd:
            stream_server = ScreenStreamServer(
                rsd, bind="127.0.0.1", http_port=port, password=password,
                audio_default_on=False,
            )
            server = asyncio.create_task(stream_server.serve(), name="screen-stream")
            try:
                reader, connection = await open_stream(port, password, server)
                try:
                    await relay(reader, out)
                except StreamError:
                    # ScreenStreamServer takes SIGTERM and SIGINT over from
                    # main_async and returns from serve() on them; the stream
                    # ending then is the stop, not the phone going away. It
                    # closes the connection before it returns, so wait for it.
                    await asyncio.wait({server}, timeout=2.0)
                    if server.done() and not server.cancelled() and server.exception() is None:
                        raise StreamError("stopped") from None
                    raise
                finally:
                    connection.close()
            finally:
                server.cancel()
                with contextlib.suppress(BaseException):
                    await server
    except (BrokenPipeError, ConnectionResetError):
        return 0  # quern-media has gone: nobody to stream to
    except StreamError as exc:
        return fail(1, str(exc))
    except asyncio.CancelledError:
        return fail(1, "stopped")
    except Exception as exc:  # noqa: BLE001 -- the reason is the whole report
        return fail(2, f"could not reach the phone over the network: {exc}")
    return 0


def fail(status: int, reason: str) -> int:
    print(f"phone: {reason}", file=sys.stderr, flush=True)
    return status


async def main_async(udid: str) -> int:
    task = asyncio.create_task(run(udid))
    loop = asyncio.get_running_loop()
    # SIGTERM from the preview manager: unwind, so the tunnel's assertion on
    # the phone is released rather than left to time out.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    watch = asyncio.create_task(watch_parent(task), name="watch-parent")
    try:
        return await task
    finally:
        watch.cancel()


async def watch_parent(task: asyncio.Task, interval: float = 2.0) -> None:
    """End with the server that started this, as quern-media's
    `--exit-with-parent` does. The broken pipe would end it too, but only at
    the next picture, and a still screen may not send one for a long time."""
    parent = os.getppid()
    while True:
        await asyncio.sleep(interval)
        if os.getppid() != parent:
            print("phone: the server that started this exited", file=sys.stderr, flush=True)
            task.cancel()
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--udid", required=True, help="the phone's hardware UDID")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args.udid))


if __name__ == "__main__":
    sys.exit(main())
