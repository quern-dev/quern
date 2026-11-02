# Sharing a Server

One quern server can serve several agents at once: yours and a colleague's, a few CI jobs, or several of your own agents on different machines. This page is for the person running that server -- what each caller can do, how they're kept out of each other's way, and how to take access away.

## Every other caller gets a client key

The server's own API key (`~/.quern/api-key`) can do everything, and it never leaves its Mac. Everyone else gets a **client key**:

- **Over the network:** pairing with `quern pair` / `quern connect` issues one ([Setting Up a Remote Server](remote-setup.md)).
- **On the same Mac:** `quern clients add LABEL` prints one, once. Give it to the caller as `QUERN_CLIENT_KEY`, which the quern MCP, `quern session run` and `quern record` read.

The key decides who is calling, not the network address: a client key used on the server's own Mac is still a client.

## What a client may do

- **Watch what the server shows:** devices, screenshots, UI trees, logs, network flows, crashes, recordings.
- **Reserve devices and change only the ones it holds.** Tapping, typing, installing apps, setting locations and the like are refused on a device the client hasn't reserved, and on one someone else holds.
- **Capture its own app's traffic** on a simulator it holds (`add_session_capture`), without changing the server's capture settings or restarting its proxy. A small number of capture proxies per client keeps one caller from crowding the machine. An Android device it holds is captured by routing it through the server's proxy (`record_device_proxy_config` with `apply=true`); the setting is cleared when its hold ends.
- **Set mocks** scoped to a simulator or Android device it holds.
- **Install its apps from its own machine.** The MCP uploads the app (`.ipa`, `.apk` or `.app.tar.gz`) and installs it.
- **Load its project's knowledge base for its own session.** It's uploaded, used only by that session, and dropped when the session ends.
- **Issue viewer links for its own recordings,** unless you turned off `remote.client_viewer_links`.

What a client can't do is anything that affects the whole Mac: starting or stopping the proxy, the system proxy, local capture, intercepts, server-wide mocks and knowledge bases, log filters, updates. Those are the server owner's.

A client key isn't a sandbox. It keeps cooperating users from changing each other's devices and the Mac's shared settings by mistake. An app a client installs still runs as the Mac's user, so only give keys to people you'd let use the Mac.

## Keeping clients out of each other's work

By default the server is **shared**: every client can watch every device's screens, logs and traffic, which is what a team usually wants. For a server shared between people or teams who shouldn't see each other's work, make it **blind** in `~/.quern/config.json`:

```json
{"sessions": {"visibility": "blind"}}
```

Restart quern after changing it. A blind server shows each client only the devices it holds and the data that comes from them. The owner still sees everything.

## Sessions, and what happens when a caller disappears

A client's reservations last as long as its connection. The MCP and `quern session run` hold that connection for you.

- **A clean exit releases at once.**
- **A dropped connection** -- a closed laptop, a lost network, a killed process -- gets a grace period to reconnect and keep its devices: 180 seconds by default, set with `{"sessions": {"grace_seconds": N}}` (120-300). After that its devices are freed.
- **When a client's hold on a device ends**, the server also ends that client's capture on it, its mocks scoped to it, its recordings on it, and the knowledge base loaded for its session, and removes the apps it installed there (see below).

For scripts that call the REST API directly, `quern session hold NAME --background` holds a session's connection and prints its id, so reservations work without the MCP.

`quern session list` shows every session and what it holds; `quern session release ID` ends one.

## Taking access away

```text
quern clients list              # every key: label, created, last used
quern clients revoke laptop     # its sessions end and its devices are freed within a second
```

Revoking also closes the client's live streams, and its next request is refused.

## What a client installed is removed when it lets go

Apps a session installs on a device it holds (`install_app`, `build_and_install`), and their data, are cleaned up when its hold ends -- a release, a closed connection after its grace, a revoked key -- so a shared machine doesn't fill up with every client's apps, and one client's app doesn't reach the next. Set it in `~/.quern/config.json`, read at start:

```json
{"sessions": {"client_release": "uninstall", "owner_release": "none"}}
```

- **`uninstall`** (the default for client keys): each install notes the app it added, and those are removed -- nothing else, so an app installed at the machine meanwhile stays. quern's own drivers (WebDriverAgent, uiautomator2) and system apps are never touched. An install over an app already there adds nothing, so that app is left, and the release says so (`installs_adding_nothing`). Where the app's bundle id can be read (an `.app`, an `.ipa`, a Gradle build), only that app counts as the install's; an APK upload is held to "exactly one new app". An install that seemed to add several apps, or an app that isn't the one installed, or whose result couldn't be listed, can't be told apart from what was already there: its apps are left too, and said (`unclear_installs`).
- **`erase`**, for a machine several people share: every iOS simulator a client held is erased when it lets go, whether or not anything was installed, and booted again if it was booted. Erasing also removes the proxy's certificate from it; `auto_install_cert` puts it back at the next capture. Android devices and physical iPhones fall back to `uninstall`.
- **`none`** (the default for the owner's own sessions): left as the session left it.

The release's answer lists what was done (`installs_removed`, `erased`). A device the same key still holds through another session, or the same session name opened again, is handed over (`installs_handed_over`) rather than cleaned under it. A clean-up that fails is tried again at the next sweep, up to three times, and then given up on and said. If the device's apps can't be listed before a client's install, the install is refused (503) rather than made, since its release couldn't tell what to remove. An install still running when the hold ends removes its app as it finishes, and its answer says so (`hold_ended`). If the device is held again by then, it isn't touched: the app goes on the new session's record if that session is the same key's, and is left if it is another key's.

## Planned

- **Bounding the build store and showing disk use** -- the rest of [#581](https://github.com/quern-dev/quern/issues/581).
