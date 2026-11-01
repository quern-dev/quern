# Using Remote Devices

Once your machine is paired with another Mac's quern server ([Setting Up a Remote Server](remote-setup.md)), your agent can see that Mac's devices and work on them. You ask for a device the way you always do; if it's on another machine, your agent finds it there.

## Ask for what you need

> "I need an iOS 17.5 iPhone simulator. Reserve one and take a screenshot."

If your own Mac has one, your agent uses it. If it doesn't, the answer to the reservation -- or to listing devices -- says which paired server has matching free devices and how to get there, and your agent switches to that server, reserves the device and carries on. You don't have to say "on the other Mac".

You can still be explicit:

> "What devices do my other Macs have free?"

or:

> "Switch to studio and boot an iPad running iOS 18.6."

## What your agent sees

**`list_remotes`** lists every server your agent could use: your own Mac first, then each paired one. For each server it shows whether it answers (or why not), its quern version, and its devices, with:

- **name and model** -- the model is what the hardware is ("iPhone 15 Pro"), whatever a simulator was renamed to;
- **OS version, device family and type**;
- **state** -- booted or shut down;
- **free, yours, or held** by someone else.

The server your agent is using now is marked `current`.

## Switching servers

**`use_remote`** moves your agent's session to another server, or back to `local`, your own Mac. Every tool call after it goes to that server, so screenshots, taps, logs and network capture all work on its devices.

A switch is made only once the agent's session is live on the new server. If that server doesn't answer, or no session can be held there, your agent stays where it was and is told why.

**One server at a time.** Your agent works on one server at a time. If it holds devices where it is, it's asked to release them first, or to switch with `release=true`, which releases them as part of the switch. Working across both machines at once, such as sharding a test run over two Macs' simulators, is planned ([#572](https://github.com/quern-dev/quern/issues/572)).

## What follows your agent to the new server

- **Its mocks.** The mock responses your agent set are removed from the server it leaves and set up again on the devices of its first reservation on the new one -- a mock scoped to a simulator on its simulators, one scoped to an Android device on its Android devices. That reservation's answer lists the new rule ids, and anything that couldn't be set up yet -- for example because the new server's proxy isn't running -- waits for a later reservation.
- **Its session capture.** Apps it asked to capture with `add_session_capture` are captured again on the newly reserved simulators.
- **Your project's knowledge base.** If your project has one (`.quern/knowledge`), it's loaded on the new server for your agent's session, so screen identification keeps working.

The answer to `use_remote` also says whether the new server's proxy is running, since mocks and capture need it.

## What stays behind

- **Machine-wide proxy settings** -- local capture, the system proxy, intercept and bypass patterns -- belong to each Mac's owner and stay as they were.
- **Recordings, logging, previews and device settings** stay with the old server and its devices.
- **Mocks the old server couldn't be reached to remove** are named in the answer and keep answering there; they're removed the next time your agent switches back to that server.
- **The `quern` command in your terminal** isn't switched. It still talks to your own Mac unless you set `QUERN_REMOTE`.

## Good to know

- **Leaving a server that has stopped answering** takes one timeout, about five seconds, not more. Anything that couldn't be cleaned up there is named.
- **Your agent's devices are released if it disappears.** If its connection drops -- the laptop lid closes, the network goes -- the remote server keeps its devices for a grace period (three minutes by default) so it can reconnect, then frees them.
- **Android emulators** on a remote server can be driven, but reservation, matching, mocks and capture for them are still being completed ([#582](https://github.com/quern-dev/quern/issues/582)). iOS simulators are fully supported.
