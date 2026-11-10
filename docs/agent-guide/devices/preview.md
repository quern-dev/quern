# Live Preview of Devices and Simulators

*`quern://guide/devices/preview` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

Open real-time video windows to see what's happening on USB-connected physical devices and on booted simulators. Each device is independently managed — no restart penalty after the initial 3-second CoreMediaIO discovery.

1. `preview_device` with a device UDID — adds that device's preview window
2. `preview_device` with another UDID — adds a second device (1s stagger, no rediscovery)
3. `preview_status` — see which devices are active vs. available. `available` always includes booted simulators (`kind: "simulator"`, `id` is the UDID `preview_device` takes). USB devices (`kind: "device"`) appear once the preview process is running, and their `id` is a CoreMediaIO id: preview a phone by its UDID from `list_devices`. `simulators_error` means simulators could not be listed, not that none are booted
4. `stop_preview` with a UDID — remove one device's preview, others stay running
5. `stop_preview` without a UDID — stop all previews and kill the process

**Key insight**: The preview process stays alive even if all windows are closed (by user or via `stop_preview` with UDID). Re-adding a device is instant — no 3s discovery delay. Only `stop_preview` without a UDID kills the process.

**Two routes, one tool.** A simulator is not a CoreMediaIO capture source, so it does not arrive the same way: `quern-media` reads its framebuffer directly and serves MJPEG on loopback, and the preview window opens on that stream. Pass a booted simulator's UDID to `preview_device` and it works; the difference matters only when something breaks, because the failure modes are unrelated.

**Limitations**: a simulator must already be booted, and `preview_device` with no UDID previews USB-connected physical devices only — it does not sweep up simulators, which would open a window for every booted one.

**OFF AIR.** A simulator window whose stream drops — the simulator shut down or rebooted, or `quern-media` restarted — stays open, greys out under an OFF AIR label, and reconnects on its own. While it is off air, `preview_status` reports `"on_air": false` for that simulator, so a frozen last frame is never reported as a live preview. Quitting Simulator.app shuts down every simulator, headless ones included.

**The person at the Mac can open simulators too.** The preview app's Devices menu lists the booted simulators alongside USB devices, and keeps the list current as simulators boot and shut down. A simulator picked there is opened by the server, so it appears in `preview_status` and `stop_preview` closes it like any other.
