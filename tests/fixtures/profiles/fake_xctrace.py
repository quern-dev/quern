"""A stand-in for `xcrun xctrace`, run as a real subprocess by tests/test_profiles.py.

It behaves the way real xctrace was measured to (Xcode 27.2), because a fake
kinder than the tool hides exactly the bugs worth finding:
- the --output bundle exists from the moment recording starts, not only once saved;
- stopping (SIGINT or --time-limit) is followed by a save that takes time,
  and only then "Output file saved as: ...";
- SIGINT during the save is noted in <bundle's parent>/signals, one line each,
  so a test can tell whether quern signalled a process that was saving.

record runs for --time-limit seconds scaled by FAKE_XCTRACE_SCALE, and saves
for FAKE_XCTRACE_SAVE_S. FAKE_XCTRACE_MODE picks the behaviour: ok, offline,
exit-early, error-stays (reports a failure and keeps running), stubborn
(ignores SIGINT and never saves), crash (dies mid-recording, never saving),
toc=<name> (which TOC fixture), fail=<schema>, empty=<schema> (the export
prints no table), sim-launch (the simulator's empty launch table).
"""

import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
MODE = os.environ.get("FAKE_XCTRACE_MODE", "ok")
SCALE = float(os.environ.get("FAKE_XCTRACE_SCALE", "0.02"))
SAVE_S = float(os.environ.get("FAKE_XCTRACE_SAVE_S", "0.2"))


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
    if "error-stays" in MODE:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))   # as xctrace exits on ^C
        print("Recording failed with errors. Saving output file...", flush=True)
        time.sleep(60)
        return 0
    out.mkdir(parents=True)
    (out / "data").write_bytes(b"x" * int(os.environ.get("FAKE_XCTRACE_BYTES", "1000")))
    print("Ctrl-C to stop the recording", flush=True)
    stopped = []
    if "stubborn" in MODE:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        while True:
            time.sleep(0.05)
    signal.signal(signal.SIGINT, lambda *_: stopped.append(1))
    started = time.monotonic()
    while not stopped and time.monotonic() - started < limit:
        time.sleep(0.01)
        if "crash" in MODE and time.monotonic() - started > min(limit / 2, 0.1):
            os._exit(1)
    if not stopped:
        print("Reached specified time limit, ending recording...", flush=True)
    signals = out.parent / "signals"
    signal.signal(signal.SIGINT, lambda *_: signals.open("a").write("SIGINT while saving\n"))
    print("Recording completed. Saving output file...", flush=True)
    time.sleep(SAVE_S)
    print(f"Output file saved as: {out}", flush=True)
    return 0


def export():
    toc = next((m.split("=", 1)[1] for m in MODE.split(",") if m.startswith("toc=")),
               "device-hangs-toc")
    failing = [m.split("=", 1)[1] for m in MODE.split(",") if m.startswith("fail=")]
    empty = [m.split("=", 1)[1] for m in MODE.split(",") if m.startswith("empty=")]
    if "--toc" in sys.argv:
        name = toc
    else:
        xpath = arg("--xpath")
        schema = xpath.split('@schema="')[1].split('"')[0]
        if schema in failing:
            print(f"Error: table {schema} is damaged", file=sys.stderr)
            return 4
        if schema in empty:
            print('<?xml version="1.0"?>\n<trace-query-result>\n</trace-query-result>')
            return 0
        launch = "sim-launch" if "sim-launch" in MODE else "device-launch"
        name = {"potential-hangs": "sim-hangs-potential-hangs", "hitches": "device-hitches",
                "time-profile": "device-time-profile",
                "life-cycle-period": f"{launch}-life-cycle-period"}[schema]
    sys.stdout.write((HERE / f"{name}.xml").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(record() if sys.argv[1] == "record" else export())
