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
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, relative, resolve } from "node:path";
import { connect } from "node:tls";

import { SESSION_ID } from "./http.js";
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

/** The remote's certificate as PEM, and the SHA-256 of its public key in the
 * form curl's pinnedPubkey takes -- after checking the certificate is the one
 * paired with. */
export function fetchPinnedCert(url: string, fingerprint: string):
    Promise<{ pem: string; spki: string }> {
  const u = new URL(url);
  return new Promise((ok, no) => {
    const socket = connect({ host: u.hostname, port: Number(u.port || 443),
                             servername: u.hostname, rejectUnauthorized: false }, () => {
      const raw = socket.getPeerCertificate(true)?.raw;
      socket.end();
      if (!raw) return no(new Error(`${u.host} presented no certificate`));
      const presented = createHash("sha256").update(raw).digest("hex");
      if (presented !== fingerprint) return no(new PinMismatch(fingerprint, presented));
      const cert = new X509Certificate(raw);
      const spkiDer = cert.publicKey.export({ type: "spki", format: "der" });
      ok({ pem: cert.toString(), spki: createHash("sha256").update(spkiDer).digest("base64") });
    });
    socket.setTimeout(10_000, () => socket.destroy(new Error(`${u.host}: TLS timed out`)));
    socket.on("error", no);
  });
}

/** A repository name the server takes: what the checkout's folder is called. */
export function repoName(root: string): string {
  const name = basename(root).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[._-]+/, "")
    .replace(/\.git$/, "").slice(0, 64);
  return name || "project";
}

/** Push the working tree holding `projectPath` to the server's sandbox and say
 * what to build there. */
export async function syncProject(server: Server, projectPath: string): Promise<Synced> {
  const path = resolve(projectPath);
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
    const tree = await git(["write-tree"], { cwd: root, env });
    const commit = await git(["-c", "user.name=quern", "-c", "user.email=quern@localhost",
      "commit-tree", tree, ...(parent ? ["-p", parent] : []), "-m", "quern sync"],
      { cwd: root, env });

    const repo = repoName(root);
    const settings = [
      "-c", "credential.helper=",
      "-c", `http.extraHeader=Authorization: Bearer ${server.apiKey}`,
      "-c", `http.extraHeader=X-Quern-Session: ${SESSION_ID}`,
    ];
    if (server.fingerprint) {
      const cert = await fetchPinnedCert(server.url, server.fingerprint);
      writeFileSync(join(tmp, "server.pem"), cert.pem);
      settings.push("-c", `http.sslCAInfo=${join(tmp, "server.pem")}`,
                    "-c", `http.pinnedPubkey=sha256//${cert.spki}`);
    }
    await git([...settings, "push", "--quiet", "--force",
               `${server.url.replace(/\/+$/, "")}/git/${repo}.git`,
               `${commit}:refs/heads/${SYNC_BRANCH}`], { cwd: root });
    const inRepo = relative(root, path).split(/[\\/]/).filter(Boolean).join("/");
    return { project_path: `sandbox:${repo}/${SYNC_BRANCH}${inRepo ? `/${inRepo}` : ""}`,
             repo, commit };
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}
