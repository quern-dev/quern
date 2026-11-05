// Runs against the compiled output: `npm run build && npm test`.
// Sending a project to a remote quern's git sandbox (#608). The push goes to
// a bare repository on disk (a file:// "server"), so git's own behaviour is
// what is tested, and nothing leaves the machine.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createServer as createTlsServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { before, test } from "node:test";

import { capNote, confirmCheckout, fetchPinnedCert, pushConfig, refusalReason, repoName,
  syncProject } from "../dist/sandbox-sync.js";
import { realpathSync, chmodSync } from "node:fs";
import { PinMismatch } from "../dist/remote.js";

const git = (cwd, ...args) => execFileSync("git", args, { cwd, encoding: "utf-8",
  env: { ...process.env, GIT_TERMINAL_PROMPT: "0" } }).trim();

/** A project repo, and a file:// "server" whose sandbox has `<repo>.git`. */
function setup({ commit = true } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "quern-sync-"));
  const project = join(dir, "My App");
  mkdirSync(join(project, "src"), { recursive: true });
  git(project, "init", "-q");
  writeFileSync(join(project, "src", "main.swift"), "print(1)\n");
  writeFileSync(join(project, ".gitignore"), "build/\n");
  if (commit) {
    git(project, "add", "-A");
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c");
  }
  const server = join(dir, "server");
  mkdirSync(join(server, "git"), { recursive: true });
  const repo = repoName(realpathSync(project));
  git(server, "init", "-q", "--bare", join(server, "git", `${repo}.git`));
  return { dir, project, repo, server: { url: `file://${server}`, apiKey: "k-1" },
           bare: join(server, "git", `${repo}.git`) };
}

const tree = (bare, ref = "quern-sync") => git(bare, "ls-tree", "-r", "--name-only", ref).split("\n");

test("the working tree is sent as it is: untracked in, ignored out, and the path to build", async () => {
  const s = setup();
  writeFileSync(join(s.project, "src", "new.swift"), "print(2)\n");     // untracked
  mkdirSync(join(s.project, "build"));
  writeFileSync(join(s.project, "build", "out.o"), "x");                // ignored
  writeFileSync(join(s.project, "src", "main.swift"), "print(3)\n");    // modified
  const out = await syncProject(s.server, join(s.project, "src", "main.swift"));
  assert.match(out.repo, /^My-App-[0-9a-f]{8}$/);
  assert.equal(out.project_path, `sandbox:${out.repo}/quern-sync/src/main.swift`);
  assert.deepEqual(tree(s.bare).sort(), [".gitignore", "src/main.swift", "src/new.swift"]);
  assert.equal(git(s.bare, "show", "quern-sync:src/main.swift"), "print(3)");
  assert.equal(git(s.bare, "rev-parse", "quern-sync"), out.commit);
});

test("nothing in the user's repository changes: index, HEAD, branch, config", async () => {
  const s = setup();
  writeFileSync(join(s.project, "untracked.txt"), "u\n");
  const before = {
    index: createHash("sha256").update(readFileSync(join(s.project, ".git", "index"))).digest("hex"),
    head: git(s.project, "rev-parse", "HEAD"),
    branch: git(s.project, "symbolic-ref", "HEAD"),
    status: git(s.project, "status", "--porcelain"),
    config: readFileSync(join(s.project, ".git", "config"), "utf-8"),
  };
  await syncProject(s.server, s.project);
  assert.equal(createHash("sha256").update(readFileSync(join(s.project, ".git", "index"))).digest("hex"),
               before.index);
  assert.equal(git(s.project, "rev-parse", "HEAD"), before.head);
  assert.equal(git(s.project, "symbolic-ref", "HEAD"), before.branch);
  assert.equal(git(s.project, "status", "--porcelain"), before.status);
  const config = readFileSync(join(s.project, ".git", "config"), "utf-8");
  assert.equal(config, before.config);
  assert.doesNotMatch(config, /extraHeader|k-1/, "the key is never written down");
});

test("a repository with no commits yet is sent whole, with no parent", async () => {
  const s = setup({ commit: false });
  const out = await syncProject(s.server, s.project);
  assert.equal(out.project_path, `sandbox:${s.repo}/quern-sync`);
  assert.deepEqual(tree(s.bare).sort(), [".gitignore", "src/main.swift"]);
  assert.equal(git(s.bare, "rev-list", "--count", "quern-sync"), "1");
});

