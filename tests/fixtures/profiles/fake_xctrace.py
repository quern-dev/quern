"""A stand-in for `xcrun xctrace`, run as a real subprocess by tests/test_profiles.py.

record: prints what xctrace prints, makes the --output bundle, and records
until SIGINT or --time-limit (scaled by FAKE_XCTRACE_SCALE so tests are quick).
export: prints the fixture for the table asked for.

FAKE_XCTRACE_MODE picks the behaviour: ok, offline, no-trace, exit-early,
stubborn (ignores SIGINT), toc=<name> (which TOC fixture), fail=<schema>,
sim-launch (the simulator's empty launch table rather than the device's).
"""

import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
MODE = os.environ.get("FAKE_XCTRACE_MODE", "ok")
SCALE = float(os.environ.get("FAKE_XCTRACE_SCALE", "0.02"))


def arg(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args else None


def record():
    out = Path(arg("--output"))
    limit = float(arg("--time-limit").rstrip("s")) * SCALE
    print(f"Starting recording with the {arg('--template')} template.", flush=True)
    if "offline" in MODE:
        print("Waiting for device to boot", flush=True)
        print(f"Timed out waiting for device to boot: {arg('--device')}", flush=True)
        return 0
    if "exit-early" in MODE:
        return 3
    print("Ctrl-C to stop the recording", flush=True)
    stopped = []
    if "stubborn" in MODE:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, lambda *_: stopped.append(1))
    if "no-trace" not in MODE:
        out.mkdir(parents=True)
        (out / "data").write_bytes(b"x" * int(os.environ.get("FAKE_XCTRACE_BYTES", "1000")))
    deadline = time.monotonic() + limit
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    print("Recording completed. Saving output file...", flush=True)
    return 0


def export():
    toc = next((m.split("=", 1)[1] for m in MODE.split(",") if m.startswith("toc=")),
               "sim-hangs-toc")
    failing = [m.split("=", 1)[1] for m in MODE.split(",") if m.startswith("fail=")]
    if "--toc" in sys.argv:
        name = toc
    else:
        xpath = arg("--xpath")
        schema = xpath.split('@schema="')[1].split('"')[0]
        if schema in failing:
            print(f"Error: table {schema} is damaged", file=sys.stderr)
            return 4
        launch = "sim-launch" if "sim-launch" in MODE else "device-launch"
        name = {"potential-hangs": "sim-hangs-potential-hangs", "hitches": "device-hitches",
                "time-profile": "device-time-profile",
                "life-cycle-period": f"{launch}-life-cycle-period"}[schema]
    sys.stdout.write((HERE / f"{name}.xml").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(record() if sys.argv[1] == "record" else export())
