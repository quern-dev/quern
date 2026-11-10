# When something is wrong

*`quern://guide/network/troubleshooting` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

**"Proxy not running"** — Check with `proxy_status` and call `start_proxy` if needed.

**"No flows captured"** — Check `proxy_status` first, in this order:

1. `tls_rejections` — clients that refused the certificate we offered, with the host, the resolved process and simulator UDID, and the TLS alert verbatim. This is the direct evidence, and it distinguishes the two causes that look identical from the outside: `unknown ca` means the device does not trust the CA, while any other alert on a device that does usually means the app pins its certificate. Don't reinstall a certificate to fix pinning.
2. A `capture_without_cert` warning, and `cert_trust_stale` on a device in `cert_setup` — the second means that device's recorded `cert_installed: true` is contradicted by the device itself, which is what an erase leaves behind.
3. Otherwise: if `local_capture` is non-empty, simulator traffic should be captured automatically — verify certs with `verify_proxy_setup`. Check that the list actually contains the process making the requests — the web-view minimum is kept for you, but anything else you previously named is replaced rather than merged. If local capture is not enabled, the device may not be configured to route through the proxy; check `proxy_setup_guide` for device configuration steps.

Check that the flows weren't evicted first: `truncated` and `complete_after` on the answer say whether the window is whole, and `since` narrows the query to when the app made its requests. Under local capture, check `simulator_tls` next: a simulator that doesn't trust the CA has its HTTPS passed through, which produces neither flows nor TLS rejections. Otherwise, an empty `tls_rejections` with no flows usually means the traffic is not reaching the proxy at all, rather than being refused once it does — a routing problem, not a certificate one. Check `list_pending_requests` first: a request the server never answers has reached the proxy, but it only appears in `query_flows` once its response arrives or it fails.
