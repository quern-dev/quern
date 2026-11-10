# When something is wrong

*`quern://guide/devices/troubleshooting` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

**"The screen summary is empty on a physical device"** — `element_count: 0` with no error, while `take_screenshot` plainly shows a populated screen. The accessibility tree and screenshots take different paths, so the device looks alive and unreadable at once.

The usual cause is that WDA's `/source` read exceeded its budget. WDA serializes requests, so the abandoned call keeps running on the device and the *next* one queues behind it — which is why retrying with a larger timeout can still fail, and why the failure looks permanent rather than intermittent. Give the device a moment to drain before retrying.

You no longer have to infer any of this. A read that timed out and fell back now says so: `get_screen_summary` and `get_ui_tree` return `source_timed_out: true` and a `degraded` string, and a post-action screen context carries them too. On a full-tree read, an `element_count` with no `degraded` beside it is a real count. That does not extend to `strategy: "skeleton"`, which never has a `degraded` field because you asked for a partial read — its count is the navigation chrome it returned, not what is on the screen. Quern also no longer treats a timeout as proof the runner is hung. WDA serialises requests, so a tree big enough to outlast any check window queues every liveness ping behind it — waiting longer can only ever distinguish slow from slower. A restart now needs evidence the runner is *gone*: a refused or reset connection, meaning nothing is listening. A ping that merely times out says something accepted it and is busy, and the runner is left alone — which matters because the restart reinstalls it through xcodebuild (#170).

The budgets are 20s for A13-era and older chips and 10s for everything newer. Both are sized from measurement with headroom: an iPhone 11 on iOS 26 takes about 10.2s to answer on its home screen, and an iPhone 15 Pro 4.4–5.3s depending on how dense the screen is. Expect these to move when *Xcode* moves rather than when the device does — the same iPhone 11 answered in 7.5s before WDA was rebuilt under Xcode 27, on identical hardware and the same screen.

Pass `source_timeout` (30–45) on `get_screen_summary` or `get_ui_tree` when a device is slower than that, or a screen is unusually dense. It does not restart anything. `strategy: "skeleton"` skips the `/source` read entirely and returns navigation chrome only, which is the right answer for maps with many pins.
