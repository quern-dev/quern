# Setting Up a Remote Server

A quern server on another Mac -- a Mac Studio that can run more simulators, a CI machine, a teammate's desk -- can lend its devices to your agent. You turn on its remote listener, pair your machine with it once, and from then on your agent can see its devices and work on them as if they were local.

This page sets up the two machines. [Using Remote Devices](remote-agents.md) covers what your agent does with them, and [Sharing a Server](remote-sharing.md) covers several people or agents using one.

## On the server's Mac: turn on the remote listener

The remote listener is off by default. Add it to `~/.quern/config.json` on the Mac that will lend its devices:

```json
{"remote": {"enabled": true}}
```

Then restart quern (`quern restart`). The listener serves the same API over TLS on port 9130, to client keys only. The main API stays on 127.0.0.1, unchanged.

```text
quern remote status
#   Listening at https://192.168.1.188:9130
#   Certificate SHA256:40:C0:3D:...:ED:67
```

Optional settings, in the same object:

- **`bind`** -- the address and port to listen on, default `"0.0.0.0:9130"`.
- **`hosts`** -- extra names the listener answers to, such as `["studio.lan"]`, besides this Mac's LAN address. A request naming any other host is refused.
- **`client_viewer_links`** -- whether a client may issue viewer links for its own recordings. On by default.

You don't need a certificate authority or a domain name. Quern makes its own certificate the first time the listener starts, and clients pin its fingerprint when they pair.

## Pair your machine with it

Pairing gives your machine a client key and the listener's certificate fingerprint in one step. On the **server's Mac**, ask for a code, naming the machine you're pairing:

```text
quern pair laptop
#   Pairing code for laptop (one use, 5 minutes):
#
#     ABCD-EFGH-JKMN-PQRS
#
#   On the client machine:
#
#     quern connect https://192.168.1.188:9130 --code ABCD-EFGH-JKMN-PQRS
#
#   This server's certificate: SHA256:40:C0:3D:...:ED:67
```

On **your machine**, run the command it printed, giving the server a name you'll use from now on:

```text
quern connect https://192.168.1.188:9130 --code ABCD-EFGH-JKMN-PQRS --name studio
#   Paired with https://192.168.1.188:9130 as 'studio' (certificate SHA256:40:C0:...).
```

The code works once, lasts five minutes, and never crosses the network: the two machines prove to each other that they know it. The name can be anything except `local`, which means your own Mac.

Your machine keeps the pairing in `~/.quern/remotes.json`. Check it any time:

```text
quern remote list
#   local    http://127.0.0.1:9110        as this Mac   up  0.25.0  39 devices (7 booted), 39 free
#   studio   https://192.168.1.188:9130   as laptop     up  0.25.0  51 devices (3 booted), 51 free
```

Each server is asked now, so the list says whether it answers, which version it runs, and what devices it could lend. `--devices` lists them all, with name, model, OS version and who holds each. `quern remote forget studio` removes a pairing.

## Point your agent at it

There are two ways. Most people want the first.

- **Leave your agent on your own Mac's server and let it switch.** Nothing more to set up: your agent sees every paired server with `list_remotes` and moves to one with `use_remote` when it needs a device there. See [Using Remote Devices](remote-agents.md).
- **Start an agent on the remote server from the beginning.** Register the MCP pointed at it with `quern mcp-install --remote studio`, or set `QUERN_REMOTE=studio` for the MCP, `quern session run` and `quern record`. A `QUERN_REMOTE` that can't be read fails, rather than quietly using your own Mac.

## Managing who can connect

Each pairing issues a client key on the server. On the server's Mac:

```text
quern clients list              # every key: label, created, last used, revoked
quern clients revoke laptop     # its sessions end and its devices are freed within a second
```

The server's own API key is refused on the remote listener, so the key that can do everything never leaves its Mac. Pair even your own laptop as a client.

To replace the listener's certificate -- say it may have been copied -- run `quern remote rotate-cert` on the server. Every client then has to pair again, because the fingerprint they pinned no longer matches; they are told so, with both fingerprints, rather than connecting to something unexpected.

## If something goes wrong

`quern remote list` says why a server can't be used:

- **not reachable** -- the server is off, asleep, or the address changed. Check `quern remote status` on it.
- **timed out** -- it accepted the connection but didn't answer in time.
- **certificate changed** -- the server rotated its certificate, or something is between you and it. Pair again if you expected the change.
- **refused its key** -- the key was revoked, or the server was reset. Pair again.

A server that couldn't be asked is never shown as having no devices, so "51 free" always means the server answered.
