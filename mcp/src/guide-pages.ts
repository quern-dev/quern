// Kept apart from index.ts so a test can import it: importing index.ts starts
// the server.

/**
 * The pages a contents page names, as [page, what it answers]: one per line of
 * the form "- `<base>/<page>` -- <what it answers>", where base is the contents
 * page's own address (quern://guide/<topic>, quern://app-knowledge-guide).
 */
export function guidePages(base: string, contents: string): Array<[string, string]> {
  const escaped = base.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  const line = new RegExp(`^- \`${escaped}/([a-z][a-z0-9-]*)\` -- (.+)$`, "gm");
  return [...contents.matchAll(line)].map((m) => [m[1], m[2]]);
}
