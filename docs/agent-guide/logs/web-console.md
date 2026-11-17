# A web page's console

*`quern://guide/logs/web-console` -- part of `quern://guide/logs` in the Quern agent guide. Start from `quern://guide`.*

A web page's `console.log` output and its JavaScript errors don't reach the device log, so `query_logs` and
`get_errors` never show them. `get_web_console` reads them from the page itself: inside an app's
`WKWebView` or Safari, on an iOS simulator or a physical iPhone. A phone needs no Xcode, but
Settings > Apps > Safari > Advanced > Web Inspector must be on; the error says so when it is off.

## What it reads

WebKit keeps each page's recent console messages, about the last 100, and quern reads what is there. That
includes what was logged before you asked, so you can read after the fact: do the thing, then read the
console. On a page that logs a lot, the oldest are gone. The answer carries a `note` when it read 80 or more.

Each message has:

- `level`: `log`, `info`, `debug`, `warning` or `error`.
- `text`: every argument, not only the first. An object reads by its description (`Object`, `Array`). To see
  inside one, use `evaluate_web_js`.
- `source`: `console-api` for a console call, or `javascript` for an uncaught error or an unhandled Promise
  rejection.
- `timestamp`: UTC. Messages from before your action have earlier ones.
- `url`, `line` and `column`, where the page gave them.

`levels: ["error"]` keeps only the errors. `read` says how many there were before the filter.

## Which page

As with `evaluate_web_js`: `page_id` (with `bundle_id`, if two apps list the same id) as `get_web_content`
lists them under `pages`. Or leave it out for the one page on screen.

Nothing stays attached to the page after a read, so Safari's Develop menu can still inspect it.
