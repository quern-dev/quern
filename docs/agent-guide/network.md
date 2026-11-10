# Network: capture, mocks and intercept

*`quern://guide/network` -- part of the Quern agent guide. Start from `quern://guide`.*

Seeing, changing and faking an app's network traffic.

This topic is split into pages, one per subject. Read the one you need:

- `quern://guide/network/capture` -- Capturing traffic: local capture vs the system proxy, capture sessions, per-simulator filtering, the capture list
- `quern://guide/network/ca-trust` -- CA trust and capture refusals: why capture refuses (428 and the rest), certificates, auto_install_cert
- `quern://guide/network/filtering` -- Filtering flows: query_flows filters (host, method, path, status) and compact detail
- `quern://guide/network/tools` -- Tools for network work: which tool for each network job, including add_session_capture
- `quern://guide/network/intercept` -- Intercept-Modify-Release for Testing Edge Cases: holding a request and releasing it changed
- `quern://guide/network/slow-network` -- Slowing the network on a simulator: latency on a simulator: intercept holds or Network Link Conditioner
- `quern://guide/network/mocks` -- Mocking for Deterministic Testing: set_mock and update_mock, mocking one simulator or Android device, filter syntax
- `quern://guide/network/mistakes` -- Mistakes to avoid: mock vs intercept, per-simulator filtering, leaving the system proxy on
- `quern://guide/network/troubleshooting` -- When something is wrong: the proxy not running, no flows captured
