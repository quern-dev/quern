import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceUITools(server: McpServer): void {
  server.registerTool("get_ui_tree", {
    description: `Get the full accessibility tree (all UI elements) from the current screen. Optionally scope to children of a specific element using children_of.

Pass include_raw=true when debugging the platform normalizer itself — e.g., to see whether an Android node carries selected="true" or some other source attribute that didn't make it into our canonical fields. Each element gains an extra_attrs dict of the raw source attributes from the underlying provider (uiautomator2 XML on Android; iOS not yet populated). Default false to keep payloads small.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      children_of: z
        .string()
        .optional()
        .describe("Only return children of the element with this identifier or label"),
      snapshot_depth: z
        .coerce.number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds (default: 3s, 6s for older devices). Use 10-15 for slow screens like feeds/lists on older devices. Physical devices only."),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed: the default path (sim-bridge on Xcode 26+/Apple Silicon, idb elsewhere) already probes hidden tab-bar/nav-bar children. Simulators only."),
      include_raw: z
        .coerce.boolean()
        .optional()
        .describe(
          "Include raw source attributes (extra_attrs) on each element — useful when debugging the normalizer to see what the underlying provider actually emitted. Default false. Currently only Android populates extra_attrs."
        ),
    }),
  }, async ({ udid, children_of, snapshot_depth, strategy, source_timeout, mode, include_raw }) => {
    try {
      const params: Record<string, string> = {};
      if (udid) params.udid = udid;
      if (children_of) params.children_of = children_of;
      if (snapshot_depth !== undefined) params.snapshot_depth = String(snapshot_depth);
      if (strategy) params.strategy = strategy;
      if (source_timeout !== undefined) params.source_timeout = String(source_timeout);
      if (mode) params.mode = mode;
      if (include_raw) params.include_raw = "true";
      const data = await apiRequest("GET", "/api/v1/device/ui", params);

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("get_element_state", {
    description: `Get a single element's state without fetching the entire UI tree. More efficient than get_ui_tree when you only need to check one element. Returns the element with its current state (enabled, value, etc.). If multiple elements match, returns the first with a match_count field.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
      identifier: z
        .string()
        .optional()
        .describe("Element identifier (case-sensitive)"),
      element_type: z
        .string()
        .optional()
        .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ label, label_contains, label_prefix, identifier, element_type, udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/ui/element", {
        label,
        label_contains,
        label_prefix,
        identifier,
        type: element_type,
        udid,
      });

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("wait_for_element", {
    description: `Wait for an element to satisfy a condition (server-side polling). Eliminates client-side retry loops and reduces API round-trips. Always returns with matched:true/false - timeouts are not errors. Supports conditions: exists, not_exists, visible, enabled, disabled, value_equals, value_contains.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
      identifier: z
        .string()
        .optional()
        .describe("Element identifier (case-sensitive)"),
      element_type: z
        .string()
        .optional()
        .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
      condition: z
        .enum([
          "exists",
          "not_exists",
          "visible",
          "enabled",
          "disabled",
          "value_equals",
          "value_contains",
        ])
        .describe("Condition to wait for"),
      value: z
        .string()
        .optional()
        .describe("Required for value_equals and value_contains conditions"),
      timeout: z
        .coerce.number()
        .default(10)
        .describe("Max wait time in seconds (default 10, max 60)"),
      interval: z
        .coerce.number()
        .default(0.5)
        .describe("Poll interval in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed now — the default path probes hidden tab-bar/nav-bar children automatically. Simulators only."),
    }),
  }, async ({
    label,
    label_contains,
    label_prefix,
    identifier,
    element_type,
    condition,
    value,
    timeout,
    interval,
    udid,
    mode,
  }) => {
    try {
      const body: Record<string, unknown> = {
        condition,
        timeout,
        interval,
      };

      if (label !== undefined) body.label = label;
      if (label_contains !== undefined) body.label_contains = label_contains;
      if (label_prefix !== undefined) body.label_prefix = label_prefix;
      if (identifier !== undefined) body.identifier = identifier;
      if (element_type !== undefined) body.type = element_type;
      if (value !== undefined) body.value = value;
      if (mode !== undefined) body.mode = mode;
      if (udid !== undefined) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/wait-for-element",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("get_screen_summary", {
    description: `Get an LLM-optimized text description of the current screen, including interactive elements and their locations. Uses smart truncation with prioritization (buttons with identifiers > form inputs > generic buttons > static text). Navigation chrome (tab bars, nav bars) is always included regardless of limit.

This is the recommended first step before interacting with UI. Use this to discover element labels and identifiers, then use tap_element to tap by name instead of coordinates.`,
    inputSchema: strictParams({
      max_elements: z
        .coerce.number()
        .default(20)
        .describe("Maximum interactive elements to include (0 = unlimited, default 20)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      snapshot_depth: z
        .coerce.number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds (default: 3s, 6s for older devices). Use 10-15 for slow screens like feeds/lists on older devices. Physical devices only."),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed: the default path (sim-bridge on Xcode 26+/Apple Silicon, idb elsewhere) already probes hidden tab-bar/nav-bar children. Simulators only."),
      identify: z
        .boolean()
        .optional()
        .describe("Match screen against loaded landmarks. Adds identified_as and confidence fields to the response."),
    }),
  }, async ({ max_elements, udid, snapshot_depth, strategy, source_timeout, mode, identify }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/screen-summary", {
        max_elements,
        udid,
        snapshot_depth,
        strategy,
        source_timeout,
        mode,
        identify,
      });

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("tap", {
    description: `Tap at specific screen coordinates on the simulator.

PREFER tap_element over this tool. Use get_screen_summary to find element labels/identifiers, then tap_element to tap by name. Only use coordinate tap as a last resort when tap_element cannot find the element.

If coordinate taps are not landing on the expected element, use take_annotated_screenshot to see exact element bounding boxes overlaid on the screen. Read the element's position as a fraction of the screen (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to calculate correct tap coordinates, then retry.`,
    inputSchema: strictParams({
      x: z.coerce.number().describe("X coordinate"),
      y: z.coerce.number().describe("Y coordinate"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ x, y, udid }) => {
    try {
      const body: Record<string, unknown> = { x, y };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/tap",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("tap_element", {
    description: `Find a UI element by label or accessibility identifier and tap its center. Returns "ambiguous" with match list if multiple elements match — use element_type (e.g., "Button", "TextField", "StaticText") to narrow results.

This is the PREFERRED way to tap UI elements. Use get_screen_summary first to discover element labels/identifiers, then use this tool. Avoid using coordinate-based tap unless this tool cannot find the element.

Label matching modes (mutually exclusive — use only one):
- label: exact match (case-insensitive)
- label_contains: substring match (case-insensitive) — useful for elements with long, dynamic labels
- label_prefix: prefix match (case-insensitive) — useful when the label starts with a stable string but has variable content after`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label text — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
      identifier: z
        .string()
        .optional()
        .describe("Accessibility identifier to search for"),
      element_type: z
        .string()
        .optional()
        .describe('Element type to filter by (e.g. "Button", "TextField")'),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds. Use 10-15 for slow screens on older devices. Physical devices only."),
      value: z
        .string()
        .optional()
        .describe('For switches/toggles: desired value ("1" = on, "0" = off). Checks current state first and skips the tap if already set. Returns status "already_set" if no tap was needed.'),
      scroll_to_find: z
        .boolean()
        .default(true)
        .describe("If the element isn't in the current view, scroll it into view (via the no-dump swipe loop) and then tap. Supported on Android and iOS. Set false to fail fast without scrolling. Note the cost of leaving this on: asking for an element that does not exist at all is indistinguishable from one that is merely off-screen, so the search runs until the request times out (~60s) and reads like a hung server rather than a missing element. When you are checking whether something is present, pass false."),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after the tap completes. Useful for verifying navigation."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the tap. Returns file paths in screenshots.before and screenshots.after."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0). Increase for slow devices or complex transitions."),
    }),
  }, async ({ label, label_contains, label_prefix, identifier, element_type, udid, source_timeout, value, scroll_to_find, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = {};
      if (label) body.label = label;
      if (label_contains) body.label_contains = label_contains;
      if (label_prefix) body.label_prefix = label_prefix;
      if (identifier) body.identifier = identifier;
      if (element_type) body.element_type = element_type;
      if (udid) body.udid = udid;
      if (source_timeout) body.source_timeout = source_timeout;
      if (value !== undefined) body.value = value;
      if (scroll_to_find === false) body.scroll_to_find = false;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/tap-element",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("swipe", {
    description: `Perform a swipe gesture from one point to another.`,
    inputSchema: strictParams({
      start_x: z.coerce.number().describe("Starting X coordinate"),
      start_y: z.coerce.number().describe("Starting Y coordinate"),
      end_x: z.coerce.number().describe("Ending X coordinate"),
      end_y: z.coerce.number().describe("Ending Y coordinate"),
      duration: z
        .coerce.number()
        .default(0.5)
        .describe("Swipe duration in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ start_x, start_y, end_x, end_y, duration, udid }) => {
    try {
      const body: Record<string, unknown> = {
        start_x,
        start_y,
        end_x,
        end_y,
        duration,
      };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/swipe",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("wait_for_settle", {
    description: `Wait until the screen stops changing — the answer no sleep can give. Compares successive screenshots and returns once consecutive frames are effectively identical, so it adapts to whatever the screen is actually doing instead of guessing a duration. Use it after any action that starts a transition or an animation, and before interacting with a web view: web content is invisible to the accessibility tree, so nothing else can tell you it has finished drawing. It answers "has drawing stopped", NOT "has content arrived" -- a blank page still loading is perfectly still, and this will call it settled in under two seconds (measured against a stalled request: settled=true after 1.6s with a white screen). Raising the timeout does not help, because nothing is moving. For a slow load, wait for the content itself -- poll get_web_content until it returns elements, or wait_for_element for a native one -- and use this afterwards to let the result stop moving. Measured on a web form where typing is silently lost if it arrives early — a 0.2s sleep landed the keystroke 1 time in 5, a 1.0s sleep landed 5 of 5, and this landed 5 of 5 in about 0.67s. Returns settled=false with a reason when the timeout expires, which means something is animating rather than loading (a spinner, a video, a carousel) and will never settle — treat that as a signal about the screen, not a failure of the wait. Also reports last_change, the fraction of pixels that differed on the final comparison, which distinguishes "nearly settled" from "moving steadily".`,
    inputSchema: strictParams({
      timeout: z
        .coerce.number()
        .default(10)
        .describe("Seconds to wait before giving up (default 10)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ timeout, udid }) => {
    try {
      const body: Record<string, unknown> = { timeout };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/wait-settled",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("get_web_content", {
    description: `Read web content inside a WKWebView on an iOS simulator, which get_ui_tree cannot see. The accessibility tree walk stops at the web view — it is absent from the tree entirely, not empty — so a screen built around one looks like nothing but its native chrome. This reads the live DOM through the simulator's Web Inspector and returns elements with real screen frames, including DOM ids and icon-only controls that carry only an aria-label. The results are merged into subsequent UI reads for about a minute, so tap_element, get_ui_tree and get_screen_summary all see them and you can tap by label as usual. That overlay is dropped as soon as anything changes the screen -- a tap, swipe, scroll or launch -- so call this again after each interaction with the page. A tap on a web element is verified against the live screen first, and returns not_found with reason "stale_web_content" rather than tapping the wrong thing if the page moved underneath it. Two routes, tried in that order. The Web Inspector gives real DOM ids and icon-only controls, and reaches an in-process WKWebView when the app sets webView.isInspectable = true (per instance, so one view opting in says nothing about another) and an SFSafariViewController with no opt-in at all, under bundle_id com.apple.SafariViewService. When the Inspector sees nothing, this falls back to hit-testing the screen, aimed by text recognition -- slower, and label-only with no DOM, but it is the only route into an ASWebAuthenticationSession, which is presented with no connected application at all. The response says which route answered, under "route". Simulator only: Android's accessibility tree already descends into WebView, and physical iOS devices use a different transport — on both, use get_ui_tree. Costs roughly a second on top of a UI tree read, so call it when a screen looks emptier than it should, then again after navigating or scrolling the page. If it reports anchored=false, the page was found but its position on screen could not be confirmed; the elements are withheld rather than returned at a guessed offset.`,
    inputSchema: strictParams({
      bundle_id: z
        .string()
        .optional()
        .describe(
          "Which connected app to read. Optional when only one app is connected to the Web Inspector, which is the usual case."
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (bundle_id) body.bundle_id = bundle_id;
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/web-content",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("scroll_to_element", {
    description: `Scroll a scrollable container until an element is in view, WITHOUT tapping it. Resolve by identifier or label. Supported on Android and iOS (simulator + physical). Drives a bounded swipe loop that re-checks the target by selector after each swipe — no full UI-tree dump, so it avoids the dump-induced scroll a tree read can cause. Android covers View RecyclerView, Compose LazyColumn, and Compose-in-ScrollView. iOS handles both scroller shapes: a directional swipe toward a located-but-off-screen target in laid-out ScrollViews, and a blind down-then-up sweep for lazy/recycled lists where rows drop out of the tree; rows scrolled under the nav/status bar are rejected rather than counted as already visible. Returns the element's on-screen position once visible; 404 if it never appears within the swipe budget.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Exact visible text or content-description of the target"),
      identifier: z
        .string()
        .optional()
        .describe("Resource-id (package-stripped, e.g. \"button_log\")"),
      max_swipes: z
        .coerce.number()
        .int()
        .default(10)
        .describe("Max scroll attempts before giving up (default 10)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ label, identifier, max_swipes, udid }) => {
    try {
      const body: Record<string, unknown> = { max_swipes };
      if (label) body.label = label;
      if (identifier) body.identifier = identifier;
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/scroll-to-element",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("type_text", {
    description: `Type text into an input field. PASS label OR identifier whenever you can: the field is then located, tapped, typed into and read back, so the call fails loudly if the text did not arrive. Without one the text goes wherever the keyboard happens to be pointed and nothing can check it -- on a screen with no text field at all this still returns ok, having typed into nothing, and a tap that failed to take focus looks identical to one that worked. The response reports verified: true|false so you can tell which you got. Verification costs a fresh UI read (~2s), which is why it is opt-in. On iOS simulators this attaches the simulated hardware keyboard before typing (required for shifted characters to retain their modifier), which hides the software keyboard — use set_hardware_keyboard with enabled=false afterward if a later step expects the software keyboard to be visible.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Field to type into, by visible text. Enables verification."),
      identifier: z
        .string()
        .optional()
        .describe("Field to type into, by accessibility identifier. Enables verification."),
      text: z.string().describe("Text to type"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after typing. Useful for detecting autocorrect issues."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the text entry."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0)."),
    }),
  }, async ({ text, label, identifier, udid, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = { text };
      if (label) body.label = label;
      if (identifier) body.identifier = identifier;
      if (udid) body.udid = udid;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/type",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("clear_text", {
    description: `Clear a text field (select-all + delete), before type_text when the field already holds content. Pass label or identifier to say WHICH field: without one this picks the first field that has a value, which is not the field you just tapped -- on a sign-in form, clearing before typing the password finds the email field and empties that instead. Focus cannot be detected; the accessibility tree does not report it. A web field is cleared through the Web Inspector when the page is visible to it (a few milliseconds), and by keystroke otherwise -- about 63ms per character, so a long value takes seconds. Either way the field ends empty. Secure text fields (passwords) may not support select-all at all.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Exact visible text or content-description of the field to clear"),
      identifier: z
        .string()
        .optional()
        .describe("Accessibility identifier of the field to clear"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ label, identifier, udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (label) body.label = label;
      if (identifier) body.identifier = identifier;
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/clear",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("press_button", {
    description: `Press a hardware button on the device. iOS: HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY. Android: home, back, recents, volumeUp, volumeDown, power, enter, delete, menu.`,
    inputSchema: strictParams({
      button: z
        .string()
        .describe(
          "Button name. iOS: HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY. Android: home, back, recents, volumeUp, volumeDown, power, enter, delete, menu"
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ button, udid }) => {
    try {
      const body: Record<string, unknown> = { button };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/press",
        undefined,
        body
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });
}