test("the commit sent has the user's HEAD as its parent", async () => {
  const s = setup();
  const out = await syncProject(s.server, s.project);
  assert.equal(git(s.bare, "rev-parse", `${out.commit}^`), git(s.project, "rev-parse", "HEAD"));
});

test("a project with a submodule is refused before anything is sent", async () => {
  const s = setup();
  const sub = join(s.dir, "sub");
  mkdirSync(sub);
  git(sub, "init", "-q");
  writeFileSync(join(sub, "x"), "x");
  git(sub, "add", "-A");
  git(sub, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s");
  git(s.project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "vendor/sub");
  await assert.rejects(syncProject(s.server, s.project), /has submodules \(vendor\/sub\)/);
  assert.throws(() => git(s.bare, "rev-parse", "--verify", "quern-sync"), "nothing was pushed");
});

test("a project using Git LFS is refused before anything is sent", async () => {
  const s = setup();
  writeFileSync(join(s.project, ".gitattributes"), "*.psd filter=lfs diff=lfs merge=lfs -text\n");
  await assert.rejects(syncProject(s.server, s.project), /uses Git LFS \(\.gitattributes\)/);
  assert.throws(() => git(s.bare, "rev-parse", "--verify", "quern-sync"));
});

test("a project not in a git repository says what to do", async () => {
  const dir = mkdtempSync(join(tmpdir(), "quern-nogit-"));
  await assert.rejects(syncProject({ url: "file:///nowhere", apiKey: "k" }, dir),
                       /is not in a git repository.*git init/);
});

test("repository names are what the server takes, and two Apps are two", () => {
  assert.match(repoName("/x/My App"), /^My-App-[0-9a-f]{8}$/);
  assert.match(repoName("/x/.hidden"), /^hidden-/);
  assert.match(repoName("/x/proj.git"), /^proj-/);
  assert.match(repoName("/x/...."), /^project-/);
  assert.ok(repoName(`/x/${"a".repeat(80)}`).length <= 64);
  assert.notEqual(repoName("/work/a/App"), repoName("/work/b/App"),
                  "two projects of one name share nothing (review)");
  assert.equal(repoName("/work/a/App"), repoName("/work/a/App"), "and one is stable");
});

test("the key and session reach git only through its environment, never its arguments", async () => {
  const s = setup();
  // A git on PATH that records what it was given, then runs the real one.
  const real = execFileSync("sh", ["-c", "command -v git"], { encoding: "utf-8" }).trim();
  const bin = join(s.dir, "bin");
  mkdirSync(bin);
  const log = join(s.dir, "git.log");
  writeFileSync(join(bin, "git"), `#!/bin/sh\n` +
    `printf 'ARGS %s\\n' "$*" >> '${log}'\n` +
    `env | grep '^GIT_CONFIG_' >> '${log}'\n` +
    `exec '${real}' "$@"\n`);
  chmodSync(join(bin, "git"), 0o755);
  const saved = process.env.PATH;
  process.env.PATH = `${bin}:${saved}`;
  try {
    await syncProject(s.server, s.project);
  } finally {
    process.env.PATH = saved;
  }
  const text = readFileSync(log, "utf-8");
  const pushArgs = text.split("\n").filter((l) => l.startsWith("ARGS push"));
  assert.equal(pushArgs.length, 1);
  assert.doesNotMatch(text.split("\n").filter((l) => l.startsWith("ARGS")).join("\n"), /k-1/,
                      "the key is in no git command line");
  assert.match(text, /GIT_CONFIG_VALUE_\d+=Authorization: Bearer k-1/);
  assert.match(text, /GIT_CONFIG_VALUE_\d+=X-Quern-Session: /);
});

test("a pinned push checks the key alone, and only for a pinned server", () => {
  const plain = pushConfig("k-1");
  assert.ok(!Object.values(plain).some((v) => /pinnedPubkey|sslVerify/.test(v)));
  const pinned = pushConfig("k-1", "PIN=");
  const pairs = Object.fromEntries(Array.from({ length: Number(pinned.GIT_CONFIG_COUNT) },
    (_, i) => [pinned[`GIT_CONFIG_KEY_${i}`] + "#" + i, pinned[`GIT_CONFIG_VALUE_${i}`]]));
  const values = (key) => Object.entries(pairs).filter(([k]) => k.startsWith(key + "#"))
    .map(([, v]) => v);
  assert.deepEqual(values("http.pinnedPubkey"), ["sha256//PIN="]);
  assert.deepEqual(values("http.sslVerify"), ["false"],
                   "the name check is off: a tunnel or a new IP is still the pinned server");
  assert.deepEqual(values("credential.helper"), [""]);
  assert.deepEqual(values("http.extraHeader").map((v) => v.split(":")[0]).sort(),
                   ["Authorization", "X-Quern-Session"]);
});

test("a refused push says the server's reason, which git alone does not", async () => {
  const seen = [];
  const srv = createServer((req, res) => {
    seen.push({ url: req.url, auth: req.headers.authorization });
    res.writeHead(403, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: 'git sandboxes are off for client keys: "client_builds"' }));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    const url = `http://127.0.0.1:${srv.address().port}`;
    const why = await refusalReason({ url, apiKey: "k-9" }, `${url}/git/app.git`);
    assert.match(why, /^HTTP 403: git sandboxes are off.*client_builds/);
    assert.equal(seen[0].url, "/git/app.git/info/refs?service=git-receive-pack");
    assert.equal(seen[0].auth, "Bearer k-9");
  } finally {
    srv.close();
  }
});

