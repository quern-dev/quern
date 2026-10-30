// Runs against the compiled output: `npm run build && npm test`.
// use_remote end to end against fake servers (use-remote-scenarios.mjs), one
// process per scenario: the server selection and held session are per process.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, test } from "node:test";

let certs;
before(() => {
  certs = mkdtempSync(join(tmpdir(), "quern-ur-cert-"));
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
    "-subj", "/CN=127.0.0.1", "-keyout", join(certs, "key.pem"), "-out", join(certs, "cert.pem")],
    { stdio: "ignore" });
});

function run(scenario) {
  const script = new URL("./use-remote-scenarios.mjs", import.meta.url).pathname;
  return new Promise((resolve, reject) => {
    execFile(process.execPath, [script, scenario, certs], { timeout: 60_000 }, (err, stdout, stderr) => {
      const line = String(stdout).trim().split("\n").pop();
      try {
        resolve(JSON.parse(line));
      } catch {
        reject(new Error(`${scenario}: ${err?.message ?? ""}\n${stdout}\n${stderr}`));
      }
    });
  });
}

test("a switch moves requests and the held session, and drops the old server's notices", async () => {
  const r = await run("success");
  assert.equal(r.startState, "live");
  assert.equal(r.error, false, r.text);
  assert.equal(r.current, "romulus");
  assert.equal(r.next, "romulus");
  assert.equal(r.heldState, "live");
  assert.ok(r.logs.romulus.includes("WS /api/v1/sessions/agent-1/live t-romulus"), "held there");
  assert.deepEqual(r.notices, [], "a notice about local is not delivered as romulus's");
});

test("a call made while the target is being checked stays where it was", async () => {
  const r = await run("window");
  assert.equal(r.concurrent, "local", "the target is asked, not selected, until the switch is sure");
  assert.equal(r.error, true);
  assert.match(r.text, /romulus did not answer .*still using local/);
  assert.equal(r.current, "local");
  assert.ok(!r.logs.romulus.includes("GET /api/v1/proxy/status"));
});

test("two switches at once run in turn and end where the last one asked", async () => {
  // romulus answers slowly, deimos at once: run together, deimos was switched
  // to first and romulus's late switch won -- or each reported its own server
  // while the other's held session stood.
  const r = await run("two-switches");
  assert.equal(r.a.error, false, r.a.text);
  assert.equal(r.b.error, false, r.b.text);
  assert.equal(r.current, "deimos");
  assert.equal(r.next, "deimos", "requests and the held session are on the same server");
  assert.ok(r.logs.deimos.includes("WS /api/v1/sessions/agent-1/live t-deimos"));
});

test("a server where no session can be held is not switched to", async () => {
  const r = await run("open-fails");
  assert.equal(r.error, true);
  assert.match(r.text, /no session could be held there, so this session is back on local\./);
  assert.equal(r.current, "local");
  assert.equal(r.next, "local");
  assert.equal(r.heldState, "live", "held on local again");
});

