# Frame sources: what exists, and what the protocols must admit

Design notes for `FrameSource` / `EncodedFrameSource`. Measured, not assumed.

## iOS simulator — CoreSimulator framebuffer

Raw `IOSurface` per composited frame, event-driven, ~60 fps under animation
and free when idle. Private API via `dlopen`. Conforms to `FrameSource`.

Timestamps are **arrival time** (`.arrival`): the callback reports only that
a frame happened.

## Physical iOS — CoreMediaIO capture device

Raw `IOSurface` via `AVCaptureVideoDataOutput`, 60 fps, real presentation
timestamps (`.reported`). Conforms to `FrameSource`.

Two constraints worth naming:

- **USB only.** A device on wifi never appears as a capture device, so it
  cannot be previewed this way at all.
- **iOS fakes the status bar.** Every frame shows 9:41 with full signal and
  battery. Documented QuickTime-capture behaviour, fine for demos, actively
  misleading for a diagnostic timeline.

## Physical iOS — WDA's own MJPEG server (not implemented)

WebDriverAgent serves MJPEG on **device port 9100**, reachable over the same
RemoteXPC tunnel as its control port. Verified against an iPhone 11:

```
Server: WDA MJPEG Server
57 frames over ~6s  ->  ~9.5 fps
828x1792 native, median 51 KB/frame, ~4 Mbps
multipart framed with --BoundaryString
```

Trade-offs against CoreMediaIO:

| | CoreMediaIO | WDA MJPEG |
|---|---|---|
| transport | USB only | tunnel — **works over wifi** |
| frame rate | 60 fps | ~9.5 fps |
| status bar | synthetic 9:41 | **real** |
| re-encodable | yes, raw frames | no, already JPEG |
| requires | DAL opt-in, USB | WDA running |

Worth having for two reasons neither of which is speed. It is the **only**
way to see a wifi-attached device, and its status bar is truthful, which
matters wherever a frame is evidence rather than decoration.

**It does not fit either protocol.** `FrameSource` emits raw surfaces;
`EncodedFrameSource` emits H.264 `CMSampleBuffer`s. This is a stream of
JPEGs — already compressed, but not in a form a recorder or an H.264 sink
can take without a decode. Adding it means either a third protocol or
generalising `EncodedFrameSource` beyond H.264. Do not paper over it by
decoding JPEGs back to surfaces just to fit: that throws away the only
advantage of an already-encoded source.

### Proven over wifi

Tested on an iPhone 15 Pro attached by **wifi only** — `usbmux list` reported
it as `Network` while a second iPhone was the only device on the USB bus.

```
WDA /status  [fd42:f516:7d1d::1]:8100  -> 200, WebDriverAgent 11.4.0
window/size                            -> 393x852 pt
dragfromtoforduration x3               -> 200, screen content changed
WDA MJPEG    [fd42:f516:7d1d::1]:9100  -> 47 frames / ~5s, 1178x2556, ~9.4 fps
```

Session, **input injection** and video, all with no cable — the swipes were
confirmed visually on the device, not just by status code. CoreMediaIO can
do none of it for a device in this state.

**The tunnel came from `devicectl`, not `tunneld`.** This is the part quern
is missing. tunneld held no tunnel for the device at any point; the address
came from:

```
xcrun devicectl device info details --device <hw-udid>
  • tunnelIPAddress: fd42:f516:7d1d::1
```

There are two independent RemoteXPC tunnel providers, and
`server/device/wda_client.py` only consults tunneld — it tries the tunneld
address, finds nothing for a wifi device, and falls through to a usbmux
forward that cannot work without a cable.

**Reading `tunnelIPAddress` from devicectl is not the fix, though it looked
like it.** Measured 2026-09-14: that tunnel lives only as long as the
`devicectl` process. Each `device info details` call mints a fresh address
(`fd31:…`, then `fd12:…`, then different again) and tears it down on exit —
probing one from another process immediately afterwards gives `No route to
host`, and the `utun` interface is gone seconds later. An address read this
way is dead before a caller can use it.

The real gap is elsewhere. tunneld *does* discover wifi devices and build
tunnels for them (`start-tunnel-task-usbmux-<udid>-Network` in the log), but
those tunnels have a **median lifetime of 2.0 seconds** across 11,587 of
them, against 32s for USB and 13+ hours for the one live wired tunnel. So
tunneld advertises an address that is usually already dead, `GET /status`
fails, and the caller falls through to the usbmux path. One Network tunnel
in the log did last 13.7 hours, so this is churn rather than a hard limit —
finding out why is the open work. See #163.

Both providers *can* hold tunnels to the same device at once, each with its
own address; they do not contend.

One prerequisite: the device must be discoverable to usbmux over the
network. Before that was enabled, `pymobiledevice3 developer dvt xcuitest`
failed with "usbmux has no device matching udid", because the launcher
resolves devices through usbmux. Afterwards it started WDA over wifi
normally.

## Android — adb screenrecord, or our own encoder

See `docs/proposals/unified-screen-streaming.md` on the spike branch.
`screenrecord` gives H.264 with a 180s cap and one IDR per session; an
on-device MediaCodec encoder in `quern-driver.apk` removes both.
`EncodedFrameSource.requestKeyframe()` already models the difference.

## Prior art

- **baguette** (tddworks, Apache 2.0) — the CoreSimulator framebuffer route,
  and the grid/focus split for a multi-device view.
- **prod-FARM-IOS-Core** — a TikTok click-farming tool, so the automation
  half is not of interest, but three infrastructure patterns are: WDA's
  MJPEG endpoint above; a **single-instance, lock-guarded WDA supervisor**
  that relaunches on death and reports per-device health including
  `unlock-required`; and sequential per-device port assignment (8100+ for
  control, 9100+ for video) persisted alongside the device list.

  The supervisor is the interesting one. Quern currently has no WDA
  supervision at all, which is why a runner dying leaves every subsequent
  call failing, and why a stale forward from a *different* process could
  silently disable automation (issues #159, #160).
