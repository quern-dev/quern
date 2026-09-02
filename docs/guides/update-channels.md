# Update Channels

Quern ships on two channels. **`stable`** is the default and tracks tagged
releases. **`beta`** is opt-in, and gets the same content earlier — usually days
to a couple of weeks ahead — so problems are found before they reach everyone.

If you never think about channels, you are on `stable`, and that is the right
place to be.

## Which channel am I on?

```sh
quern set-channel
```

With no argument it prints the current channel and the branch it tracks, plus
the valid names:

```
Current update channel: stable (tracks origin/release/stable)
Valid channels: stable, beta
```

You can also just ask Claude — "what's my Quern channel?" — since the channel and
update status come back with `ensure_server` in the normal course of a session.

## Switching to beta

```sh
quern set-channel beta
quern update
```

Both steps are needed. `set-channel` writes your preference to
`~/.quern/config.json` and nothing else: it does not switch branches, download
anything, or change the running server. The next `quern update` is what actually
moves you.

What that update does depends on how you installed Quern:

- **Installed with the install script (tarball):** the updater takes whichever
  GitHub Release is the newest one marked as a prerelease. If there are no
  prereleases at the moment, it falls back to the latest stable, so you are
  never served *older* content than a stable user.
- **Installed as a git clone:** you need to be on `release/beta`, or on `main`
  and willing to switch. If you are sitting on a feature branch, the updater
  will tell you beta updates exist but will not move your workspace for you.
  That is deliberate — silently switching branches under someone's uncommitted
  work is not a trade Quern makes.

## Switching back

```sh
quern set-channel stable
quern update
```

The same two steps in reverse. Stable is always a safe target: because stable
releases are cut from content that has already been through beta, going back
does not mean losing fixes you were relying on.

## What to expect on beta

Beta is where a release is checked against real projects before it is tagged. In
practice that means you see fixes sooner and you are the one who finds anything
that was missed. If you use Quern daily on work you care about, `stable` is the
better default. If you would rather catch a regression early — and are willing
to report it — beta is genuinely useful, and switching back takes two commands.

The daily update notice respects your channel, so on beta it tells you about
beta releases rather than nagging you toward stable.
