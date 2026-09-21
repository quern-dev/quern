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

import asyncio
import logging

logger = logging.getLogger(__name__)


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
    There was an hour-long cache inside ``is_cert_installed``, which merely
    shrank that window to an hour -- and an erase is most often followed by
    going straight back to work. It has since been deleted outright (ADR 1).

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
                    "Pass skip_cert_check to proceed anyway -- correct when "
                    "you are deliberately exercising TLS failure. Three "
                    "endpoints share this refusal, so it does not name one."
                ),
            },
        ],
    }


async def warn_if_capture_lacks_trust(controller, processes: list[str]) -> list[dict[str, str]]:
    """Say so at startup when local capture is on and the CA is not trusted.

    The boot-time counterpart to `_ensure_ca_is_trusted`, and deliberately a
    weaker thing: it warns where the gate refuses.

    It has to be weaker. The two API paths that begin routing can refuse,
    because the caller is right there and a 428 is an answer to a request. This
    path has no request. `quern enable-local-capture` writes `config.json` and
    tells you to restart, and the lifespan builds the adapter from that file --
    so by the time anyone can check trust, the decision was made in a previous
    process, possibly days ago, against simulators that were not booted then.
    Refusing to start the server over a certificate would take the whole debug
    server down for a condition affecting one device, which is a worse outcome
    than the silent HTTPS failure it would prevent.

    So the point is only that the state stops being silent. Before this,
    `quern enable-local-capture MyApp` against an untrusting simulator produced
    a server that started cleanly, printed `Local capture: MyApp`, and captured
    nothing decryptable -- with the sole report of the condition being a
    `capture_without_cert` warning in `proxy_status`, which is HTTP-only and so
    invisible to the person who ran the CLI command.

    One exception to "warns rather than acts": with `auto_install_cert` set the
    user has already answered this question, so it installs, exactly as the
    four gates do. Otherwise this would be the single path where opting into
    "handle it for me" still produced broken capture -- and the only one whose
    complaint goes to a log rather than to a caller.

    Returns the devices still untrusting afterwards, so a caller can test the
    decision rather than the log line.
    """
    if not processes:
        return []

    missing = await simulators_without_cert(controller)
    if not missing:
        return []

    # The setting is consent, and it means the same thing here as at the four
    # gates: Quern handles it from now on. Warning instead would make this the
    # one path where "handle it for me" produced a log line and broken capture
    # -- and it is the path with no caller to read the log line.
    from server.config import get_auto_install_cert

    if get_auto_install_cert():
        from server.proxy.cert_manager import install_cert

        installed, failed = [], []
        for dev in missing:
            try:
                # Shielded: this runs in the startup warmup task, which is
                # cancelled at shutdown. `install_cert` shells out to simctl and
                # *then* records what it did, and cancelling between those two
                # leaves the TrustStore holding a certificate the record calls
                # absent -- cancelling the await does not stop the subprocess
                # that already ran. The shield lets the write finish.
                #
                # The stranded state would be self-correcting, since nothing
                # trusts the record any more (ADR 1) and the next check asks the
                # device. But "it heals later" is a reason not to panic, not a
                # reason to write it.
                await asyncio.shield(
                    install_cert(controller, dev["udid"], device_name=dev["name"]),
                )
                installed.append(dev)
            except Exception as e:
                failed.append((dev, e))
                logger.warning(
                    "auto_install_cert is set but installing the CA on %s (%s) "
                    "failed, so HTTPS from it will not be captured: %s",
                    dev["name"], dev["udid"][:8], e,
                )
        if installed:
            logger.info(
                "Auto-installed the mitmproxy CA on %s for local capture",
                ", ".join(f"{d['name']} ({d['udid'][:8]})" for d in installed),
            )
        return [d for d, _ in failed]

    logger.warning(
        "Local capture is enabled for %s, but %s do(es) not trust the mitmproxy "
        "CA. Every HTTPS request from those simulators will fail, and nothing in "
        "the app will point at the proxy as the cause. Install it with the "
        "install_proxy_cert tool, or run `quern set-auto-install-cert on` to have "
        "Quern handle it from now on.",
        ", ".join(processes),
        ", ".join(f"{d['name']} ({d['udid'][:8]})" for d in missing),
    )
    return missing
