// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { inlineLandmarks } from "../dist/tools/landmark-schema.js";

test("the bare list form still parses, as every existing caller sends it", () => {
  const out = inlineLandmarks.parse({ Home: [{ element: "Button", label: "OK" }] });
  assert.deepEqual(out.Home, [{ element: "Button", label: "OK" }]);
});

test("the object form carries scrollable and landmark_conventions through", () => {
  const out = inlineLandmarks.parse({
    Home: {
      landmark_conventions: 2,
      scrollable: false,
      landmarks: [{ element: "RadioButton", identifier: "tab_home", selected: true }],
    },
  });
  assert.equal(out.Home.landmark_conventions, 2);
  assert.equal(out.Home.scrollable, false);
});

test("a misspelt screen field is refused, not dropped", () => {
  // Dropped, it would read as undeclared with nothing said.
  const result = inlineLandmarks.safeParse({
    Home: { landmark_convention: 2, landmarks: [{ element: "Button", label: "OK" }] },
  });
  assert.equal(result.success, false);
});

test("a URL landmark needs no element and keeps its fields", () => {
  const out = inlineLandmarks.parse({
    Web: [{ web_url_contains: "/settings", web_process: "com.apple.SafariViewService" }],
  });
  assert.deepEqual(out.Web, [
    { web_url_contains: "/settings", web_process: "com.apple.SafariViewService" },
  ]);
});

test("a non-integer declaration is refused", () => {
  const result = inlineLandmarks.safeParse({
    Home: { landmark_conventions: 2.5, landmarks: [] },
  });
  assert.equal(result.success, false);
});
