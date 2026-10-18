/** Getting held-session notices to the agent (#254).
 *
 * An agent cannot receive a push, so what it needs to know about its held
 * session -- "quern restarted, your reservations are gone" -- rides on its next
 * tool result. At the tool layer, not on API answers: several tools read only
 * part of the server's JSON (a build's summary, a progress poll), and a notice
 * attached to a field they drop was lost (review). Here it is appended to the
 * result itself, as its own text item, whatever the tool did.
 */
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { sessionNotices } from "./http.js";

type ToolResult = { content?: { type: string; text?: string }[]; [k: string]: unknown };

/** `result` with any pending notices appended, each delivered once. */
export function withSessionNotices<T>(result: T): T {
  if (!sessionNotices.length || !result || typeof result !== "object") return result;
  const r = result as unknown as ToolResult;
  if (!Array.isArray(r.content)) return result;
  const notes = sessionNotices.splice(0).map((n) => ({ type: "text", text: `quern session notice: ${n}` }));
  return { ...r, content: [...r.content, ...notes] } as unknown as T;
}

/** Make every tool registered on `server` from now on deliver notices. */
export function deliverSessionNotices(server: McpServer): void {
  const register = server.registerTool.bind(server) as (...a: unknown[]) => unknown;
  (server as unknown as { registerTool: (...a: unknown[]) => unknown }).registerTool = (
    name: unknown, config: unknown, handler: unknown,
  ) => register(name, config, async (...args: unknown[]) =>
    withSessionNotices(await (handler as (...a: unknown[]) => Promise<unknown>)(...args)));
}
