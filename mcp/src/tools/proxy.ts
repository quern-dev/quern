import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { discoverServer } from "../config.js";
import { apiRequest } from "../http.js";

export function registerProxyTools(server: McpServer): void {
  server.tool(
    "query_flows",
    `Query captured HTTP flows from the network proxy. Filter by host, method, status code, and more.`,
    {
      host: z.string().optional().describe("Filter by hostname"),
      path_contains: z.string().optional().describe("Filter by path substring"),
      method: z
        .string()
        .optional()
        .describe("Filter by HTTP method (GET, POST, etc.)"),
      status_min: z
        .number()
        .optional()
        .describe("Minimum status code (e.g. 400 for errors)"),
      status_max: z
        .number()
        .optional()
        .describe("Maximum status code"),
      has_error: z
        .boolean()
        .optional()
        .describe("Filter to flows with connection errors"),
      simulator_udid: z
        .string()
        .optional()
        .describe("Filter by simulator UDID (only flows from this simulator)"),
      client_ip: z
        .string()
        .optional()
        .describe("Filter by client IP address (physical device identification)"),
      limit: z
        .number()
        .min(1)
        .max(1000)
        .default(100)
        .describe("Max flows to return"),
      offset: z.number().min(0).default(0).describe("Pagination offset"),
    },
    async ({
      host,
      path_contains,
      method,
      status_min,
      status_max,
      has_error,
      simulator_udid,
      client_ip,
      limit,
      offset,
    }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/flows", {
          host,
          path_contains,
          method,
          status_min,
          status_max,
          has_error,
          simulator_udid,
          client_ip,
          limit,
          offset,
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "wait_for_flow",
    `Wait for an HTTP flow matching filters to appear. Blocks server-side until a match is found or timeout expires. Always returns with matched:true/false — timeouts are not errors.

Use this after triggering a UI action to observe the resulting network request without polling. Auto-sets 'since' to 5 seconds before the call to catch flows that completed between the action and the wait call.`,
    {
      host: z.string().optional().describe("Filter by hostname"),
      path_contains: z
        .string()
        .optional()
        .describe("Filter by path substring"),
      method: z
        .string()
        .optional()
        .describe("Filter by HTTP method (GET, POST, etc.)"),
      status_min: z
        .number()
        .optional()
        .describe("Minimum status code (e.g. 400 for errors)"),
      status_max: z.number().optional().describe("Maximum status code"),
      has_error: z
        .boolean()
        .optional()
        .describe("Filter to flows with connection errors"),
      simulator_udid: z
        .string()
        .optional()
        .describe(
          "Filter by simulator UDID (only flows from this simulator)"
        ),
      client_ip: z
        .string()
        .optional()
        .describe("Filter by client IP address (physical device identification)"),
      timeout: z
        .number()
        .min(0.1)
        .max(60)
        .default(10)
        .describe("Max wait time in seconds (default 10, max 60)"),
      interval: z
        .number()
        .min(0.1)
        .max(5)
        .default(0.5)
        .describe("Poll interval in seconds (default 0.5)"),
    },
    async ({
      host,
      path_contains,
      method,
      status_min,
      status_max,
      has_error,
      simulator_udid,
      client_ip,
      timeout,
      interval,
    }) => {
      try {
        const body: Record<string, unknown> = {};
        if (host !== undefined) body.host = host;
        if (path_contains !== undefined) body.path_contains = path_contains;
        if (method !== undefined) body.method = method;
        if (status_min !== undefined) body.status_min = status_min;
        if (status_max !== undefined) body.status_max = status_max;
        if (has_error !== undefined) body.has_error = has_error;
        if (simulator_udid !== undefined)
          body.simulator_udid = simulator_udid;
        if (client_ip !== undefined) body.client_ip = client_ip;
        body.timeout = timeout;
        body.interval = interval;

        // Extended HTTP timeout so the MCP client doesn't time out before the server
        const httpTimeoutMs = timeout * 1000 + 5000;

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/flows/wait",
          undefined,
          body,
          httpTimeoutMs
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "get_flow_detail",
    `Get full request/response detail for a single captured HTTP flow, including headers and bodies.`,
    {
      flow_id: z.string().describe("The flow ID to retrieve"),
    },
    async ({ flow_id }) => {
      try {
        const data = await apiRequest(
          "GET",
          `/api/v1/proxy/flows/${encodeURIComponent(flow_id)}`
        );

        // Try to parse JSON body strings into objects so they render
        // as structured JSON instead of escaped strings
        const record = data as Record<string, Record<string, unknown>>;
        for (const key of ["request", "response"]) {
          const section = record?.[key];
          if (section?.body && typeof section.body === "string") {
            try {
              section.body = JSON.parse(section.body as string);
            } catch {
              // Not JSON — keep as string
            }
          }
        }

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "proxy_status",
    `Check proxy state and configuration. Returns status (running/stopped/error),
port, flows captured, intercept state, mock rules count, system proxy state,
local_capture mode, and local_ip.

The system_proxy field shows whether the macOS system proxy is currently
configured. If null/false, the user's browser works normally and traffic
is NOT being captured.

The local_capture field is a list of process names being captured via mitmproxy
local mode. When non-empty, traffic from those processes (e.g. ["MobileSafari"])
is transparently captured without needing a system proxy. Empty list means disabled.
Use set_local_capture to change the process list on the fly.

The local_ip field is the Mac's outward-facing IP address — use this as the
proxy server address when configuring a physical device's Wi-Fi proxy settings.

Each entry in cert_setup (keyed by device UDID) now includes:
- wifi_proxy_host: IP address last configured on the device's Wi-Fi proxy settings
- wifi_proxy_port: Port last configured (typically 9101)
- wifi_proxy_set_at: ISO 8601 timestamp of when the proxy was last set
- wifi_proxy_stale: true if wifi_proxy_host differs from the current local_ip,
  meaning the device's proxy config is pointing to a stale address and needs
  reconfiguring with the current local_ip. Call record_device_proxy_config after
  completing Wi-Fi proxy setup to update these values.`,
    {},
    async () => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/status");

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "verify_proxy_setup",
    `Verify that mitmproxy CA certificate is installed on simulator(s). Performs ground-truth verification by querying the simulator's TrustStore database. Works for both booted and shutdown simulators. Use this to check if proxy setup is complete before capturing traffic. Returns detailed installation status per device with timestamps, and detects devices that may have been erased.

IMPORTANT: Prefer omitting udid to check all devices in a single call (~1-2s total). Do NOT loop over individual UDIDs — the batch call is just as fast and avoids N redundant round-trips. Filter the results client-side if you only need a subset (e.g. iPhones only).`,
    {
      udid: z
        .string()
        .optional()
        .describe(
          "Specific simulator UDID to verify. If omitted, verifies devices matching the state/type filters."
        ),
      state: z
        .enum(["booted", "shutdown"])
        .optional()
        .default("booted")
        .describe("Filter by device state. Defaults to 'booted' to avoid checking all ~38 simulators."),
      device_type: z
        .enum(["simulator", "device"])
        .optional()
        .default("simulator")
        .describe("Filter by device type. Defaults to 'simulator' since cert verification is primarily for simulators."),
    },
    async ({ udid, state, device_type }) => {
      try {
        const body: Record<string, unknown> = {};
        if (udid !== undefined) body.udid = udid;
        if (state) body.state = state;
        if (device_type) body.device_type = device_type;

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/cert/verify",
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "install_proxy_cert",
    `Install the mitmproxy CA certificate on simulator(s). Required for HTTPS traffic capture. Idempotent by default — skips simulators that already have the cert installed. Use force to reinstall.

IMPORTANT: Prefer omitting udid to install on all booted simulators in a single call. Do NOT loop over individual UDIDs — the batch call is just as fast and avoids N redundant round-trips.`,
    {
      udid: z
        .string()
        .optional()
        .describe(
          "Specific simulator UDID. If omitted, installs on all booted simulators."
        ),
      force: z
        .boolean()
        .optional()
        .describe(
          "Force reinstall even if cert is already present (default: false)"
        ),
    },
    async ({ udid, force }) => {
      try {
        const body: Record<string, unknown> = {};
        if (udid !== undefined) body.udid = udid;
        if (force !== undefined) body.force = force;

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/cert/install",
          undefined,
          Object.keys(body).length > 0 ? body : undefined
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "start_proxy",
    `Start the mitmproxy network capture.

IMPORTANT: By default, this does NOT configure the system proxy.
The proxy will listen on the specified port but traffic won't be
routed through it until you call configure_system_proxy.

For SIMULATORS: The proxy automatically configures per-simulator proxy
settings on all booted simulators that have the mitmproxy CA cert installed.
Simulator traffic is captured without affecting the host's browser or network.
Check the simulator_proxy field in proxy_status to see which simulators are configured.

For PHYSICAL DEVICES: You still need to call configure_system_proxy to route
traffic through mitmproxy (or configure the device's Wi-Fi proxy settings manually).

WORKFLOW (simulators — zero setup):
1. Call start_proxy → simulators are auto-configured
2. Run your tests → traffic appears in flow capture
3. Call stop_proxy when done

WORKFLOW (physical devices):
1. Call start_proxy (proxy listens, system proxy stays OFF)
2. When ready to capture: call configure_system_proxy
3. Run your tests/capture traffic
4. When done: call unconfigure_system_proxy (restore user's browser)

NOTE: If local_capture is enabled (check proxy_status), all local traffic
including simulator traffic is captured transparently without any proxy
configuration needed.`,
    {
      port: z
        .number()
        .optional()
        .describe("Port for the mitmproxy listener (default: 9101)"),
      listen_host: z
        .string()
        .optional()
        .describe("Host to listen on (default: 0.0.0.0)"),
      system_proxy: z
        .boolean()
        .optional()
        .describe(
          "Configure macOS system proxy automatically (default: false). Only set to true if you need immediate capture without manual control."
        ),
    },
    async ({ port, listen_host, system_proxy }) => {
      try {
        const body: Record<string, unknown> = {};
        if (port !== undefined) body.port = port;
        if (listen_host !== undefined) body.listen_host = listen_host;
        if (system_proxy !== undefined) body.system_proxy = system_proxy;

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/start",
          undefined,
          Object.keys(body).length > 0 ? body : undefined
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "stop_proxy",
    `Stop the mitmproxy network capture. Automatically restores the macOS system proxy to its pre-Quern state if it was configured.`,
    {},
    async () => {
      try {
        const data = await apiRequest("POST", "/api/v1/proxy/stop");

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "get_flow_summary",
    `Get an LLM-optimized summary of recent HTTP traffic. Groups by host, shows errors, slow requests, and overall statistics. Supports cursor-based polling for efficient delta updates.`,
    {
      window: z
        .enum(["30s", "1m", "5m", "15m", "1h"])
        .default("5m")
        .describe("Time window to summarize"),
      host: z
        .string()
        .optional()
        .describe("Filter to a specific host"),
      since_cursor: z
        .string()
        .optional()
        .describe(
          "Cursor from a previous summary response — returns only new activity since then"
        ),
      simulator_udid: z
        .string()
        .optional()
        .describe("Filter to flows from a specific simulator UDID"),
      client_ip: z
        .string()
        .optional()
        .describe("Filter by client IP address (physical device identification)"),
    },
    async ({ window, host, since_cursor, simulator_udid, client_ip }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/flows/summary", {
          window,
          host,
          since_cursor,
          simulator_udid,
          client_ip,
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "proxy_setup_guide",
    `Get device proxy configuration instructions with auto-detected local IP. Includes steps for both simulator and physical device setup. For physical devices, the response includes cert_install_url — a direct URL to download the Quern CA cert from Safari on the device. The correct order is: install cert → trust cert → configure Wi-Fi proxy.`,
    {},
    async () => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/setup-guide");

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "configure_system_proxy",
    `Manually configure macOS system proxy to route through mitmproxy.

Use this after start_proxy when you're ready to begin capturing traffic.
Remember to call unconfigure_system_proxy when done to restore the user's browser.

NOTE: The proxy must be running first (call start_proxy).`,
    {
      interface: z
        .string()
        .optional()
        .describe("Network interface name (e.g. 'Wi-Fi'). Auto-detected if omitted."),
    },
    async ({ interface: iface }) => {
      try {
        const body = iface ? { interface: iface } : undefined;
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/configure-system",
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
    }
  );

  server.tool(
    "unconfigure_system_proxy",
    `Restore macOS system proxy to its pre-Quern state.

IMPORTANT: Always call this when you finish capturing traffic to restore
the user's browser functionality. The proxy server will keep running in
the background and can be re-enabled with configure_system_proxy.`,
    {},
    async () => {
      try {
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/unconfigure-system"
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
    }
  );

  server.tool(
    "record_device_proxy_config",
    `Record that the Wi-Fi proxy has been configured on a physical device with the current machine IP. ` +
    `Call this after successfully completing the Wi-Fi proxy setup in device Settings. ` +
    `Quern stores the address so it can detect if the machine IP changes and the proxy config becomes stale. ` +
    `The host and port are derived from the running server — you only need to provide the device UDID.`,
    {
      udid: z.string().describe("Device UDID"),
    },
    async ({ udid }) => {
      try {
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/device-proxy-config",
          undefined,
          { udid }
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "set_local_capture",
    `Set the list of process names for local capture mode. Uses mitmproxy's
macOS System Extension to transparently capture traffic from specific processes
without configuring a system proxy.

Restarts the proxy automatically to apply the new configuration — no server
restart needed. Pass an empty list to disable local capture.

On first use, macOS will prompt to allow the Mitmproxy Redirector system
extension in System Settings > Privacy & Security.`,
    {
      processes: z
        .array(z.string())
        .describe(
          'List of process names to capture (e.g. ["MobileSafari", "Metatext"]). Empty list disables local capture.'
        ),
    },
    async ({ processes }) => {
      try {
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/local-capture",
          undefined,
          { processes }
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );
}
