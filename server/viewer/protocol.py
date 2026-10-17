"""The viewer listener's HTTP protocol: uvicorn's h11, with limits (#449).

The listener runs inside the main quern process and is reachable from the
LAN without a token, so its connections are the main server's file
descriptors. Plain uvicorn has no timeout for a request that never arrives,
and no cap on connections: a review opened 260 idle sockets, and the whole
process ran out of descriptors -- the 9100 API, log writes and subprocess
spawns all failing with EMFILE.

So two limits, before anything else runs:

- a connection past MAX_CONNECTIONS is closed on arrival, holding nothing;
- a connection that has not sent a complete request within REQUEST_TIMEOUT
  is closed, which ends both idle sockets and slowloris drip-feeding.

After its first request a connection is uvicorn's: a viewer's stream is
meant to be long, and keep-alive idling is bounded by timeout_keep_alive.
"""

from __future__ import annotations

from uvicorn.protocols.http.h11_impl import H11Protocol

#: Connections the viewer listener holds at once. Each viewer uses one for
#: the stream and briefly one more for the page and the muxer.
MAX_CONNECTIONS = 64
#: Seconds to send a complete request before the connection is closed.
REQUEST_TIMEOUT = 5.0


class GuardedH11Protocol(H11Protocol):
    """H11Protocol that refuses past a cap and closes requestless connections."""

    _refused = False
    _request_timer = None

    def connection_made(self, transport):  # type: ignore[override]
        if len(self.connections) >= MAX_CONNECTIONS:
            # Before super(): nothing is registered, so connection_lost below
            # has nothing to undo.
            self._refused = True
            transport.close()
            return
        super().connection_made(transport)
        self._request_timer = self.loop.call_later(REQUEST_TIMEOUT, self._no_request)

    def _no_request(self) -> None:
        self._request_timer = None
        if self.cycle is None and self.transport is not None and not self.transport.is_closing():
            self.transport.close()

    def handle_events(self) -> None:
        super().handle_events()
        # A request's headers are complete once uvicorn has started its cycle.
        if self.cycle is not None and self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None

    def connection_lost(self, exc):  # type: ignore[override]
        if self._refused:
            return
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        super().connection_lost(exc)
