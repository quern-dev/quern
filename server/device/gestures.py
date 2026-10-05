"""Multi-finger gestures as finger paths (#252).

Pinch, rotate and two-finger pan are the same primitive -- two contacts, each
moving along its own path -- and differ only in the geometry. That geometry is
computed here, in points, so it is testable without a device; a backend that
can move several contacts at once (`multitouch = True`) is handed the paths and
nothing else. Taps that need the system's timing -- a double tap, a two-finger
tap -- are described by points plus a count, for the same backend.

Every path has the same number of waypoints, one per time step, and the first
and last are where the fingers go down and come up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from server.models import InvalidDeviceRequestError

GestureType = Literal["pinch", "rotate", "pan", "double_tap", "two_finger_tap"]
GESTURE_TYPES: tuple[str, ...] = ("pinch", "rotate", "pan", "double_tap", "two_finger_tap")

#: Waypoints for a short gesture. A rotation adds more, so a large angle still
#: moves along an arc rather than across chords.
DEFAULT_STEPS = 12
#: Separation, in points, of the fingers at the narrow end of a pinch: where
#: they start for a spread, and where they end for a squeeze.
DEFAULT_PINCH_DISTANCE = 60.0
#: Distance from the centre to each finger in a rotation.
DEFAULT_ROTATE_RADIUS = 80.0
#: Separation of the fingers in a two-finger pan or tap.
DEFAULT_FINGER_SPREAD = 40.0
#: The largest turn a rotation takes, ten full circles. Each 10 degrees is a
#: waypoint and each waypoint at least 8ms on the bridge, so this bounds the
#: gesture near 3s; unbounded, a large enough angle outlasted the bridge's 30s
#: command timeout, which kills it mid-gesture with the fingers still down.
MAX_ROTATE_DEGREES = 3600.0
#: The shortest step the bridge sends; a duration shorter than the steps allow
#: takes this long per step anyway, and the plan says so.
MIN_STEP_SECONDS = 0.008
#: A double tap's taps must land inside the system's double-tap interval; two
#: 0.08s apart read as one double tap on iOS 18.6, 0.6s apart as two singles.
DEFAULT_TAP_INTERVAL = 0.08

Point = tuple[float, float]


@dataclass
class Plan:
    """What a backend is asked to do.

    `paths` for a moving gesture, or `points` and `count` for a tap, in
    points. `geometry` is the same thing said for the caller, who asked for a
    scale or an angle and should be told where the fingers actually went.
    """

    kind: str
    paths: list[list[Point]] | None = None
    points: list[Point] | None = None
    count: int = 1
    interval: float = DEFAULT_TAP_INTERVAL
    duration: float = 0.6

    def __post_init__(self) -> None:
        # What the bridge will actually take: it never steps faster than
        # MIN_STEP_SECONDS, so a long path at a short duration runs longer, and
        # the response should not claim otherwise.
        if self.paths:
            self.duration = max(self.duration, (len(self.paths[0]) - 1) * MIN_STEP_SECONDS)

    def geometry(self) -> dict:
        if self.paths is not None:
            return {"fingers": [{"from": _r(p[0]), "to": _r(p[-1])} for p in self.paths],
                    "duration": self.duration}
        return {"fingers": [{"at": _r(p)} for p in self.points or []],
                "count": self.count, "interval": self.interval}


def _r(p: Point) -> list[float]:
    return [round(p[0], 1), round(p[1], 1)]


def _bad(message: str) -> InvalidDeviceRequestError:
    # Labelled `quern` rather than a backend: the request was refused before
    # one was chosen (#186).
    return InvalidDeviceRequestError(message, tool="quern")


def _positive(name: str, value: float | None, default: float) -> float:
    if value is None:
        return default
    if not math.isfinite(value) or value <= 0:
        raise _bad(f"{name} must be a positive number, not {value!r}")
    return float(value)


def _lerp_paths(starts: list[Point], ends: list[Point], steps: int) -> list[list[Point]]:
    return [
        [(a[0] + (b[0] - a[0]) * i / (steps - 1), a[1] + (b[1] - a[1]) * i / (steps - 1))
         for i in range(steps)]
        for a, b in zip(starts, ends, strict=True)
    ]


def pinch(x: float, y: float, scale: float | None, *, distance: float | None = None,
          angle: float = 0.0, duration: float = 0.6) -> Plan:
    """Two fingers converging or spreading along a line through (x, y).

    `scale` is end separation over start separation, as a pinch recogniser
    reports it: above 1 spreads (zoom in), below 1 squeezes. `distance` is the
    separation at the narrow end. `angle` turns the line from horizontal, in
    degrees, for a recogniser or a screen edge that needs the other axis.
    """
    if scale is None:
        raise _bad("pinch needs a scale: above 1 spreads the fingers, below 1 squeezes them")
    if not math.isfinite(scale) or scale <= 0 or scale == 1:
        raise _bad(f"pinch scale must be a positive number other than 1, not {scale!r}")
    narrow = _positive("distance", distance, DEFAULT_PINCH_DISTANCE)
    start, end = (narrow, narrow * scale) if scale > 1 else (narrow / scale, narrow)
    ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def pair(separation: float) -> list[Point]:
        h = separation / 2
        return [(x - ux * h, y - uy * h), (x + ux * h, y + uy * h)]

    return Plan("pinch", paths=_lerp_paths(pair(start), pair(end), DEFAULT_STEPS),
                duration=duration)


def rotate(x: float, y: float, degrees: float | None, *, radius: float | None = None,
           angle: float = 0.0, duration: float = 0.6) -> Plan:
    """Two fingers on opposite sides of (x, y), orbiting it by `degrees`.

    Positive is clockwise on screen, which is the sign a rotation recogniser
    reports. The fingers follow the arc: interpolating straight between the
    end points would cut the chord, and at 180 degrees bring both fingers
    through the centre.
    """
    if degrees is None:
        raise _bad("rotate needs degrees: positive turns clockwise")
    if not math.isfinite(degrees) or degrees == 0 or abs(degrees) > MAX_ROTATE_DEGREES:
        raise _bad(f"rotate degrees must be a non-zero number within "
                   f"±{MAX_ROTATE_DEGREES:.0f}, not {degrees!r}")
    r = _positive("radius", radius, DEFAULT_ROTATE_RADIUS)
    steps = max(DEFAULT_STEPS, math.ceil(abs(degrees) / 10) + 1)
    paths: list[list[Point]] = [[], []]
    for i in range(steps):
        theta = math.radians(angle + degrees * i / (steps - 1))
        c, s = math.cos(theta), math.sin(theta)
        paths[0].append((x - r * c, y - r * s))
        paths[1].append((x + r * c, y + r * s))
    return Plan("rotate", paths=paths, duration=duration)


def pan(x: float, y: float, dx: float | None, dy: float | None, *,
        distance: float | None = None, duration: float = 0.6) -> Plan:
    """Two fingers, `distance` apart around (x, y), moving together by (dx, dy)."""
    dx, dy = float(dx or 0), float(dy or 0)
    if not (math.isfinite(dx) and math.isfinite(dy)) or (dx == 0 and dy == 0):
        raise _bad("pan needs dx or dy: how far to move both fingers, in points")
    h = _positive("distance", distance, DEFAULT_FINGER_SPREAD) / 2
    starts = [(x - h, y), (x + h, y)]
    ends = [(px + dx, py + dy) for px, py in starts]
    return Plan("pan", paths=_lerp_paths(starts, ends, DEFAULT_STEPS), duration=duration)


def double_tap(x: float, y: float, *, count: int | None = None,
               interval: float | None = None) -> Plan:
    """`count` taps (default 2) inside the double-tap interval, as one gesture."""
    n = 2 if count is None else count
    if n < 2:
        raise _bad(f"double_tap count must be 2 or more, not {n}: a single tap is `tap`")
    return Plan("double_tap", points=[(x, y)], count=n,
                interval=_positive("interval", interval, DEFAULT_TAP_INTERVAL))


def two_finger_tap(x: float, y: float, *, distance: float | None = None,
                   count: int | None = None, interval: float | None = None) -> Plan:
    """Two fingers, `distance` apart around (x, y), down and up together."""
    n = 1 if count is None else count
    if n < 1:
        raise _bad(f"two_finger_tap count must be 1 or more, not {n}")
    h = _positive("distance", distance, DEFAULT_FINGER_SPREAD) / 2
    return Plan("two_finger_tap", points=[(x - h, y), (x + h, y)], count=n,
                interval=_positive("interval", interval, DEFAULT_TAP_INTERVAL))


def plan(kind: str, x: float, y: float, *, scale: float | None = None,
         degrees: float | None = None, dx: float | None = None, dy: float | None = None,
         distance: float | None = None, angle: float | None = None,
         duration: float | None = None, count: int | None = None,
         interval: float | None = None) -> Plan:
    """The finger paths or tap points for one named gesture, or a refusal."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise _bad(f"x and y must be numbers, not {x!r}, {y!r}")
    d = _positive("duration", duration, 0.6)
    a = 0.0 if angle is None else float(angle)
    # NaN went through to the bridge as a bare `NaN`, which its JSON parser
    # rejects, and came back as a 500.
    if not math.isfinite(a):
        raise _bad(f"angle must be a number, not {angle!r}")
    if kind == "pinch":
        return pinch(x, y, scale, distance=distance, angle=a, duration=d)
    if kind == "rotate":
        return rotate(x, y, degrees, radius=distance, angle=a, duration=d)
    if kind == "pan":
        return pan(x, y, dx, dy, distance=distance, duration=d)
    if kind == "double_tap":
        return double_tap(x, y, count=count, interval=interval)
    if kind == "two_finger_tap":
        return two_finger_tap(x, y, distance=distance, count=count, interval=interval)
    raise _bad(f"unknown gesture {kind!r}: one of {', '.join(GESTURE_TYPES)}")
