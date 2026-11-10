// Kept apart from index.ts so a test can import it: importing index.ts starts
// the server.

/**
 * The pages a topic's contents list names, as [page, what it answers]: one per
 * line of the form "- `quern://guide/<topic>/<page>` -- <what it answers>".
 */
export function guidePages(topic: string, contents: string): Array<[string, string]> {
  const line = new RegExp(`^- \`quern://guide/${topic}/([a-z][a-z0-9-]*)\` -- (.+)$`, "gm");
  return [...contents.matchAll(line)].map((m) => [m[1], m[2]]);
}
