"""A web page's console, read on demand (#714).

WebKit keeps each page's recent console messages, and replays them to an
inspector that enables the Console domain, before it answers the enable.
Measured on an iOS 27.2 simulator: the replay carried `console.log`/`warn`/
`error` output, uncaught errors and unhandled Promise rejections, with their
original timestamps, including what was logged before quern attached -- and
only the last ~100 messages: after 1,500 logs, 97 came back, the newest.

So the console is read, not streamed: enable, take the replay, disable. Nothing
is held on the page between reads, so Safari's Develop menu can still open it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: About how many messages WebKit keeps per page; older ones are gone. Not
#: exact: 97 and 93 came back in two reads of pages that had logged more.
WINDOW = 100

#: From this many messages read, the oldest may already have been dropped.
NEAR_WINDOW = 80


class Collector:
    """Gathers the Console events a page sends, as they arrive."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def on_event(self, event: dict) -> None:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Console.messageAdded":
            message = params.get("message") or {}
            # A replayed message carries its own count: logged three times
            # before quern attached, it arrives once with repeatCount 3
            # (CodeRabbit on #722).
            self.messages.append(dict(message, repeat=int(message.get("repeatCount") or 1)))
        elif method == "Console.messageRepeatCountUpdated" and self.messages:
            # The last message again, `count` times in all.
            self.messages[-1]["repeat"] = int(params.get("count") or 1)


def entry(message: dict) -> dict:
    """One console message as `get_web_console` gives it."""
    timestamp = message.get("timestamp")
    when = None
    if isinstance(timestamp, int | float):
        when = datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
    out: dict[str, Any] = {
        "level": message.get("level"),
        "text": _text(message),
        "source": message.get("source"),
        "timestamp": when,
    }
    url = message.get("url")
    if url and url != "undefined":
        out["url"] = url
        out["line"] = message.get("line")
        out["column"] = message.get("column")
    if message.get("repeat", 1) > 1:
        out["repeat"] = message["repeat"]
    return out


def _text(message: dict) -> str:
    """Every argument, not only the first. A console-api message carries the
    first argument alone in `text`; `console.log('n', 4)` reads "n" there."""
    parameters = message.get("parameters")
    if not parameters:
        return str(message.get("text") or "")
    return " ".join(_argument(p) for p in parameters)


def _argument(parameter: dict) -> str:
    """One console argument as the console shows it: JavaScript's spelling,
    not Python's (CodeRabbit on #722)."""
    kind = parameter.get("type")
    if kind == "object" and parameter.get("subtype") == "null":
        return "null"
    if kind == "undefined":
        return "undefined"
    if kind == "boolean":
        return "true" if parameter.get("value") else "false"
    if kind == "string":
        return str(parameter.get("value", ""))
    # A number's description is how JavaScript writes it: -0, NaN, Infinity.
    if parameter.get("description") is not None:
        return str(parameter["description"])
    if "value" in parameter:
        return str(parameter["value"])
    return str(parameter.get("className") or kind)
