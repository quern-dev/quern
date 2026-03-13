# Workflow: Agent-Generated Test Scripts

This is the "teach a man to fish" workflow. Instead of having your AI agent run through a test scenario interactively every time (burning tokens on each run), ask it to **write a script** that does the same thing using Quern's HTTP API. Run the script as many times as you want — zero tokens, zero agent involvement. Bring the agent back only when something fails.

## The Idea

Your agent knows:
- How to interact with your app (UI automation, app lifecycle)
- How to verify network behavior (proxy flows, mocking)
- How to check results (screenshots, screen content, logs)
- How to manage device state (save, restore, configure)

All of these capabilities are available through Quern's HTTP API — the same API the MCP tools use. So anything your agent can do interactively, it can also write as a standalone script.

## Step 1: Develop the Test Interactively

Start by working through the scenario with your agent as you normally would:

> "Boot a simulator, install my app, log in as testuser, navigate to the Orders screen, and verify that the order list loads correctly from the API"

Your agent figures out the flow: which elements to tap, what to wait for, what the API response should look like, what the screen should show. It works through edge cases and timing issues.

## Step 2: Ask for a Script

Once the flow works:

> "Now write me a Python test script that does exactly what we just did, using Quern's HTTP API directly. Include assertions for the API response and the screen content. Make it something I can run with pytest."

Your agent generates a script like:

```python
"""Order list loading test — generated from interactive session."""
import requests
import pytest
import time

BASE = "http://localhost:9100/api/v1"
API_KEY = open("~/.quern/api-key").read().strip()  # your agent reads the real path
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

@pytest.fixture(autouse=True)
def setup_device():
    """Boot simulator, install app, restore clean state."""
    # Resolve a simulator
    r = requests.post(f"{BASE}/devices/resolve", json={"device_type": "simulator"}, headers=HEADERS)
    udid = r.json()["udid"]

    # Restore known-good state
    requests.post(f"{BASE}/device/app/state/restore", json={
        "udid": udid, "bundle_id": "com.example.myapp", "label": "logged-in"
    }, headers=HEADERS)

    yield udid

def test_order_list_loads(setup_device):
    udid = setup_device

    # Launch app
    requests.post(f"{BASE}/device/app/launch", json={
        "udid": udid, "bundle_id": "com.example.myapp"
    }, headers=HEADERS)
    time.sleep(2)

    # Tap Orders tab
    requests.post(f"{BASE}/device/ui/tap-element", json={
        "udid": udid, "label": "Orders", "element_type": "Button"
    }, headers=HEADERS)
    time.sleep(1)

    # Verify the API call happened
    flows = requests.get(f"{BASE}/proxy/flows", params={
        "simulator_udid": udid, "host": "api.example.com", "url_contains": "/orders"
    }, headers=HEADERS).json()

    assert len(flows["flows"]) > 0, "No /orders API call captured"
    assert flows["flows"][0]["response"]["status_code"] == 200

    # Verify screen shows order data
    screen = requests.get(f"{BASE}/device/screen-summary", params={
        "udid": udid
    }, headers=HEADERS).json()

    assert any("Order #" in el.get("label", "") for el in screen["elements"]), \
        "No order items visible on screen"

def test_order_list_empty_state(setup_device):
    udid = setup_device

    # Mock empty response
    requests.post(f"{BASE}/proxy/mocks", json={
        "pattern": "~d api.example.com & ~u /orders",
        "status_code": 200,
        "body": '{"orders": []}',
        "headers": {"Content-Type": "application/json"}
    }, headers=HEADERS)

    # Launch and navigate
    requests.post(f"{BASE}/device/app/launch", json={
        "udid": udid, "bundle_id": "com.example.myapp"
    }, headers=HEADERS)
    time.sleep(2)

    requests.post(f"{BASE}/device/ui/tap-element", json={
        "udid": udid, "label": "Orders", "element_type": "Button"
    }, headers=HEADERS)
    time.sleep(2)

    # Verify empty state UI
    screen = requests.get(f"{BASE}/device/screen-summary", params={
        "udid": udid
    }, headers=HEADERS).json()

    assert any("No orders" in el.get("label", "") for el in screen["elements"]), \
        "Empty state message not shown"

    # Clean up mock
    requests.delete(f"{BASE}/proxy/mocks", headers=HEADERS)

def test_order_list_server_error(setup_device):
    udid = setup_device

    # Mock 500 error
    requests.post(f"{BASE}/proxy/mocks", json={
        "pattern": "~d api.example.com & ~u /orders",
        "status_code": 500,
        "body": '{"error": "Internal Server Error"}'
    }, headers=HEADERS)

    requests.post(f"{BASE}/device/app/launch", json={
        "udid": udid, "bundle_id": "com.example.myapp"
    }, headers=HEADERS)
    time.sleep(2)

    requests.post(f"{BASE}/device/ui/tap-element", json={
        "udid": udid, "label": "Orders", "element_type": "Button"
    }, headers=HEADERS)
    time.sleep(2)

    # Verify error state UI
    screen = requests.get(f"{BASE}/device/screen-summary", params={
        "udid": udid
    }, headers=HEADERS).json()

    assert any("error" in el.get("label", "").lower() or "retry" in el.get("label", "").lower()
               for el in screen["elements"]), \
        "No error state or retry button shown"

    requests.delete(f"{BASE}/proxy/mocks", headers=HEADERS)
```

This is illustrative — your agent will generate something tailored to your actual app, endpoints, and UI structure.

## Step 3: Run It

```bash
pytest test_orders.py -v
```

No agent, no tokens, no conversation context. Just your test script talking directly to Quern's HTTP API. Run it after every code change, in CI, on a schedule — whatever you want.

## Step 4: Iterate with the Agent

