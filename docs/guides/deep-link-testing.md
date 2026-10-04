# Deep Link Testing

Mobile apps have two fundamentally different ways of handling deep links, and they break for different reasons. Quern's `open_url` tool lets you test both — but understanding which one you're testing matters.

## The Two Types

### Custom URL Schemes

These look like `myapp://profile/settings` or `fb://page/12345`. The app registers a scheme in its Info.plist (iOS) or AndroidManifest.xml (Android), and the OS routes any URL with that scheme directly to the app.

They're simple, reliable, and have been around since the early days of mobile. They're also not verified — anyone can claim any scheme, and there's no guarantee that `myapp://` actually belongs to your app. If two apps register the same scheme, the behavior is undefined.

### Universal Links (iOS) / App Links (Android)

These look like regular HTTPS URLs: `https://myapp.com/profile/settings`. The magic is that the OS intercepts them before the browser sees them and routes them to your app instead — but only if:

1. **iOS**: Your server hosts an Apple App-Site-Association (AASA) file at `https://myapp.com/.well-known/apple-app-site-association` that declares which paths your app handles, and your app's entitlements match the domain.
2. **Android**: Your server hosts a Digital Asset Links file at `https://myapp.com/.well-known/assetlinks.json` that includes your app's signing certificate hash, and your AndroidManifest declares the intent filter with `autoVerify="true"`.

These are the "proper" deep links — verified, secure, and they work even if the app isn't installed (the URL falls back to the website). They're also more fragile, because the verification chain has more moving parts.

## Testing with open_url

Quern's `open_url` opens a URL the way a tapped link arrives, through the operating system's own routing:

- **iOS simulator**: `simctl openurl`, whichever UI backend is reading the screen.
- **Physical iPhone**: WDA's `/url` with no bundle id (iOS 16.4+), which asks the system to open the URL as another app would. With a bundle id WDA would hand the URL to the app directly and skip the universal-link check, so quern never sends one.
- **Android**: a `VIEW` intent with no package, carrying the `BROWSABLE` category for an http(s) link, as a browser tap does. Other schemes go without it, since a viewer such as Contacts' for `content://` URIs does not declare it.

