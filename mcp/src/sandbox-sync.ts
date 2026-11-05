/** Send a project to a remote quern's git sandbox, to build there (#605,
 * prototype).
 *
 * When build_and_install names a project on this machine and the server is
 * another machine, the project is pushed to the caller's own sandbox on that
 * server (server/sandboxes.py) and built from its checkout there. The agent
 * names its local path as usual; this does the rest.
 *
 * What is pushed is the working tree as it is -- untracked files included,
 * ignored ones not -- as a commit made with a temporary index: no stash, no
 * branch change, nothing written to the user's repository but the objects.
 * The credentials go on the one git command as -c settings, never into the
 * repository's config: the key as a header, and TLS pinned to the remote's
 * certificate, fetched here and checked against the paired fingerprint first.
 */

import { execFile } from "node:child_process";
import { createHash, X509Certificate } from "node:crypto";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { isIP } from "node:net";
import { tmpdir } from "node:os";
import { basename, join, relative, resolve } from "node:path";
import { connect } from "node:tls";

import { quernHeaders, serverFetch } from "./http.js";
import { PinMismatch } from "./remote.js";

/** The branch every synced snapshot goes to: one checkout per repository, at
 * a stable path, so builds there stay incremental. */
export const SYNC_BRANCH = "quern-sync";

interface Server {
  url: string;
  apiKey: string;
  fingerprint?: string;
}

export interface Synced {
  /** What to build: `sandbox:<repo>/quern-sync/<path in the repo>`. */
  project_path: string;
  repo: string;
  commit: string;
}

function git(args: string[], opts: { cwd?: string; env?: Record<string, string> } = {}):
    Promise<string> {
  return new Promise((ok, no) => execFile("git", args, {
    cwd: opts.cwd, maxBuffer: 16 * 1024 * 1024, timeout: 15 * 60_000,
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0", ...(opts.env ?? {}) },
  }, (err, stdout, stderr) => {
    if (err) {
      const why = String(stderr || err.message).trim();
      no(new Error((err as NodeJS.ErrnoException).code === "ENOENT"
        ? "git is not installed on this machine, so the project cannot be sent to the server"
        : `git ${args.find((a, i) => !a.startsWith("-") && args[i - 1] !== "-c" &&
                                   args[i - 1] !== "-C") ?? ""} failed: ${why}`));
      return;
    }
    ok(String(stdout).trim());
  }));
}

/** The SHA-256 of the remote's public key, in the form curl's pinnedPubkey
 * takes -- after checking its certificate is the one paired with. */
export function fetchPinnedCert(url: string, fingerprint: string):
    Promise<{ spki: string }> {
  const u = new URL(url);
  // An IPv6 hostname keeps its brackets in a URL, which no resolver takes;
  // and an IP address is not a server name for SNI (CodeRabbit).
  const host = u.hostname.replace(/^\[(.*)\]$/, "$1");
  return new Promise((ok, no) => {
    const socket = connect({ host, port: Number(u.port || 443),
                             ...(isIP(host) ? {} : { servername: host }),
                             rejectUnauthorized: false }, () => {
      const raw = socket.getPeerCertificate(true)?.raw;
      socket.end();
      if (!raw) return no(new Error(`${u.host} presented no certificate`));
      const presented = createHash("sha256").update(raw).digest("hex");
      if (presented !== fingerprint) return no(new PinMismatch(fingerprint, presented));
      const cert = new X509Certificate(raw);
      const spkiDer = cert.publicKey.export({ type: "spki", format: "der" });
      ok({ spki: createHash("sha256").update(spkiDer).digest("base64") });
    });
    socket.setTimeout(10_000, () => socket.destroy(new Error(`${u.host}: TLS timed out`)));
    socket.on("error", no);
  });
}

/** A repository name the server takes: the checkout's folder name, and a
 * short hash of where it is, so two projects both called App -- or two
 * worktrees of one -- never share a sandbox, a checkout or its build
 * (review). Stable for one checkout, so its builds stay incremental. */
