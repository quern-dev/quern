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

If the scheme isn't registered, the OS returns an error — on iOS you'll get an `LSApplicationWorkspaceErrorDomain` error, on Android a `No Activity found to handle Intent` error. Both are surfaced clearly in the `open_url` response.

### Testing Universal Links / App Links

> "Open https://myapp.com/checkout/order/12345 on the simulator"

This is where it gets subtle. When you use `open_url` with an HTTPS URL:

- **On Android**: `am start -a android.intent.action.VIEW` sends the URL through the intent resolver. If the app has a verified App Link for that domain, the app opens directly. If not, the user gets a disambiguation dialog (or it opens in the browser).
- **On iOS**: `simctl openurl` dispatches through the same system as link taps. If the app has a valid universal link registration for the domain, the app opens. If not, Safari opens.

So `open_url` with an HTTPS URL tests whether the universal link / app link verification is actually working. If your app opens, great — the full chain (server config → OS verification → app entitlements) is intact. If the browser opens instead, something in that chain is broken.

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

If you've built an [app knowledge base](app-knowledge.md), deep links are documented in the `deep-links/` directory with their URL patterns, which screens they land on, and what preconditions they require. Your agent can use this to:

- Test every documented deep link automatically
- Verify deep links still land on the correct screen after app changes
- Detect new screens that should have deep links but don't

## Documenting Deep Links

The knowledge base includes a `deep-links/` template for each link:

```yaml
deep_link: "Product Detail"
url_scheme: "myapp://product/{id}"
universal_link: "https://myapp.com/product/{id}"
skips_screens: ["home", "category"]
lands_on: "[[screens/product-detail]]"
preconditions: ["app must be installed"]
```

This captures both the custom scheme and universal link form, which screens the link bypasses, and where it lands. Your agent uses this to test both paths and verify the landing screen.

## Tips

- **Test both paths.** A custom scheme working doesn't mean the universal link works. They fail independently.
- **Test cold launch vs warm launch.** Does the deep link work when the app isn't running? What about when it's backgrounded on a different screen?
- **Test on real devices for universal links.** Simulator behavior for AASA verification doesn't always match real devices. The OS may cache verification results differently.
- **Check the verification files.** Use `open_url("https://myapp.com/.well-known/apple-app-site-association")` in a browser on the simulator/emulator to verify the file is accessible and correctly formatted.
- **Watch for redirect chains.** If your domain redirects (www → non-www, HTTP → HTTPS), make sure the AASA/assetlinks file is accessible at the final domain without redirects.
- **Version your deep links.** When path patterns change, old links in emails and push notifications will break. Test that old patterns either still work or fail gracefully.
