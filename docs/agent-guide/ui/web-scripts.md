# Running JavaScript in a web page

*`quern://guide/ui/web-scripts` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

`evaluate_web_js` runs a script of yours in a web page -- Safari, or an app's
`WKWebView` -- and returns what it gave. Use it when the DOM `get_web_content`
reads does not say what you need: a JavaScript variable, `localStorage`, a
framework's store, whether a request finished. Or use it to act on the page:
set a value, call a page function, change the DOM.

It works on an iOS simulator, and on a physical iPhone with no Xcode, through
the phone's own Web Inspector. On a phone, Settings > Apps > Safari > Advanced >
Web Inspector must be on; the error says so when it is off. Android's WebView is
not reachable this way.

**It acts on the page.** What the script changes stays changed, and every call
is logged as an action with its script, so a trace shows what was run. The web
overlay from `get_web_content` is dropped afterwards, since the page may have
changed: read it again before tapping web elements.

## Choosing the page

Without `page_id`, the script runs in the one page on screen. With none on
screen, or several, the error lists the pages, and you pick one by `page_id`
(and `bundle_id`, if two apps list the same id), as `get_web_content` lists them
under `pages`. On a simulator a page in the background can be named: a Safari
tab behind another answers, and reports `document.visibilityState` as
`hidden`. On a phone only pages on screen are listed, since a tab in the
background does not answer there.

## What comes back

The value of a script is its last expression statement, as in a console:
`document.title` or `var n = 2; n * 21`.

- `threw: true` with `exception` -- the script threw. That is an answer, not a
  tool failure: read the text.
- `type` and `value` -- a primitive, or an object or array that serialises as
  JSON.
- `type: "node"`, `value: null` and `description` (`<input id="q">`) -- a DOM
  node, which has no JSON value. Return what you want from it instead:
  `document.getElementById('q').value`.
- `note` -- an object that cannot be serialised, such as one with a cycle.
- `awaited: true` -- the script returned a Promise, and this is what it
  settled to. A rejection reads as a throw. So async work is an async function
  called at the end: `(async () => (await fetch('/api/me')).status)()`.

`timeout` (default 10 s, at most 60) bounds the script and the Promise it
returns. A page that does not answer in time is an error, and whatever the
script changed before then stays changed.