let certs;
before(() => {
  certs = mkdtempSync(join(tmpdir(), "quern-sync-cert-"));
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
    "-subj", "/CN=127.0.0.1", "-keyout", join(certs, "key.pem"), "-out", join(certs, "cert.pem")],
    { stdio: "ignore" });
});

test("the certificate git trusts is the paired one, and only after checking it", async () => {
  const pem = readFileSync(join(certs, "cert.pem"), "utf-8");
  const der = Buffer.from(pem.replace(/-----[^-]+-----|\s/g, ""), "base64");
  const fp = createHash("sha256").update(der).digest("hex");
  const srv = createTlsServer({ key: readFileSync(join(certs, "key.pem")), cert: pem },
                              (req, res) => res.end());
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    const url = `https://127.0.0.1:${srv.address().port}`;
    const got = await fetchPinnedCert(url, fp);
    // The pin is of the public key, as curl computes it -- not of the
    // certificate, which a looks-like-base64 check could not tell (review).
    const expected = execFileSync("sh", ["-c",
      `openssl x509 -in '${join(certs, "cert.pem")}' -pubkey -noout | ` +
      "openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64"],
      { encoding: "utf-8" }).trim();
    assert.equal(got.spki, expected);
    await assert.rejects(fetchPinnedCert(url, "00".repeat(32)), PinMismatch);
  } finally {
    srv.close();
  }
});

async function listingServer(listing) {
  const srv = createServer((req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(listing));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${srv.address().port}`, close: () => srv.close() };
}

test("a push taken but not checked out is an error saying why", async () => {
  const s = await listingServer({ max_bytes: 2 ** 30, repos: [{ repo: "App-1", bytes: 10,
    branches: [{ branch: "quern-sync", commit: "old" }],
    last_checkout: { branches: [{ branch: "quern-sync", error: "App-1/quern-sync expands to 9 MB" }] } }] });
  try {
    await assert.rejects(confirmCheckout({ url: s.url, apiKey: "k" }, "App-1", "new"),
                         /did not check it out: App-1\/quern-sync expands to 9 MB/);
    await confirmCheckout({ url: s.url, apiKey: "k" }, "App-1", "old");
  } finally {
    s.close();
  }
});

test("a push that ended with nothing said is explained against the cap", async () => {
  const s = await listingServer({ max_bytes: 100 * 2 ** 20,
    repos: [{ repo: "a", bytes: 99 * 2 ** 20, branches: [] }] });
  try {
    assert.match(await capNote({ url: s.url, apiKey: "k" }), /hold 99 MB of their 100 MB cap/);
  } finally {
    s.close();
  }
});

test("the certificate is fetched from an IPv6 address as well as an IPv4 one or a name", async () => {
  // A listener's IPv6 URL is bracketed, and URL.hostname keeps the brackets,
  // which no resolver takes (CodeRabbit).
  const pem = readFileSync(join(certs, "cert.pem"), "utf-8");
  const der = Buffer.from(pem.replace(/-----[^-]+-----|\s/g, ""), "base64");
  const fp = createHash("sha256").update(der).digest("hex");
  const srv = createTlsServer({ key: readFileSync(join(certs, "key.pem")), cert: pem },
                              (req, res) => res.end());
  await new Promise((r) => srv.listen(0, "::", r));
  try {
    const { port } = srv.address();
    const spkis = [];
    for (const host of ["[::1]", "127.0.0.1", "localhost"]) {
      spkis.push((await fetchPinnedCert(`https://${host}:${port}`, fp)).spki);
    }
    assert.equal(new Set(spkis).size, 1);
  } finally {
    srv.close();
  }
});
