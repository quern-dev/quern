# Release facts for this machine

Copy to `RELEASE_LOCAL.md` at the repository root and fill in. That filename is
gitignored; this template is not, so **do not put your values here.**

Rules, so this file stays useful instead of becoming a second rulebook:

- **Values and local facts only.** The procedure lives in
  `docs/release-channels.md` and stays there. Two documents describing one
  process drift, and this is the copy nobody reviews.
- **No secrets.** The keychain holds those. Names of things are fine; the
  things themselves are not.
- **Nothing irrecoverable.** Every line below should be re-derivable from the
  machine. Losing this file should cost rediscovery, not a release.

---

## Signing and notarization

Both are read from the environment by `scripts/release-menubar.sh`.

- `SIGNING_IDENTITY` — exported from: <!-- e.g. ~/.zshrc -->
  Recover with `security find-identity -v -p codesigning`.
- `NOTARY_PROFILE` — profile name: <!-- the name you passed to store-credentials -->
  Recreate with `xcrun notarytool store-credentials "<name>"`.

Confirm before starting a cut, because it can stop resolving with nothing in
the repository having changed:

    xcrun notarytool history --keychain-profile "$NOTARY_PROFILE"

Last confirmed working: <!-- date -->

## The update-into-this-release test

The doc pass asks you to update *into* the candidate from the previous release,
for real. Record which installs you use, so it is not re-decided each time.

- Git install at: <!-- path -->  currently on tag: <!-- vN.M.K -->
- Tarball install at: <!-- path or host -->
- Anything that machine needs first: <!-- VPN, mounted volume, etc. -->

## Hosts

<!-- Any machine that takes part in a release: what it is for, how to reach it,
     what it has installed. -->

| Host | Role | Notes |
|---|---|---|
| | | |

## Things that have bitten me here

<!-- Local failures worth not rediscovering: a stale profile, a full disk on
     the build machine, a keychain that locks on idle. Date them. -->
