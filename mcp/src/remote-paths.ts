/** Paths, when this machine's disk is not the server's (#254, phase 3b).
 *
 * A path the MCP sends is read on the server's disk. That is only the same
 * file when the two share a disk -- told by /health's `host_id`, a random id
 * the server keeps in ~/.quern/host-id, against this machine's own -- and a
 * client key may not name server paths at all. So for either, an app to
 * install is sent to the server instead (`uploadApp`), and paths in answers
 * are the server's, not this machine's.
 */

import { createReadStream } from "node:fs";
import { lstat, readFile, readdir, readlink, realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { pipeline, Readable } from "node:stream";
import { createGzip } from "node:zlib";

import { quernHeaders, serverFetch } from "./http.js";

interface Server {
  url: string;
  apiKey: string;
  fingerprint?: string;
}

/** Client keys are issued as `qc_...` (server/clients.py); the owner's is not. */
export function isClientKey(apiKey: string): boolean {
  return apiKey.startsWith("qc_");
}

// Where a server on this machine keeps it: QUERN_STATE_DIR moves the whole
// of ~/.quern for it, so a scratch server run that way is not "another
// machine" to an MCP given the same variable (review).
function hostIdFile(): string {
  return join(process.env.QUERN_STATE_DIR || join(homedir(), ".quern"), "host-id");
}
const shared = new Map<string, Promise<boolean>>();

/** Whether the server at `server.url` reads the same disk as this process.
 * Asked once per server. /health is read first: on this machine, that is
 * what makes the server write its host-id, which is then here to compare. A
 * server that cannot say reads as not shared -- the side on which a path is
 * sent as data rather than trusted to mean the same file. */
export function sharesDisk(server: Server): Promise<boolean> {
  let answer = shared.get(server.url);
  if (!answer) {
    // A real answer either way is kept; only a server that could not be
    // asked is asked again. Forgetting every "not shared" made each remote
    // result that held a path cost a /health round trip (review).
    let keep = true;
    answer = (async () => {
      let theirs: unknown;
      try {
        const resp = await serverFetch(server, `${server.url}/health`, {
          signal: AbortSignal.timeout(5000),
        });
        theirs = ((await resp.json()) as { host_id?: unknown }).host_id;
      } catch {
        keep = false;
        return false;
      }
      if (typeof theirs !== "string" || !/^[0-9a-f]{32}$/.test(theirs)) return false;
      try {
        return (await readFile(hostIdFile(), "utf-8")).trim() === theirs;
      } catch {
        return false;      // no host-id here: no server has run on this disk
      }
    })();
    shared.set(server.url, answer);
    answer.then(() => { if (!keep) shared.delete(server.url); });
  }
  return answer;
}

/** Whether an app at a path on this machine must be sent, not named. */
export async function mustUpload(server: Server): Promise<boolean> {
  return isClientKey(server.apiKey) || !(await sharesDisk(server));
}

/** Whether paths in the server's answers are on another disk. */
export async function pathsAreRemote(server: Server): Promise<boolean> {
  return !(await sharesDisk(server));
}

// -- uploading an app --------------------------------------------------------

export interface Uploaded {
  upload_id: string;
  name: string;
  kind: string;
  bytes: number;
  expires_in: number;
}

/** Send the app at `appPath` (on this machine) to the server: an .ipa or
 * .apk as it is, a .app bundle as a gzipped tar. Streamed, never held in
 * memory. Throws, saying what to do, for anything that is not one. */
export async function uploadApp(server: Server, appPath: string): Promise<Uploaded> {
  let info;
  try {
    info = await stat(appPath);
  } catch {
    throw new Error(`${appPath} is not on this machine: install_app sends the app from ` +
      "the machine this MCP runs on to the quern server, so app_path must be a local path");
  }
  // A symlinked bundle (a build system's output link) is sent as the bundle
  // it points at, not as one symlink the server would refuse (review).
  const real = await realpath(appPath);
  const base = basename(appPath.replace(/\/+$/, ""));
  let name: string;
  let body: Readable;
  const headers: Record<string, string> = {
    ...quernHeaders(server.apiKey), "Content-Type": "application/octet-stream",
  };
  if (info.isDirectory() && base.endsWith(".app")) {
    name = `${base}.tar.gz`;
    // pipeline, not pipe: pipe does not pass a source's error on, and an
    // unreadable file in the bundle was an uncaught exception that ended the
    // MCP (review). Here it fails the gzip stream, and so the upload.
    const gzip = createGzip();
    pipeline(Readable.from(tarTree(real, base)), gzip, () => {});
    body = gzip;
  } else if (info.isFile() && (base.endsWith(".ipa") || base.endsWith(".apk"))) {
    name = base;
    body = createReadStream(real);
    headers["Content-Length"] = String(info.size);
  } else {
    throw new Error(`${appPath} is not an app: give a .app bundle, an .ipa or an .apk`);
  }
  const url = `${server.url}/api/v1/uploads?name=${encodeURIComponent(name)}`;
  const resp = await serverFetch(server, url, {
    method: "POST", headers, body: body as unknown as BodyInit,
    duplex: "half",
  } as RequestInit, { noTimeouts: true });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${text}`);
  return JSON.parse(text) as Uploaded;
}

// -- a tar of a directory, for a .app bundle ---------------------------------

const BLOCK = 512;

function octal(value: number, width: number): string {
  return value.toString(8).padStart(width - 1, "0") + "\0";
}

function header(name: string, type: string, size: number, mode: number, mtime: number,
                linkname = ""): Buffer {
  const h = Buffer.alloc(BLOCK);
  h.write(name, 0, 100, "utf-8");
  h.write(octal(mode & 0o7777, 8), 100, "ascii");
  h.write(octal(0, 8), 108, "ascii");
  h.write(octal(0, 8), 116, "ascii");
  h.write(octal(size, 12), 124, "ascii");
  h.write(octal(mtime, 12), 136, "ascii");
  h.write("        ", 148, "ascii");
  h.write(type, 156, "ascii");
  h.write(linkname, 157, 100, "utf-8");
  h.write("ustar\0", 257, "ascii");
  h.write("00", 263, "ascii");
  let sum = 0;
  for (const byte of h) sum += byte;
  h.write(sum.toString(8).padStart(6, "0") + "\0 ", 148, "ascii");
  return h;
}

/** A PAX record, `<len> key=value\n`, whose length counts itself. */
function paxRecord(key: string, value: string): string {
  const body = ` ${key}=${value}\n`;
  let len = Buffer.byteLength(body);
  len += String(len + String(len).length).length;
  return `${len}${body}`;
}

function* entry(name: string, type: string, size: number, mode: number, mtime: number,
                linkname = ""): Generator<Buffer> {
  // Names past ustar's 100 bytes, or not ASCII, go in a PAX header first.
  const plain = (s: string) => Buffer.byteLength(s) <= 100 && /^[\x20-\x7e]*$/.test(s);
  if (!plain(name) || !plain(linkname)) {
    let records = "";
    if (!plain(name)) records += paxRecord("path", name);
    if (!plain(linkname)) records += paxRecord("linkpath", linkname);
    const data = Buffer.from(records, "utf-8");
    yield header("PaxHeader", "x", data.length, 0o644, mtime);
    yield* padded(data);
    name = plain(name) ? name : name.slice(0, 99);
    linkname = plain(linkname) ? linkname : "";
  }
  yield header(name, type, size, mode, mtime, linkname);
}

function* padded(data: Buffer): Generator<Buffer> {
  yield data;
  const rest = data.length % BLOCK;
  if (rest) yield Buffer.alloc(BLOCK - rest);
}

/** The tar stream of the tree at `root`, named `top/...` inside: files with
 * their permission bits (an app's executable needs its own), symlinks as
 * symlinks (a framework's `Versions/Current` is one), in a stable order. */
export async function* tarTree(
  root: string, top: string,
  /** Which paths to include, relative to `root` ("" is the root itself).
   * With a filter, links are followed and sent as what they point at (#560). */
  include?: (rel: string, isDir: boolean) => boolean,
): AsyncGenerator<Buffer> {
  async function* walk(abs: string, rel: string, under: string): AsyncGenerator<Buffer> {
    let st;
    if (include) {
      // Links followed: a knowledge base is the user's own files, and a
      // linked `.quern/knowledge` or `screens/` sent nothing (review). What is
      // sent is plain files and folders, which is all the server takes.
      try {
        st = await stat(abs);
      } catch {
        return;                                   // a broken link: nothing to send
      }
      if (!include(under, st.isDirectory())) return;
    } else {
      st = await lstat(abs);
    }
    const mtime = Math.floor(st.mtimeMs / 1000);
    if (st.isSymbolicLink()) {
      yield* entry(rel, "2", 0, st.mode, mtime, await readlink(abs));
    } else if (st.isDirectory()) {
      yield* entry(`${rel}/`, "5", 0, st.mode, mtime);
      for (const child of (await readdir(abs)).sort()) {
        yield* walk(join(abs, child), `${rel}/${child}`, under ? `${under}/${child}` : child);
      }
    } else if (st.isFile()) {
      yield* entry(rel, "0", st.size, st.mode, mtime);
      let written = 0;
      for await (const chunk of createReadStream(abs, { end: Math.max(st.size - 1, 0) })) {
        const buf = chunk as Buffer;
        written += buf.length;
        yield buf;
      }
      if (written !== st.size) {
        throw new Error(`${abs} changed size while it was being sent; try again`);
      }
      const rest = st.size % BLOCK;
      if (rest) yield Buffer.alloc(BLOCK - rest);
    }
    // Sockets, devices and fifos have no place in an app, and are skipped.
  }
  yield* walk(root, top, "");
  yield Buffer.alloc(BLOCK * 2);
}

/** What of a project's `.quern/knowledge` is sent (#560): `app.md` and the
 * screen files, which is all the server's loader reads today. One place, so
 * screenshots for visual diffs can be added here when the server reads them. */
export function knowledgeIncluded(rel: string, isDir: boolean): boolean {
  if (rel === "") return true;
  if (isDir) return rel === "screens";
  return rel === "app.md" || /^screens\/[^/]+\.md$/.test(rel);
}

/** Send a project's knowledge base to the server, for load_landmarks
 * (upload_id) to load for this session (#560). */
export async function uploadKnowledge(server: Server, knowledgeDir: string,
                                      app: string): Promise<Uploaded> {
  const gzip = createGzip();
  pipeline(Readable.from(tarTree(knowledgeDir, "knowledge", knowledgeIncluded)), gzip,
           () => {});
  const name = `${app.replace(/[^A-Za-z0-9._-]/g, "_")}.knowledge.tar.gz`;
  const resp = await serverFetch(server,
    `${server.url}/api/v1/uploads?name=${encodeURIComponent(name)}`, {
      method: "POST",
      headers: { ...quernHeaders(server.apiKey), "Content-Type": "application/octet-stream" },
      body: gzip as unknown as BodyInit,
      duplex: "half",
    } as RequestInit, { noTimeouts: true });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${text}`);
  return JSON.parse(text) as Uploaded;
}

