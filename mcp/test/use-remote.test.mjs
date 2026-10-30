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
