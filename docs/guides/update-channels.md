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

```text
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

- **Installed with the install script (tarball):** the updater compares the
  newest prerelease against the newest stable release and takes whichever is
  actually newer, so you are never served *older* content than a stable user.
  Between beta cycles that usually means you are on the same build as stable —
  which is the point, not a fault. Until 0.18.1 it simply took the newest
  prerelease, which after a few stable releases meant beta users were offered a
  downgrade.
- **Installed as a git clone:** your checkout has to be on the branch your
  channel tracks — `release/beta` for beta. Any other branch, including `main`,
  is treated the same way: `quern update` tells you how far ahead the release
  branch is, prints the exact `git checkout` to run, and applies nothing.

  ```sh
  git checkout release/beta
  quern update
  ```

  It will not move your workspace for you, by design — silently switching
  branches under someone's uncommitted work is not a trade Quern makes.

## Switching back

```sh
quern set-channel stable
quern update
```

Mid beta cycle the newest stable can be *older* than the beta build you are
running, and that move is allowed — it is the one time an update deliberately
goes backwards. Any other backwards move is refused, because the only thing
that produces one is the channel resolving the wrong release, and a silent
downgrade is how you end up on a stale build without noticing.

On a tarball install that is all of it. **On a git clone you also have to move
the checkout back**, because `set-channel` changes your preference and nothing
else — a clone left sitting on `release/beta` will keep being told it is on the
wrong branch rather than being returned to stable:

```sh
quern set-channel stable
git checkout release/stable
quern update
```

Stable is always a safe target: stable releases are cut from content that has
already been through beta, so going back does not mean losing fixes you were
relying on.

## What to expect on beta

Beta is where a release is checked against real projects before it is tagged. In
practice that means you see fixes sooner and you are the one who finds anything
that was missed. If you use Quern daily on work you care about, `stable` is the
better default. If you would rather catch a regression early — and are willing
to report it — beta is genuinely useful, and switching back takes two commands.

The daily update notice respects your channel, so on beta it tells you about
beta releases rather than nagging you toward stable.

You do not have to wait for it. `quern check-updates` asks now, and the
Quern app has **Check for Updates** for the same thing — worth knowing on
beta, where releases land closer together than the once-a-day check. Both keep
working even if you turn the automatic check off.

Running an update refreshes that answer itself, so the notice does not go on
offering a version you have already installed. It asks rather than assuming:
the run knows what it pulled, but not whether the channel moved again while it
was working, and a checkout sitting behind its release branch has a real update
outstanding after a run that did nothing. If the refresh cannot reach the
network the previous answer is left alone and the next automatic check is not
suppressed — an update that could not confirm anything should not buy a day of
silence.