// -- paths in answers ----------------------------------------------------------

/** Screenshots the server saved for an answer (server/screenshots.py, the
 * timeline): fetched and returned inline when its disk is not this one. */
const SHOT = /\/tmp\/quern\/(?:screenshots|timeline)\/[A-Za-z0-9._\/-]+?\.png/g;

/** A JSON field holding an absolute path: what a reader would take for a
 * file on this machine. */
const SERVER_PATH = /"[a-z_]*(?:path|dir|directory|file|dsym|dwarf|mapping|manifest|events|screenshot|before|after|source)"\s*:\s*"\//i;

/** Tools whose paths are this machine's own: a screenshot saved where it was
 * asked, a recording downloaded here, a knowledge base made here. */
const LOCAL_PATHS = new Set([
  "take_screenshot", "take_annotated_screenshot", "download_recording", "init_app_knowledge",
]);

/** Images attached to one answer, at most: a timeline can name hundreds. */
export const MAX_INLINE = 6;

type Content = { type: string; text?: string; data?: string; mimeType?: string };
type Result = { content?: Content[]; [k: string]: unknown };

/** `result`, when the server is another machine: with the screenshots it
 * names attached as images, and a line saying its paths are the server's.
 * Unchanged otherwise -- and unchanged, not failed, if anything here does. */
