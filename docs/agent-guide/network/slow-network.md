# Slowing the network on a simulator

*`quern://guide/network/slow-network` -- part of `quern://guide/network` in the Quern agent guide. Start from `quern://guide`.*

`set_device_condition`'s network profiles are for physical iPhones only (`quern://guide/performance`). A simulator uses the Mac's network, so there are two ways, and neither is a quern setting:

- **One endpoint:** intercept it (`set_intercept` with `simulator_udid`), wait for the request with `list_held_flows`, and release it with `release_flow` once the delay you want has passed. The hold is the latency, and every other request is untouched. A held flow is released automatically after 30 seconds, so this reaches delays up to that and no longer; for a longer stall, or a timeout test, use Network Link Conditioner below. Clear it with `clear_intercept` when done.
- **Everything:** Network Link Conditioner, from Apple's Additional Tools for Xcode. It slows the whole Mac, every app and every simulator on it, so ask the user before turning it on, and turn it off afterwards.