When a test fails:

> "test_order_list_empty_state is failing — the empty state message isn't appearing. Here's the output. Can you investigate?"

Now your agent earns its tokens. It can:
- Run the scenario interactively to see what's actually happening
- Take a screenshot at the failure point
- Check the logs for errors
- Look at the network traffic
- Inspect your code for the bug

Once fixed:

> "Update the test script to match the new behavior"

## Step 5: Expand the Suite

> "Add tests for: 401 unauthorized (should show login prompt), 429 rate limited (should show retry-after message), network timeout (should show offline state), and malformed JSON (should not crash)"

Your agent adds test cases to the existing script. Each new scenario is a function that sets up a mock, triggers the action, and asserts the expected UI state.

## Why This Matters

| Approach | Tokens per run | Speed | Repeatability |
|---|---|---|---|
| Interactive agent testing | Hundreds of thousands | Minutes | Depends on context |
| Generated test script | Zero | Seconds | Perfectly repeatable |
| Agent investigating failures | Thousands (targeted) | Minutes | N/A |

The expensive part is figuring out *what* to test and *how* — that's where the agent's intelligence is valuable. The cheap part is *running* the tests — that's where a script excels.

## Advanced: Agent-Monitored Test Runs

For the best of both worlds:

> "Run my test suite. If anything fails, investigate the failure — check screenshots, logs, and network traffic — and tell me what went wrong."

Your agent kicks off the test script, watches the output, and when a test fails, it digs in using Quern's tools to diagnose the issue. You get automated testing with intelligent failure analysis.

## Advanced: Remote Test Farm

Your test scripts talk to Quern over HTTP. That HTTP server doesn't have to be on your laptop.

### The Setup

Get a **Mac Mini M4 Pro** (or any Mac you can dedicate to this). The more RAM, the better — 32GB runs 6-8 simulators comfortably, 64GB can handle more. Install Quern on it, start the server, and you have a dedicated test machine.

Your test scripts just need one change:

```python
# Instead of localhost...
BASE = "http://mac-mini.local:9100/api/v1"
```

That's it. Your scripts run on your laptop (or a CI runner, or a cron job, or anywhere) and the simulators, proxy, and UI automation all happen on the Mac Mini. Your laptop stays free for development.

### Parallel Test Execution

This is where it gets exciting. A typical UI automation test suite that takes 2 hours running serially can finish in **20 minutes** spread across 6 simulators running in parallel.

Your test script boots multiple simulators, partitions the test cases across them, and runs them concurrently:

```python
import concurrent.futures

devices = ensure_devices(count=6, type="simulator")

# Partition tests across devices
test_groups = distribute_tests(all_tests, devices)

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    futures = {
        pool.submit(run_test_group, device, tests): device
        for device, tests in test_groups.items()
    }
    results = {device: f.result() for f, device in futures.items()}

generate_report(results)
```

Each simulator gets its own slice of tests, its own proxy traffic (automatically tagged by simulator UDID), its own app state. No interference, no flaky shared state, no "works on device 3 but fails on device 5."

### Why Not a Cloud CI Provider?

Cloud-based iOS CI (Bitrise, CircleCI, Xcode Cloud, etc.) is great for builds. But for UI automation at scale:

- **Parallelism is expensive.** Most providers charge per concurrent machine. Running 6 simulators in parallel means paying for 6 machines. On your own Mac Mini, it's free after the hardware cost.
- **Test sharding is hard or impossible.** Most CI providers run your test suite serially on a single simulator. Some support Xcode's native test parallelism, but that's limited to XCTest and doesn't give you network mocking, state management, or custom UI flows.
- **Network mocking isn't available.** Good luck setting up mitmproxy in a cloud CI environment. With Quern on your own hardware, proxy capture, mocking, and interception work out of the box.
- **You own the environment.** No waiting for provider capacity. No mystery failures from a shared VM image. No "works on my machine but fails in CI." Your Mac Mini is your machine — you control the OS version, Xcode version, simulator runtimes, everything.

### The Math

A Mac Mini M4 Pro with 32GB RAM costs around $1,600 one-time. A cloud CI provider running 6 parallel macOS machines costs $300-600/month. The Mac Mini pays for itself in 3-5 months — and then it's free forever.

And you get something no cloud provider offers: full network interception with mocking on every test run, app state checkpoints for instant test setup, and your AI agent on standby to investigate any failure.

### Build It, Own It

The complete pipeline:

1. **Your agent** writes the test scripts (interactive → script generation)
2. **Your Mac Mini** runs them in parallel across 6+ simulators
3. **Quern** handles device management, proxy, mocking, state, and screenshots
4. **Your CI system** (GitHub Actions, Jenkins, whatever) triggers the run and collects the report
5. **Your agent** investigates failures on demand

No vendor lock-in. No per-minute billing. No test sharding limitations. A dedicated test farm that you own, running scripts that your AI agent wrote, against infrastructure you control.

## Tips

- **Start with the happy path.** Get one solid test working before adding error cases.
- **Save device state checkpoints.** Your script's setup fixture should restore to a known state, not assume the app is fresh. This makes tests independent and repeatable.
- **Use `time.sleep()` sparingly.** Your agent should use `wait_for_element` (via the HTTP API) instead of fixed sleeps where possible. Sleeps are fragile; element waits are reliable.
- **Keep mocks scoped.** Each test should set up its own mocks and clean them up afterward. Leftover mocks from a previous test cause confusing failures.
- **JavaScript works too.** If your team prefers Node.js, ask for a script using `fetch` or `axios` instead of Python `requests`. The HTTP API doesn't care what language calls it.
- **Version control your tests.** These scripts are as valuable as your app code. They encode knowledge about expected behavior that would otherwise live only in your (or your agent's) head.