Pass `bundle_id` to have quern confirm where the link went — see [Did it open in the app?](#did-it-open-in-the-app) below.

### Testing Custom Schemes

> "Open myapp://checkout/order/12345 on the simulator"

This is the simplest case. The OS looks up which app registered the `myapp://` scheme and launches it with the URL. Your agent verifies the right screen loaded:

> "Open the deep link, then check if we landed on the order detail screen for order 12345"

When nothing handles the scheme, every platform fails loudly. On a simulator `simctl` raises with `NSOSStatusErrorDomain, code=-10814`; on a physical iPhone WDA answers with `LSApplicationWorkspaceErrorDomain Code=115` (measured on iOS 26.5), and quern raises that. On Android `am start` exits 0 even when it cannot resolve the intent, so quern reads its output and raises "No app on … handled …" ([#78](https://github.com/quern-dev/quern/issues/78)).

A link that *is* handled can still land on the wrong screen, so verify the destination: pass `bundle_id`, and follow the call with `wait_for_element` on something only the target screen has.

### Testing Universal Links / App Links

> "Open https://myapp.com/checkout/order/12345 on the simulator"

This is where it gets subtle. When you use `open_url` with an HTTPS URL:

- **On Android**: the intent goes with no package and `BROWSABLE`, so the system resolves it as it would a tap. If the app has a verified App Link for that domain, the app opens directly. If not, the browser opens. If two activities claim the path, Android shows its app chooser, and so does a user's tap.
- **On iOS**: `simctl openurl` on a simulator, WDA's `/url` on a device, both through the system that handles link taps. If the app has a valid universal link registration for the path, the app opens. If not, Safari opens.

So `open_url` with an HTTPS URL tests whether verification is actually working — **for a build where verification is supposed to work.** On a release build, the browser opening instead of your app means something in the chain (server config → OS verification → app entitlements) is broken.

**On a debug or staging build, the browser opening is often correct behaviour rather than a bug** — but for different reasons per platform, and only Android gives you a way around it.

**Android.** Debug builds are usually signed with a keystore whose certificate hash is absent from `assetlinks.json`, and staging domains frequently host no verification file at all, so the link genuinely is not a verified App Link. Auditing assetlinks here means debugging something that is working as configured. To drive the link into the app anyway, deliver it to the package:

> "Open https://staging.myapp.com/product/abc123 on the emulator with direct=true, bundle_id com.myapp.debug"

`direct=true` addresses the intent to `bundle_id`, as an Espresso test's `setPackage` does, bypassing verification entirely. Keep it for links the system will not route: it cannot find a link a user's tap would not reach, and it can show a chooser a tap would not, or skip one a tap would show. Test production links without it.

**iOS has no equivalent bypass.** `direct=true` is refused on iOS with a 400, rather than quietly taking the system route for a caller who believes it bypassed verification; `bundle_id` there only names the app to confirm. If Safari opens on iOS, that is a real signal about your Associated Domains entitlement and AASA file, not something to wave away as "just a debug build". Test iOS routing through your custom scheme, and test universal links with a build whose entitlements actually match the domain.

Either way, keep the two questions apart: **routing** (does the path reach the right screen with the right parameters) and **verification** (does the OS agree the domain belongs to your app). They fail independently, so testing them together tells you less than testing them separately.

### Did it open in the app?

`open_url` succeeding means the URL was opened, not that the app took it — a path the domain does not claim opens in the browser, and that is still a successful open. Pass `bundle_id` and quern reports what happened:

- `opened_in_app`: whether that app was in front once the hand-over had settled. Two seconds pass before a read counts, because both wrong answers were measured inside that window: on an iPhone Safari took the front about a second after the open while the app still read as in front, and on Android an activity that crashed on the link was in front for 0.4 seconds first.
- `foreground_app`: what was in front instead — a bundle id or package on a device, the display name on a simulator — and on Android `foreground_activity`.
- `crashed` (Android): whether the app crashed after the open, read from the crash buffer. The screen cannot say: a crash can leave the home screen in front, or the app's own previous screen, which looks exactly like the link having opened.
- `warning`: where the link went and what that usually means — not claimed by the domain, Android's app chooser, a crash.

`opened_in_app` and `crashed` are null, with `opened_in_app_error` or `crash_check_error`, when quern could not tell: that is never reported as either answer.

### Testing Both for the Same Screen

A thorough deep link test hits the same screen via both paths:

> "First, open myapp://product/abc123 and verify we land on the product screen. Then go home, and open https://myapp.com/product/abc123 and verify we land on the same screen."

If the custom scheme works but the universal link opens Safari instead, the problem is in the verification chain — AASA file, entitlements, or domain configuration — not in your app's URL routing code.

## Common Failure Modes

### Universal Link / App Link Verification Failures

These are the sneaky ones. They often work in development and break in production, or work on one device and not another.

**iOS AASA issues:**
- AASA file not at the exact path `/.well-known/apple-app-site-association`
- AASA served with wrong Content-Type (must be `application/json`)
- AASA cached by Apple's CDN — changes can take hours to propagate
- AASA file behind a redirect (Apple's crawler doesn't follow redirects)
- App's Associated Domains entitlement doesn't match the AASA domain
- Wildcard patterns in AASA not matching expected paths

**Android Asset Links issues:**
- `assetlinks.json` not at `/.well-known/assetlinks.json`
- Signing certificate hash doesn't match (debug vs release keystore)
- `autoVerify="true"` missing from the intent filter
- Multiple intent filters — all domains must verify, or none get auto-verified
- Domain verification silently fails and falls back to browser

### Deep Link Routing Bugs

These are app-level issues where the URL is received but handled incorrectly:

- **Missing route**: The app doesn't have a handler for that specific path pattern
- **Auth gate**: The deep link lands on a screen that requires login, but the app shows a blank screen or crashes instead of redirecting to login first
- **Stale state**: The app was already running with cached data, and the deep link to a different context doesn't refresh properly
- **Parameter parsing**: The app doesn't handle URL-encoded characters, query parameters, or fragments correctly

### iOS asks before it opens

A custom-scheme link on iOS can raise a system alert — *Open in "YourApp"?* with
Cancel and Open — instead of dispatching straight through. Until it is answered
it sits above everything, and **every subsequent UI query returns the alert
rather than your app**, so automation that does not expect it looks like it hung
against a wedged simulator.

Two consequences worth planning for:

- Follow an `open_url` with a check for the alert and tap **Open**, the same way
  you would handle any other system prompt.
- A run that dies between opening a link and answering the alert leaves the
  simulator stuck behind it. The next run then fails somewhere unrelated. If a
  simulator starts returning a screen with three elements and a question mark,
  look for a leftover prompt before debugging anything else.

### Cold launch and warm launch arrive by different routes

The advice to test both is not only about app state. On iOS they are literally
different entry points, and **which ones depends on your app's lifecycle** —
getting this wrong is a common way to lose links on exactly one path, silently,
with no error anywhere.

**Scene-based apps** (`UIScene`, the default for UIKit apps since iOS 13):

| | |
|---|---|
| Cold | `connectionOptions.urlContexts` in `scene(_:willConnectTo:options:)` |
| Warm | `scene(_:openURLContexts:)` |
| Universal link | `connectionOptions.userActivities`, or `scene(_:continue:)` |

**App-delegate apps** (no scene manifest):

| | |
|---|---|
| Cold | `launchOptions[.url]` in `didFinishLaunching` **and then** `application(_:open:options:)` |
| Warm | `application(_:open:options:)` |
| Universal link | `application(_:continue:restorationHandler:)` |

**A cold launch here delivers the URL twice.** Measured against a fixture that
counts every delivery: one `openurl` at a terminated app-delegate app produced
**two** recorded links — `didFinishLaunching` sees it in `launchOptions`, and
then `application(_:open:options:)` is called with the same URL. An app that
handles both without deduplicating will run its deep link routing twice per cold
launch, which shows up as a doubled analytics event, a duplicated navigation
push, or a repeated network call rather than as an error.

The scene lifecycle does not do this: the same test against the scene-based
bundle recorded the link **once**, through `scene(_:willConnectTo:options:)`
only. If you are migrating, that is one behaviour that quietly gets better.

Note that `application(_:open:options:)` is legacy — Apple's direction is to
handle URL delivery in the scene delegate, and on recent SDKs the app-delegate
method is deprecated. If a scene-based app implements only the app-delegate
callbacks, they are simply never called.

**Implementing `application(_:configurationForConnecting:options:)` switches
the lifecycle on its own.** The Info.plist manifest is not the only trigger —
adding that method to an app delegate is enough to make UIKit use scenes, after
which none of the app-delegate URL callbacks fire. Measured: a bundle with no
`UIApplicationSceneManifest` at all reported delivery through
`scene(_:willConnectTo:options:)` until the method was compiled out. If your
deep links stopped arriving after an unrelated refactor, check whether that
method appeared.

**SwiftUI** apps can use `.onOpenURL` on the root view, which covers both cold
and warm delivery without touching either delegate.

The practical test consequence is the same either way: a deep link suite that
only ever runs against an already-open app exercises one path and tells you
nothing about the other. Terminate the app between cases.

### Simulator / Emulator Specific

- **iOS simulator**: `tel:` and `mailto:` URIs fail because Phone and Mail apps aren't installed on simulators. This is expected — test these on physical devices.
- **Android emulator**: Some intent filters require the app to be the default handler, which may need user confirmation on first launch.
- **Universal links on iOS simulator**: Sometimes require the app to have been launched at least once before universal link dispatch works. If `open_url` opens Safari instead of your app, try launching the app first, then retrying.

## Combining with Other Quern Tools

### Deep Link + Network Verification

> "Open the product deep link and show me what API calls the app makes to load the product data"

Your agent opens the deep link, then checks the proxy to see if the app made the expected API call — verifying that the deep link not only navigated to the right screen but triggered the correct data fetch.

### Deep Link + State Restoration

> "Restore the 'logged out' checkpoint, then open the checkout deep link. What happens?"

Testing that the app handles deep links gracefully when preconditions aren't met. Does it redirect to login? Does it remember where to go after login completes?

### Deep Link + App Knowledge Base

If you've built an [app knowledge base](app-knowledge.md), deep links are registered in `deep-links/deep_links.json` with their paths, the screen each one lands on, the elements that confirm it, and any caveats. Your agent can use this to:

- Test every documented deep link automatically
- Verify deep links still land on the correct screen after app changes
- Detect new screens that should have deep links but don't

## Documenting Deep Links

Deep links live in a single structured registry at `deep-links/deep_links.json` — not one markdown file per link, unlike screens and alerts. The file carries the domains once at the top level, and each entry describes a path under them:

```json
{
  "production_domain": "myapp.com",
  "staging_domain": "staging.myapp.com",
  "associated_domains": ["applinks:myapp.com"],
  "deep_links": [
    {
      "name": "product-detail",
      "description": "Open a product by ID.",
      "path": "/product/abc123",
      "lands_on": "screens/product-detail",
      "skips_screens": ["home", "category"],
      "verify": {"identifier": "product_header"},
      "premium_gated": false,
      "caveats": ["Shows onboarding on first visit"]
    }
  ]
}
```

The field that earns its keep is `verify`: it holds `wait_for_element` keyword arguments confirming the landing screen, which is exactly the check Android's silent success makes mandatory. `lands_on` is a plain path to a screen doc, not a wikilink. Use separate arrays alongside `deep_links` when an app has distinct link families with different URL patterns.

See [`docs/app-knowledge-guide.md`](https://github.com/quern-dev/quern/blob/main/docs/app-knowledge-guide.md#documenting-deep-links) for the full field list — it is the authoring reference and is not published to this site.

## Tips

- **Test both paths.** A custom scheme working doesn't mean the universal link works. They fail independently.
- **Test cold launch vs warm launch.** Does the deep link work when the app isn't running? What about when it's backgrounded on a different screen?
- **Test on real devices for universal links.** Simulator behavior for AASA verification doesn't always match real devices. The OS may cache verification results differently.
- **Check the verification files.** Use `open_url("https://myapp.com/.well-known/apple-app-site-association")` in a browser on the simulator/emulator to verify the file is accessible and correctly formatted.
- **Watch for redirect chains.** If your domain redirects (www → non-www, HTTP → HTTPS), make sure the AASA/assetlinks file is accessible at the final domain without redirects.
- **Version your deep links.** When path patterns change, old links in emails and push notifications will break. Test that old patterns either still work or fail gracefully.