export async function withRemotePaths<T>(tool: string, result: T,
                                         find: () => Server): Promise<T> {
  const r = result as unknown as Result;
  if (LOCAL_PATHS.has(tool) || !r || !Array.isArray(r.content)) return result;
  const text = r.content.filter((c) => c.type === "text").map((c) => c.text ?? "").join("\n");
  const shots = [...new Set(text.match(SHOT) ?? [])];
  if (!shots.length && !SERVER_PATH.test(text)) return result;
  try {
    const server = find();
    if (!(await pathsAreRemote(server))) return result;
    const images: Content[] = [];
    const missing: string[] = [];
    for (const path of shots.slice(0, MAX_INLINE)) {
      try {
        const resp = await serverFetch(server,
          `${server.url}/api/v1/screenshots?path=${encodeURIComponent(path)}`,
          { headers: quernHeaders(server.apiKey), signal: AbortSignal.timeout(15000) });
        if (!resp.ok) {
          missing.push(path);
          continue;
        }
        images.push({ type: "text", text: `screenshot ${path}:` });
        images.push({ type: "image", data: Buffer.from(await resp.arrayBuffer()).toString("base64"),
                      mimeType: "image/png" });
      } catch {
        missing.push(path);
      }
    }
    const notes = [`Paths in this answer are on the quern server's disk (${server.url}), not ` +
      "this machine's" + (shots.length ? "; the screenshots it saved are attached below" : "") +
      ". download_recording copies a recording's files here."];
    if (shots.length > MAX_INLINE) {
      notes.push(`${shots.length - MAX_INLINE} more screenshot(s) named above are not attached.`);
    }
    if (missing.length) notes.push(`Could not fetch: ${missing.join(", ")}.`);
    return { ...r, content: [...r.content, { type: "text", text: notes.join(" ") }, ...images] } as
      unknown as T;
  } catch {
    return result;
  }
}
