# Waiting for the screen

*`quern://guide/ui/waiting` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

Use `wait_for_settle` when you are waiting for the screen to stop changing rather than for a specific element — after a navigation, an animation, or before interacting with a web view, whose content is invisible to the accessibility tree so nothing else can tell you it has finished drawing.

It answers "has drawing stopped", not "has content arrived". A blank page still loading is perfectly still, and will be called settled in under two seconds. Raising the timeout does not help, because nothing is moving. So for a slow load, wait for the content first — poll `get_web_content` until it returns elements, or `wait_for_element` for a native one — and use `wait_for_settle` afterwards to let the result stop moving.

Check the `settled` field before acting on the result. `settled: false` means something is animating rather than loading — a spinner, a video, a carousel — and will never settle. Treat that as a fact about the screen, not a failed wait.
