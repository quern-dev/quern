import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { errorText, strictParams } from "./helpers.js";
import { execFile } from "node:child_process";
import { homedir } from "node:os";

export function registerSystemTools(server: McpServer): void {
  server.registerTool(
    "update_quern",
    {
      description:
        `Update Quern to the latest release on the user's configured channel (stable or beta). Launches the equivalent of "quern update" — pulls the latest source (or fetches the release tarball), reinstalls Python deps, rebuilds the MCP server, and restarts the daemon. The HTTP call returns immediately; the actual upgrade runs in a detached child and takes ~30-60 seconds. The Quern server will restart during that window, so the next MCP tool call may fail until it's back up. Reconnect by retrying ensure_server. Only call this after the user has confirmed they want to update — typically prompted by an "update_available" hint in ensure_server's response.`,
      inputSchema: strictParams({}),
    },
    async () => {
      try {
        const data = await apiRequest("POST", "/api/v1/system/update");
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(data, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: "text" as const,
              text: `${errorText(e, "Error launching update")}\n\nFall back to running "quern update" in a terminal manually.`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.registerTool(
    "set_update_channel",
    {
      description:
        `Set the user's Quern update channel preference (stable or beta). "stable" tracks the release/stable branch (only tagged releases); "beta" tracks release/beta for opt-in early testing. Setting the channel only updates ~/.quern/config.json — it does not switch git branches or apply an update. After flipping to beta, the next call to update_quern (and any future periodic check) will compare against the beta branch. Only call this after the user has explicitly asked to change channels.`,
      inputSchema: strictParams({
        channel: z.enum(["stable", "beta"]).describe(
          "stable (default, recommended) or beta (opt-in prerelease)",
        ),
      }),
    },
    async ({ channel }) => {
      try {
        const data = await apiRequest(
          "PUT",
          "/api/v1/system/channel",
          undefined,
          { channel },
        );
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(data, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: "text" as const,
              text: errorText(e, "Error setting channel"),
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.registerTool("list_remotes", {
    description: `List the remote quern servers this machine is paired with (quern connect), each asked now: whether it answers, its quern version (and whether it differs from this Mac's), and its devices with which are free -- not held by anyone -- and which this key holds.

Use it to see what other Macs could lend before choosing where to work: a remote's devices are used by starting an MCP with QUERN_REMOTE=<name>, or quern session run / record with it. A remote that could not be asked says why (down, timed out, certificate changed, key refused), never "no devices". Read-only; it uses only what each remote already lets this key read.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await quernRemoteList();
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return { content: [{ type: "text" as const, text: errorText(e) }], isError: true };
    }
  });
}

/** `quern remote list --json`, from the quern CLI on this machine: the paired
 * remotes live in its ~/.quern/remotes.json, whichever server this MCP talks to.
 * It exits 1 when a remote could not be asked, and still prints the list. */
export function quernRemoteList(): Promise<unknown> {
  const candidates = ["quern", `${homedir()}/.local/bin/quern`];
  const attempt = (i: number): Promise<unknown> => new Promise((resolve, reject) => {
    execFile(candidates[i], ["remote", "list", "--json"], { timeout: 60_000 },
      (err, stdout, stderr) => {
        const out = String(stdout ?? "").trim();
        if (out) {
          try {
            resolve(JSON.parse(out));
            return;
          } catch {
            // not JSON: an older quern, or an error; fall through
          }
        }
        const missing = err && (err as NodeJS.ErrnoException).code === "ENOENT";
        if (missing && i + 1 < candidates.length) {
          resolve(attempt(i + 1));
          return;
        }
        reject(new Error(missing
          ? "the quern CLI is not installed on this machine, so its paired remotes cannot be listed"
          : `quern remote list failed: ${String(stderr || err?.message || "no output").trim()}`));
      });
  });
  return attempt(0);
}
