"""Forgetting a device's recorded proxy configs, across every spelling of it.

`read_cert_state_for_device` returns the canonicalised *merge* of every
spelling a device is filed under, while `update_cert_state` writes to the one
raw key it is handed. Clearing the canonical entry therefore left the other
spelling's configs on disk, and `_canonicalised` unioned them straight back on
the next read -- while the return value, computed from the read *before* the
write, asserted a removal the very next read contradicted.

That is two failures of the same kind stacked: a write that does not take, and
a report that cannot notice. Tested here at the level where both live, with
the aliases real rather than patched to identity -- patching them out is what
made the endpoint test structurally unable to see this.

See #265.
"""

from __future__ import annotations

import json

import pytest

from server.device import devicectl
from server.proxy import cert_state


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cert_state, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cert_state, "CERT_STATE_FILE", tmp_path / "cert-state.json")
    return tmp_path / "cert-state.json"


@pytest.fixture
def aliased(monkeypatch):
    """`RAW-UDID` and `CANON-UDID` are the same device."""
    monkeypatch.setattr(devicectl, "_identity_aliases", {"RAW-UDID": "CANON-UDID"})


def _configs(udid: str):
    return (cert_state.read_cert_state_for_device(udid) or {}).get("wifi_proxy_configs")


class TestForgettingActuallyForgets:
    def test_a_config_under_a_second_spelling_does_not_come_back(
        self, state_file, aliased,
    ):
        state_file.write_text(json.dumps({
            "RAW-UDID": {"wifi_proxy_configs": {
                "MonaLisa": {"proxy_host": "192.168.1.189", "proxy_port": 9177},
            }},
            "CANON-UDID": {"wifi_proxy_configs": {
                "MonaLisa": {"proxy_host": "192.168.1.189", "proxy_port": 9177},
            }},
        }))
        assert _configs("CANON-UDID")

        removed = cert_state.forget_device_proxy_configs("CANON-UDID")

        assert removed == ["MonaLisa"]
        assert not _configs("CANON-UDID")
        assert not _configs("RAW-UDID")

    def test_asking_by_the_other_spelling_works_too(self, state_file, aliased):
        state_file.write_text(json.dumps({
            "RAW-UDID": {"wifi_proxy_configs": {"MonaLisa": {"proxy_host": "1.2.3.4"}}},
            "CANON-UDID": {"wifi_proxy_configs": {"MonaLisa": {"proxy_host": "1.2.3.4"}}},
        }))

        assert cert_state.forget_device_proxy_configs("RAW-UDID") == ["MonaLisa"]
        assert not _configs("CANON-UDID")

    def test_the_return_value_is_what_a_later_read_cannot_see(
        self, state_file, aliased,
    ):
        """Not what the function set out to remove. Computed from the read
        before the write, it was an assertion that could not fail."""
        state_file.write_text(json.dumps({
            "RAW-UDID": {"wifi_proxy_configs": {"Ghost": {"proxy_host": "1.2.3.4"}}},
            "CANON-UDID": {"wifi_proxy_configs": {"Ghost": {"proxy_host": "1.2.3.4"}}},
        }))

        removed = cert_state.forget_device_proxy_configs("CANON-UDID")
        surviving = _configs("CANON-UDID") or {}

        assert set(removed).isdisjoint(surviving)

    def test_several_networks_all_go(self, state_file):
        state_file.write_text(json.dumps({
            "D": {"wifi_proxy_configs": {
                "MonaLisa": {"proxy_host": "1.2.3.4"},
                "CasaCuevas": {"proxy_host": "1.2.3.4"},
            }},
        }))

        assert cert_state.forget_device_proxy_configs("D") == ["CasaCuevas", "MonaLisa"]
        assert not _configs("D")

    def test_another_device_is_untouched(self, state_file):
        state_file.write_text(json.dumps({
            "D1": {"wifi_proxy_configs": {"MonaLisa": {"proxy_host": "1.2.3.4"}}},
            "D2": {"wifi_proxy_configs": {"MonaLisa": {"proxy_host": "5.6.7.8"}}},
        }))

        cert_state.forget_device_proxy_configs("D1")

        assert _configs("D2") == {"MonaLisa": {"proxy_host": "5.6.7.8"}}

    def test_nothing_recorded_is_an_empty_list_not_a_claim(self, state_file):
        state_file.write_text(json.dumps({"D": {"cert_installed": True}}))
        assert cert_state.forget_device_proxy_configs("D") == []

    def test_other_fields_survive(self, state_file):
        """Forgetting the proxy config is not forgetting the device."""
        state_file.write_text(json.dumps({
            "D": {
                "cert_installed": True,
                "fingerprint": "ab:cd",
                "wifi_proxy_configs": {"MonaLisa": {"proxy_host": "1.2.3.4"}},
            },
        }))

        cert_state.forget_device_proxy_configs("D")
        entry = cert_state.read_cert_state_for_device("D") or {}

        assert entry.get("cert_installed") is True
        assert entry.get("fingerprint") == "ab:cd"
