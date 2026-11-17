"""A caller's own script, run in a web page (#713).

Both Web Inspectors -- a simulator's socket and a phone's lockdown service --
speak the same WebKit protocol, so the script is run and its result read here,
through a `send(method, params)` each of them supplies.

The result is described rather than taken by value. Measured on an iOS 27.2
simulator: `Runtime.evaluate` with `returnByValue` answers `{}` for a DOM node
and for a Promise alike, which reads as an empty object that is really there.
So the script runs once without it, and what came back is read from its type:

- a primitive is its value;
- a Promise is awaited (`Runtime.awaitPromise`; `awaitPromise` on the evaluate
  itself is ignored), and its result read the same way, a rejection as a throw;
- a DOM node is its description (`<input id="q">`), which has no value;
- any other object is fetched by value from the object the script returned
  (`Runtime.callFunctionOn`), not by running the script again, and one that
  cannot be -- a cycle -- says so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: What a send to the page answers: the protocol message, or None when the
#: page did not answer in time.
Send = Callable[[str, dict], Awaitable[dict | None]]

#: Objects the script's result holds are kept under this group, and released
#: together once it has been read.
GROUP = "quern-evaluate"

_THIS = "function () { return this; }"


class NoAnswer(Exception):
    """The page did not answer a step in time."""


async def run_script(send: Send, script: str) -> dict:
    """Run `script` in the page and describe what it gave.

    The answer always has `threw`. A throw has `exception`; otherwise there
    are `type` and `value`, with `description` and `class` for an object and
    `awaited` for a Promise. Raises `NoAnswer` when the page stops answering.
    """
    reply = await send("Runtime.evaluate", {"expression": script, "objectGroup": GROUP})
    try:
        return await _describe(send, reply)
    finally:
        try:
            await send("Runtime.releaseObjectGroup", {"objectGroup": GROUP})
        except Exception:  # noqa: BLE001 -- the answer is already read
            pass


async def _describe(send: Send, reply: dict | None) -> dict:
    if reply is None:
        raise NoAnswer
    if "error" in reply:
        # The protocol refused the message itself: not the script's throw.
        error = reply.get("error") or {}
        return {"threw": True, "exception": str(error.get("message") or error)}
    outcome = reply.get("result") or {}
    result = outcome.get("result") or {}
    if outcome.get("wasThrown"):
        return {"threw": True, "exception": _text(result)}

    kind = result.get("type")
    if kind != "object" or result.get("subtype") == "null":
        if kind == "undefined":
            return {"threw": False, "type": "undefined", "value": None}
        if kind == "bigint":
            return {"threw": False, "type": "bigint", "value": result.get("description")}
        if result.get("subtype") == "null":
            return {"threw": False, "type": "null", "value": None}
        return {"threw": False, "type": kind, "value": result.get("value")}

    described: dict[str, Any] = {
        "threw": False, "type": result.get("subtype") or "object", "value": None,
        "class": result.get("className"), "description": result.get("description"),
    }
    object_id = result.get("objectId")
    if object_id is None:
        # Already by value, as a callFunctionOn or awaitPromise answer is.
        described["value"] = result.get("value")
        return described
    if result.get("className") == "Promise":
        awaited = await send("Runtime.awaitPromise", {"promiseObjectId": object_id})
        settled = await _describe(send, awaited)
        settled["awaited"] = True
        return settled
    if result.get("subtype") == "node":
        return described
    by_value = await send("Runtime.callFunctionOn", {
        "objectId": object_id, "functionDeclaration": _THIS, "returnByValue": True})
    if by_value is None:
        raise NoAnswer
    if "error" in by_value:
        described["note"] = ("not serialisable as JSON: "
                             f"{(by_value.get('error') or {}).get('message')}")
        return described
    described["value"] = ((by_value.get("result") or {}).get("result") or {}).get("value")
    return described


def _text(result: dict) -> str:
    return str(result.get("description") or result.get("value") or result.get("className")
               or "an exception with no description")
