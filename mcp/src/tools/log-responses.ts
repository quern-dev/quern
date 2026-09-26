/**
 * Response shaping for log tools that rebuild the server's answer rather than
 * pass it on. Kept free of MCP and HTTP so it can be run by a test, because
 * checking it by reading the source failed three times on one branch (#255):
 * a text match cannot tell forwarding a field from not forwarding it.
 */

export interface LogQueryAnswer {
  entries?: unknown[];
  [field: string]: unknown;
}

/**
 * What `tail_logs` returns: the server's answer with `total` meaning "entries
 * here", and every other field carried through untouched.
 *
 * Spread rather than listed. The completeness fields (`truncated`,
 * `complete_after`) are the one signal that separates "nothing happened" from
 * "it was evicted", and naming them one by one is how the next field gets
 * dropped. No defaults either: an older server that sends no `truncated`
 * must come through as *absent*, never as `false` -- which the server
 * documents as a guarantee that nothing was lost.
 */
export function tailLogsResult(data: LogQueryAnswer): Record<string, unknown> {
  const entries = data.entries ?? [];
  return { ...data, entries, total: entries.length };
}
