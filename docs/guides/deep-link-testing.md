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

Quern's `open_url` dispatches URIs directly through the OS's intent resolver (Android) or URL dispatch system (iOS). This is the same code path as tapping a link in a text message, a push notification, or another app — **not** the same as opening in a browser.

### Testing Custom Schemes

> "Open myapp://checkout/order/12345 on the simulator"

This is the simplest case. The OS looks up which app registered the `myapp://` scheme and launches it with the URL. Your agent verifies the right screen loaded:

> "Open the deep link, then check if we landed on the order detail screen for order 12345"

What happens when nothing handles the scheme differs by platform, and the difference matters:

- **iOS** fails loudly. `simctl` exits non-zero and Quern raises, so you get an error containing `NSOSStatusErrorDomain, code=-10814` and `Simulator device failed to open <url>`.
- **Android currently reports success.** `am start` exits 0 even when it cannot resolve the intent — it writes `Error: Activity not started, unable to resolve Intent` to stderr and returns 0 anyway — so `open_url` answers `{"status": "ok"}` for a URL nothing can open. It is indistinguishable from a working deep link.

So on Android, **do not treat `status: ok` as evidence the link resolved.** Verify the destination instead: follow the call with `get_screen_summary` (or `wait_for_element` on something only the target screen has) and assert you actually landed there. That is the right habit on both platforms, and on Android it is the only signal you have.

This is tracked as [#78](https://github.com/quern-dev/quern/issues/78); once it's fixed, a failed Android dispatch will raise like iOS does.

### Testing Universal Links / App Links

> "Open https://myapp.com/checkout/order/12345 on the simulator"

This is where it gets subtle. When you use `open_url` with an HTTPS URL:

- **On Android**: `am start -a android.intent.action.VIEW` sends the URL through the intent resolver. If the app has a verified App Link for that domain, the app opens directly. If not, the user gets a disambiguation dialog (or it opens in the browser).
- **On iOS**: `simctl openurl` dispatches through the same system as link taps. If the app has a valid universal link registration for the domain, the app opens. If not, Safari opens.

So `open_url` with an HTTPS URL tests whether verification is actually working — **for a build where verification is supposed to work.** On a release build, the browser opening instead of your app means something in the chain (server config → OS verification → app entitlements) is broken.

**On a debug or staging build, the browser opening is often correct behaviour rather than a bug** — but for different reasons per platform, and only Android gives you a way around it.

**Android.** Debug builds are usually signed with a keystore whose certificate hash is absent from `assetlinks.json`, and staging domains frequently host no verification file at all, so the link genuinely is not a verified App Link. Auditing assetlinks here means debugging something that is working as configured. To drive the link into the app anyway, name the package:

> "Open https://staging.myapp.com/product/abc123 on the emulator, targeting com.myapp.debug"

`open_url` accepts a package that is passed straight to `am start`, delivering the intent to that app and bypassing verification entirely.

**iOS has no equivalent bypass.** `open_url` calls `simctl openurl`, which takes only a URL — the `bundle_id` parameter is accepted by the API but **ignored on iOS**, so passing it changes nothing and gives no warning that it did nothing. If Safari opens on iOS, that is a real signal about your Associated Domains entitlement and AASA file, not something to wave away as "just a debug build". Test iOS routing through your custom scheme, and test universal links with a build whose entitlements actually match the domain.

Either way, keep the two questions apart: **routing** (does the path reach the right screen with the right parameters) and **verification** (does the OS agree the domain belongs to your app). They fail independently, so testing them together tells you less than testing them separately.

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

The guide's advice to test both is not only about app state — on iOS they are
literally different entry points:

- **Warm** (app already running): `application(_:open:options:)`
- **Cold** (app not running): the URL is in `launchOptions[.url]` on
  `didFinishLaunching`, and `application(_:open:options:)` is never called

Handle one and not the other and the link is lost silently on that path, with no
error anywhere. Universal links add a third: `application(_:continue:...)` with
an `NSUserActivity`. A deep link test that only ever runs against an app that is
already open will not notice any of this.

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