export function repoName(root: string): string {
  const name = basename(root).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[._-]+/, "")
    .replace(/\.git$/, "").slice(0, 55) || "project";
  return `${name}-${createHash("sha256").update(root).digest("hex").slice(0, 8)}`;
}

/** git configuration for the push, as environment variables rather than
 * `-c` arguments: arguments are in `ps` for anyone on the machine, and in
 * the message of a git that fails without saying why; this machine's
 * environment is not (review). The key and session are every quern
 * request's (http.ts). A pinned remote is checked by its public key alone:
 * its certificate names the addresses it was made for, and a tunnel or a
 * new IP is still that server (review). Needs git 2.31 or later. */
export function pushConfig(apiKey: string, spki?: string): Record<string, string> {
  const pairs: [string, string][] = [
    ["credential.helper", ""],
    ...Object.entries(quernHeaders(apiKey))
      .map(([name, value]): [string, string] => ["http.extraHeader", `${name}: ${value}`]),
  ];
  if (spki) pairs.push(["http.sslVerify", "false"], ["http.pinnedPubkey", `sha256//${spki}`]);
  const env: Record<string, string> = { GIT_CONFIG_COUNT: String(pairs.length) };
  pairs.forEach(([key, value], i) => {
    env[`GIT_CONFIG_KEY_${i}`] = key;
    env[`GIT_CONFIG_VALUE_${i}`] = value;
  });
  return env;
}

/** Push the working tree holding `projectPath` to the server's sandbox and say
 * what to build there. */
