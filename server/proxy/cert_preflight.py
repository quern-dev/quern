"""Correlate system-proxy capture with per-device certificate trust.

Both halves of the broken state are Quern's own: it configures the proxy, and
it installs the CA. Only the correlation was missing, so enabling capture
against a device that does not trust the CA produced silent HTTPS failures
whose symptom -- a blank screen, an app with no network -- points nowhere near
the proxy. See docs/proposals/cert-preflight-on-launch.md for the analysis.

Scoped to booted simulators on purpose. The macOS system proxy is what
simulators route through, because they share the host's network stack. A
physical device is proxied by its own Wi-Fi configuration instead, so it is
unaffected by this call and reporting it here would be noise.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("quern-debug-server.cert-preflight")


async def simulators_without_cert(controller) -> list[dict[str, str]]:
    """Booted simulators that do not trust the mitmproxy CA.

    Returns a list of ``{"udid", "name"}``, empty when every booted simulator
    trusts it -- which is the common case and the one that must stay fast.

    A device absent from cert-state.json has never had a cert installed, so
    absence and ``cert_installed: false`` mean the same thing here.

    Queries the TrustStore every time, and never quern's own record of what it
    last installed. Erasing a simulator recreates its TrustStore empty while
    leaving that record saying the cert is installed, so the record is exactly
    wrong in the one case this function exists to catch -- reported from the
    field with a record 10.5 hours older than the erase that invalidated it.
    The hour-long cache inside ``is_cert_installed`` is not good enough either:
    it merely shrinks the window to an hour, and an erase is most often
    followed by going straight back to work. The query costs 0.6 ms against a
    local SQLite file, which is not a latency worth trading truth for.

    Never raises. A preflight that fails closed would block capture over its
    own bug, which is worse than the failure it prevents; on any error it
    reports nothing missing and lets the call proceed.
    """
    if controller is None:
        return []

    try:
        from server.models import DeviceState, DeviceType
        from server.proxy import cert_manager

        devices = await controller.list_devices()

        missing = []
        for d in devices:
            if d.device_type != DeviceType.SIMULATOR or d.state != DeviceState.BOOTED:
                continue
            # `is_cert_installed` asks the device, always. It used to take a
            # `verify` flag guarding an hour-long cache, and reading that cache
            # here let an erase go unnoticed for an hour -- measured three
            # minutes after `simctl erase`, with the TrustStore empty and this
            # preflight reporting nothing missing. The cache is gone (ADR 1).
            # Per device, not around the loop. A single failing TrustStore
            # query used to reach the outer handler and return `[]`, throwing
            # away every device already *confirmed* untrusted -- so one
            # unreadable device silently un-refused capture for all the others,
            # and the gate opened on exactly the state it exists to catch.
            # Failing open is the right call for a device we could not check;
            # it is never right for one we could.
            try:
                trusted = await cert_manager.is_cert_installed(
                    controller, d.udid, device_name=d.name,
                )
            except Exception as e:
                logger.debug(
                    "Could not check the CA on %s (%s), skipping it: %s",
                    d.name, d.udid[:8], e,
                )
                continue
            if not trusted:
                missing.append({"udid": d.udid, "name": d.name})
        return missing
    except Exception as e:
        logger.debug("Cert preflight could not run, allowing the call: %s", e)
        return []


def trust_is_stale(udid: str, cert_data: dict, untrusted_udids: set[str]) -> bool:
    """Whether a stored `cert_installed: true` is contradicted by a live check.

    A function rather than an expression inline in the endpoint, because an
    expression inside a response builder is not reachable by any test. Both
    conditions matter and inverting either is silent:

    - Only for a device actually checked and found wanting. `untrusted_udids`
      comes from `simulators_without_cert`, which covers booted simulators; a
      shutdown one is never checked, so absence means "not contradicted"
      rather than "verified".
    - Only when we recorded it installed. A device that never had a cert is
      not *stale*, and saying so would send someone looking for an erase that
      never happened.
    """
    return udid in untrusted_udids and bool(cert_data.get("cert_installed"))


def refusal_detail(missing: list[dict[str, str]]) -> dict:
    """The structured body returned when capture would fail silently.

    Names every resolution rather than only the obvious one. A response that
    offers just "install the certificate" railroads everyone into trusting a
    MITM root CA, which is a larger and more persistent commitment than the
    proxy toggle that prompted it.
    """
    return {
        "error": "capture_without_cert",
        "message": (
            f"{len(missing)} booted simulator(s) do not trust the mitmproxy CA. "
            "HTTPS from them will fail with no indication that the proxy is the "
            "cause."
        ),
        "devices": missing,
        "resolutions": [
            {
                "action": "install_proxy_cert",
                "detail": "Install the CA on each device listed, then retry.",
            },
            {
                "action": "set_auto_install_cert",
                "detail": (
                    "Set auto_install_cert in ~/.quern/config.json to install it "
                    "automatically from now on. Reported by proxy_status, and "
                    "removable with quern uninstall."
                ),
            },
            {
                "action": "skip_cert_check",
                "detail": (
                    "Pass skip_cert_check to configure the proxy anyway -- "
                    "correct when you are deliberately exercising TLS failure."
                ),
            },
        ],
    }
