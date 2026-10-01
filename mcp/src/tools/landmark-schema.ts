import { z } from "zod";

/**
 * Inline landmarks for load_landmarks, matching what the server accepts.
 *
 * Kept in its own module so mcp/test can parse against it. The schema used to
 * admit only `{screen: [landmark, ...]}` with `element` required, so three
 * things the server reads were unreachable over MCP: a screen's `scrollable`
 * and `landmark_conventions`, which need the object form, and a URL landmark
 * (`web_url_contains`), which has no element.
 */

export const landmarkSelector = z.object({
  element: z
    .string()
    .optional()
    .describe(
      "Element type, e.g. 'Button' or 'Heading'. Required unless web_url_contains is given. " +
      "Matched across backends: with a label or identifier, RadioButton also matches the " +
      "Button WDA reports for the same tab item."
    ),
  identifier: z.string().optional(),
  label: z.string().optional(),
  label_contains: z.string().optional(),
  absent: z.boolean().optional(),
  selected: z
    .boolean()
    .optional()
    .describe(
      "Selection state for tabs, switches, radios, checkboxes. " +
      "true = element must be selected (e.g. the active tab); " +
      "false = element must not be selected. Omit to ignore."
    ),
  web_url_contains: z
    .string()
    .optional()
    .describe(
      "Match a loaded web page's URL instead of an element, for a screen whose identity is " +
      "entirely web. Cannot be combined with element, identifier or label fields."
    ),
  web_process: z
    .string()
    .optional()
    .describe("Bundle id hosting the page; only with web_url_contains."),
});

// Strict: a misspelt `landmark_convention` must be refused, not dropped and
// read as undeclared with nothing said.
export const inlineScreen = z
  .object({
    landmarks: z.array(landmarkSelector),
    scrollable: z
      .boolean()
      .optional()
      .describe("Whether this screen scrolls. Omit when nobody knows."),
    landmark_conventions: z
      .number()
      .int()
      .optional()
      .describe(
        "The landmark conventions this screen is written for (currently 2). A target, " +
        "not a claim: compliance is computed and reported in the response's conventions block."
      ),
  })
  .strict();

export const inlineLandmarks = z
  .record(z.string(), z.union([z.array(landmarkSelector), inlineScreen]))
  .describe(
    "Inline landmarks: object keyed by screen name. Each value is either an array of landmark " +
    "selectors, or {landmarks, scrollable?, landmark_conventions?} to say more about the screen."
  );