export async function syncProject(server: Server, projectPath: string): Promise<Synced> {
  // The real path: git names the top level by it, so a project reached
  // through a symlink (macOS's /var is one) would otherwise come out as
  // ../../.. relative to its own repository.
  let path: string;
  try {
    path = realpathSync(resolve(projectPath));
  } catch (e) {
    throw new Error(`${projectPath} could not be read (${(e as Error).message})`);
  }
  let root: string;
  try {
    root = await git(["-C", path, "rev-parse", "--show-toplevel"]);
  } catch {
    try {
      root = await git(["-C", join(path, ".."), "rev-parse", "--show-toplevel"]);
    } catch {
      throw new Error(`${projectPath} is not in a git repository: on a remote quern the project ` +
        "is sent with git, so put it in one (git init) and try again");
    }
  }
  const tmp = mkdtempSync(join(tmpdir(), "quern-sync-"));
  try {
    // The working tree as a commit, through an index of our own.
    const env = { GIT_INDEX_FILE: join(tmp, "index") };
    let parent: string | undefined;
    try {
      parent = await git(["rev-parse", "--verify", "-q", "HEAD"], { cwd: root });
      await git(["read-tree", "HEAD"], { cwd: root, env });
    } catch {
      parent = undefined;     // an unborn branch: nothing to start from
    }
    await git(["add", "-A"], { cwd: root, env });
    const staged = await git(["ls-files", "-s"], { cwd: root, env });
    refuseWhatCannotBeSent(staged);
    // LFS: a .gitattributes in the snapshot that sends files through the lfs
    // filter means the push would carry only pointers.
    for (const line of staged.split("\n")) {
      const [meta, file] = line.split("\t");
      if (!file || !/(^|\/)\.gitattributes$/.test(file)) continue;
      const blob = meta.split(" ")[1];
      if (/filter=lfs/.test(await git(["cat-file", "-p", blob], { cwd: root }))) {
        throw new Error(`this project uses Git LFS (${file}), whose files a push to a remote ` +
          "quern's sandbox would not carry: build it on the server's own disk instead");
      }
    }
    const tree = await git(["write-tree"], { cwd: root, env });
    const commit = await git(["-c", "user.name=quern", "-c", "user.email=quern@localhost",
      "commit-tree", tree, ...(parent ? ["-p", parent] : []), "-m", "quern sync"],
      { cwd: root, env });

    const repo = repoName(root);
    const spki = server.fingerprint
      ? (await fetchPinnedCert(server.url, server.fingerprint)).spki : undefined;
    const remote = `${server.url.replace(/\/+$/, "")}/git/${repo}.git`;
    try {
      await git(["push", "--quiet", "--force", remote, `${commit}:refs/heads/${SYNC_BRANCH}`],
                { cwd: root, env: pushConfig(server.apiKey, spki) });
    } catch (e) {
      // git says only "returned error: 403", or that the remote hung up when
      // a push was cut off at the cap; the server says why.
      const why = await refusalReason(server, remote) ?? await capNote(server);
      throw why ? new Error(`the server refused the push: ${why}`) : e;
    }
    // A push can be taken and its checkout still refused -- it expanded past
    // the cap -- so the outcome is read back, not assumed.
    await confirmCheckout(server, repo, commit);
    const inRepo = relative(root, path).split(/[\\/]/).filter(Boolean).join("/");
    return { project_path: `sandbox:${repo}/${SYNC_BRANCH}${inRepo ? `/${inRepo}` : ""}`,
             repo, commit };
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

/** Refuse, before sending anything, a tree the sandbox cannot build as it is:
 * submodules, which it does not fetch, and LFS pointers, whose objects a plain
 * push does not carry. `ls-files -s` lists mode 160000 for a submodule. */
export function refuseWhatCannotBeSent(staged: string): void {
  const submodules = staged.split("\n").filter((l) => l.startsWith("160000 "))
    .map((l) => l.split("\t")[1]);
  if (submodules.length) {
    throw new Error(`this project has submodules (${submodules.slice(0, 5).join(", ")}), which ` +
      "a remote quern's sandbox does not fetch: build it on the server's own disk instead");
  }
}

/** Why the server refused a push to `remote`, in its own words, or undefined.
 * A GET of info/refs is answered the way the push was -- 403, 413, 507 --
 * with the reason as JSON `detail`. */
export async function refusalReason(server: Server, remote: string): Promise<string | undefined> {
  try {
    const resp = await serverFetch(server, `${remote}/info/refs?service=git-receive-pack`,
      { headers: quernHeaders(server.apiKey), signal: AbortSignal.timeout(10_000) });
    if (resp.ok) return undefined;
    const body = (await resp.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = body?.detail;
    return `HTTP ${resp.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`;
  } catch {
    return undefined;
  }
}

interface SandboxListing {
  repos?: { repo: string; bytes: number; branches: { branch: string; commit: string }[];
            last_checkout?: { branches?: { branch?: string; error?: string }[] } | null }[];
  max_bytes?: number;
}

async function listing(server: Server): Promise<SandboxListing | undefined> {
  try {
    const resp = await serverFetch(server, `${server.url.replace(/\/+$/, "")}/api/v1/sandboxes`,
      { headers: quernHeaders(server.apiKey), signal: AbortSignal.timeout(10_000) });
    return resp.ok ? (await resp.json()) as SandboxListing : undefined;
  } catch {
    return undefined;
  }
}

/** What the key's sandboxes hold against their cap, when a push failed with
 * nothing more said: a push cut off at the cap ends as "the remote end hung
 * up", and by then nothing of it is held to explain it (review). */
export async function capNote(server: Server): Promise<string | undefined> {
  const got = await listing(server);
  if (!got?.max_bytes || !got.repos) return undefined;
  const held = got.repos.reduce((n, r) => n + r.bytes, 0);
  return `the push did not complete; this key's sandboxes hold ${Math.round(held / 2 ** 20)} ` +
    `MB of their ${Math.round(got.max_bytes / 2 ** 20)} MB cap (sandboxes.max_mb), and a push ` +
    "that would pass it is cut off";
}

/** That the server checked out what was pushed, or why it did not. */
export async function confirmCheckout(server: Server, repo: string, commit: string):
    Promise<void> {
  const got = await listing(server);
  const entry = got?.repos?.find((r) => r.repo === repo);
  if (!entry) return;     // could not be asked: the build says what it finds
  if (entry.branches.some((b) => b.branch === SYNC_BRANCH && b.commit === commit)) return;
  const why = entry.last_checkout?.branches?.find((b) => b.branch === SYNC_BRANCH)?.error;
  throw new Error(`the project was sent, but the server did not check it out` +
    (why ? `: ${why}` : ""));
}