test("holdings that cannot be read refuse the switch unless release is given", async () => {
  const r = await run("held-unknown");
  assert.equal(r.refused.error, true);
  assert.match(r.refused.text, /Could not tell whether you hold devices on local \(HTTP 503/);
  assert.equal(r.currentAfterRefusal, "local");
  assert.equal(r.error, false, r.text);
  assert.equal(r.current, "romulus");
});

test("held devices refuse the switch; release=true releases them and switches", async () => {
  const r = await run("holding");
  assert.match(r.refused.text, /You hold SIM-1 on local/);
  assert.equal(r.currentAfterRefusal, "local");
  assert.ok(r.logs.local.includes("DELETE /api/v1/sessions/agent-1/reservations"));
  assert.deepEqual(JSON.parse(r.text).released, ["SIM-1"]);
  assert.equal(r.current, "romulus");
});

test("the hint: only for a pool shortage, and only where count and booting allow", async () => {
  const r = await run("hint");
  assert.match(r.noMatch.text, /Not enough here, but romulus has 1 free that match: iPhone 15/);
  assert.match(r.noMatch.text, /use_remote\(name="romulus"\)/);
  assert.doesNotMatch(r.tooMany.text, /romulus/, "one match is not two");
  assert.doesNotMatch(r.bootedOnly.text, /romulus/, "shut down, and booting is off");
  assert.doesNotMatch(r.notLive.text, /romulus/, "a session that is not live is not fixed elsewhere");
  assert.match(r.listEmpty.elsewhere, /^None here, but romulus has 1 free that match/,
               "an empty filtered list_devices says where to look too");
  assert.match(r.listEmpty.elsewhere, /then list_devices again\.$/, "a listing is told to list, not reserve");
  assert.match(r.noMatch.text, /then reserve_devices again\.$/);
  assert.equal(r.listByState.elsewhere, undefined, "a filter the hint cannot check: no hint");
  assert.equal(r.listByModel.elsewhere, undefined, "list_devices matches names only, so a model does not");
  assert.match(r.listByName.elsewhere ?? "", /romulus/);
  assert.equal(r.listUnfiltered.elsewhere, undefined, "nothing asked for, nothing to point at");
});

for (const scenario of ["stale-start", "stale-early"]) {
  test(`an open still in flight to the old server never reaches the new one (${scenario})`, async () => {
    const r = await run(scenario);
    assert.equal(r.error, false, r.text);
    assert.equal(r.current, "romulus");
    const sockets = r.logs.romulus.filter((l) => l.startsWith("WS "));
    assert.ok(sockets.length >= 1);
    assert.deepEqual(sockets.filter((l) => !l.endsWith(" t-romulus")), [],
                     "every socket presents romulus's own token, never local's");
    assert.equal(r.heldState, "live");
  });
}

test("a reservation made during a switch waits for it, and lands on the new server", async () => {
  // Made on the old server after the switch checked what was held there, the
  // switch's clean close released it unreported (CodeRabbit).
  const r = await run("reserve-during-switch");
  assert.equal(r.error, false, r.text);
  assert.equal(r.current, "romulus");
  assert.ok(r.logs.romulus.includes("POST /api/v1/sessions/agent-1/reservations"));
  assert.ok(!r.logs.local.includes("POST /api/v1/sessions/agent-1/reservations"));
});

test("a switch moves the agent's mocks and capture, and its next reservation sets them up", async () => {
  const r = await run("carry");
  const s = r.switched;
  assert.equal(s.current, "romulus");
  // Off the old server: removed, or already gone (404); one that could not be
  // removed is said to still answer there.
  for (const id of ["mock_local1", "mock_local2", "mock_local3"]) {
    assert.ok(r.logs.local.includes(`DELETE /api/v1/proxy/mocks/${id}`), id);
  }
  assert.deepEqual(s.carried.still_on_old, ["mock_local3"]);
  assert.match(s.note, /mock_local3 from local: they still answer traffic there/);
  assert.equal(s.carried.mocks, 3);
  assert.equal(s.carried.session_capture, 1);
  assert.deepEqual(s.knowledge, { kind: "session_loaded", app: "com.example" });
  assert.equal(s.proxy.status, "running");

  // A reservation with no simulator sets up nothing, and says they wait.
  assert.deepEqual(r.noSimulator.carried.still_waiting, { mocks: 3, session_capture: 1 });

  // Two simulators: each mock once per simulator (a client key may not mock
  // every device), as last updated, and the capture on each.
  const made = r.reserved.carried.mocks_recreated;
  assert.equal(made.length, 6);
  const posted = r.bodies.romulus.filter((b) => b.path === "POST /api/v1/proxy/mocks").map((b) => b.body);
  assert.equal(posted.length, 6, "all from this reservation: none before it, none after");
  assert.deepEqual(posted.slice(0, 2).map((b) => [b.pattern, b.simulator_udid, b.response.body]),
                   [["~u /a", "SIM-R1", "A2"], ["~u /a", "SIM-R2", "A2"]]);
  assert.equal(posted[2].response.status_code, 500);
  const captured = r.bodies.romulus.filter((b) => b.path === "POST /api/v1/proxy/session-capture");
  assert.deepEqual(captured.map((b) => [b.body.simulator_udid, b.body.processes]),
                   [["SIM-R1", ["Metatext"]], ["SIM-R2", ["Metatext"]]]);
  assert.equal(r.reserved.carried.still_waiting, undefined);
  // Set up once: a later reservation carries nothing more.
  assert.equal(r.again.carried, undefined);
});

test("what could not be set up waits for the next reservation; clear_mocks drops it", async () => {
  const r = await run("carry-retry");
  assert.equal(r.switched.proxy.status, "stopped");
  assert.match(r.switched.proxy.note, /proxy is stopped/);
  assert.match(r.down.carried.failed[0].error, /HTTP 503/);
  assert.deepEqual(r.down.carried.still_waiting, { mocks: 1, session_capture: 0 });
  assert.equal(r.up.carried.mocks_recreated.length, 1);
  assert.equal(r.toDeimos.carried.mocks, 1, "moved on with the agent");
  assert.equal(r.afterClear.carried, undefined, "cleared while waiting: nothing comes back");
});

test("the record follows the server: one entry per mock, replaced responses, gone is gone", async () => {
  const r = await run("carry-hops");
  // A rule the server reports gone is forgotten, and the 404 still reaches the caller.
  assert.equal(r.clearGone.error, true);
  // Two reservations at once: each simulator gets the mock once (review: twice).
  assert.equal(r.romulusPosts, 2);
  const made = r.parallel.flatMap((c) => c?.mocks_recreated ?? []);
  assert.deepEqual(made.map((m) => m.simulator_udid).sort(), ["SIM-R1", "SIM-R2"]);
  // The next hop moves one mock, not one per copy (review: they multiplied).
  assert.equal(r.toDeimos.carried.mocks, 1);
  // update_mock replaced the whole response, as the server does: the status and
  // header set before it were reset to the server's defaults (review).
  assert.deepEqual(r.mockBodies[0].response,
                   { status_code: 200, headers: { "content-type": "application/json" }, body: "B" });
  // A client key's release_devices ends its scoped mocks on the server; they
  // do not come back at the next switch (review).
  assert.equal(r.afterRelease.carried, undefined);
});

test("a mock set up on one simulator of two is retried on the other only", async () => {
  const r = await run("carry-partial");
  assert.deepEqual(r.first.mocks_recreated.map((m) => m.simulator_udid), ["SIM-R1"]);
  assert.equal(r.first.failed[0].simulator_udid, "SIM-R2");
  assert.deepEqual(r.first.still_waiting, { mocks: 1, session_capture: 0 });
  assert.deepEqual(r.second.mocks_recreated.map((m) => m.simulator_udid), ["SIM-R2"],
                   "SIM-R1 already has it: not a second rule there (review)");
  assert.equal(r.second.still_waiting, undefined);
  assert.equal(r.posts.filter((u) => u === "SIM-R1").length, 1);
});
