"""Secrets in captured traffic, kept out of what quern shows (#742).

The module on its own: what a placeholder keeps and hides, the fingerprint,
the settings, and the way back. The surfaces that use it -- every flow
answer, the log lines, recordings, the intercept and replay inputs -- are
tested where they are, in tests/test_redact_surfaces.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from server.models import FlowRecord, FlowRequest, FlowResponse
from server.proxy import redact

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """Each test with its own key and memory, and no config."""
    from server import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "USER_CONFIG_FILE", tmp_path / "config.json")
    redact._forget_for_tests()
    yield
    redact._forget_for_tests()


def _config(tmp_path, proxy: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"proxy": proxy}))


def _flow(headers=None, url="https://api.example.com/v1/me?token=abc123&page=2",
          response_headers=None) -> FlowRecord:
    host, _, rest = url.split("://", 1)[1].partition("/")
    path = "/" + rest
    return FlowRecord(
        id="f1", timestamp=datetime.now(UTC),
        request=FlowRequest(method="GET", url=url, host=host, path=path,
                            headers=headers or {}),
        response=FlowResponse(status_code=200, headers=response_headers or {}),
    )


# -- what a placeholder shows -----------------------------------------------------


class TestWhatIsShown:
    def test_a_bearer_token_keeps_its_scheme_and_length(self):
        shown, names = redact.redact_flow(_flow({"Authorization": f"Bearer {TOKEN}"}))
        value = shown.request.headers["Authorization"]
        assert value.startswith("Bearer ‹redacted, ")
        assert f"{len(TOKEN):,} chars" in value and TOKEN not in value
        assert names == ["authorization", "token"]

    def test_the_stored_flow_is_not_changed(self):
        """Replay and mocks read the stored record: it must keep the real value."""
        flow = _flow({"Authorization": f"Bearer {TOKEN}"})
        redact.redact_flow(flow)
        assert flow.request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "token=abc123" in flow.request.url

    def test_header_names_match_case_insensitively(self):
        shown, _ = redact.redact_flow(_flow({"x-API-key": "k-123"}))
        assert "k-123" not in shown.request.headers["x-API-key"]

    def test_a_cookie_keeps_its_names_and_hides_its_values(self):
        shown, _ = redact.redact_flow(_flow({"Cookie": "session=s3cr3t; theme=dark"}))
        value = shown.request.headers["Cookie"]
        assert value.startswith("session=‹redacted") and "; theme=‹redacted" in value
        assert "s3cr3t" not in value

    def test_a_set_cookie_keeps_its_attributes(self):
        shown, _ = redact.redact_flow(_flow(response_headers={
            "Set-Cookie": "sid=abc; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly"}))
        value = shown.response.headers["Set-Cookie"]
        assert value.startswith("sid=‹redacted") and "abc;" not in value
        assert "; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly" in value

    def test_only_secret_query_parameters_change(self):
        shown, _ = redact.redact_flow(_flow())
        assert shown.request.url.startswith("https://api.example.com/v1/me?token=‹redacted")
        assert shown.request.url.endswith("&page=2")
        assert "abc123" not in shown.request.path and "&page=2" in shown.request.path

    def test_other_headers_pass_untouched(self):
        shown, names = redact.redact_flow(
            _flow({"Accept": "application/json"}, url="https://api.example.com/x"))
        assert shown.request.headers == {"Accept": "application/json"} and names == []


# -- the fingerprint --------------------------------------------------------------


class TestTheFingerprint:
    def _id(self, value: str) -> str:
        shown, _ = redact.redact_flow(_flow({"X-Api-Key": value}, url="https://e.com/"))
        return shown.request.headers["X-Api-Key"].rsplit("id ", 1)[1].rstrip("›")

    def test_equal_values_share_an_id_and_different_ones_do_not(self):
        assert self._id("one") == self._id("one") != self._id("two")

    def test_it_is_keyed_to_this_server(self, tmp_path):
        """A plain hash would make a short secret guessable from its id."""
        before = self._id("hunter2")
        redact._forget_for_tests()
        (tmp_path / "redaction-key").unlink()             # another server's key
        assert self._id("hunter2") != before

    def test_the_key_is_private(self, tmp_path):
        self._id("x")
        assert (tmp_path / "redaction-key").stat().st_mode & 0o777 == 0o600


# -- reveal and the settings ------------------------------------------------------


class TestRevealAndSettings:
    def test_reveal_true_shows_everything(self):
        shown, names = redact.redact_flow(_flow({"Authorization": "Bearer t"}), reveal=True)
        assert shown.request.headers["Authorization"] == "Bearer t" and names == []

    def test_a_reveal_list_narrows_to_its_names(self):
        flow = _flow({"Authorization": "Bearer t", "Cookie": "a=1"})
        shown, names = redact.redact_flow(flow, reveal="authorization")
        assert shown.request.headers["Authorization"] == "Bearer t"
        assert "a=1" not in shown.request.headers["Cookie"]
        assert names == ["cookie", "token"]

    def test_redact_false_turns_it_off(self, tmp_path):
        _config(tmp_path, {"redact": False})
        shown, names = redact.redact_flow(_flow({"Authorization": "Bearer t"}))
        assert shown.request.headers["Authorization"] == "Bearer t" and names == []

    @pytest.mark.parametrize("value", ["false", 0, None, "no"])
    def test_only_a_literal_false_turns_it_off(self, tmp_path, value):
        """A typo must never mean "show every token"."""
        _config(tmp_path, {"redact": value})
        shown, _ = redact.redact_flow(_flow({"Authorization": "Bearer t"}))
        assert shown.request.headers["Authorization"] != "Bearer t"

    def test_extra_names_add_to_the_defaults(self, tmp_path):
        """A list that replaced them, written to add one header, would stop
        redacting Authorization."""
        _config(tmp_path, {"redact_headers": ["X-Shazam-Token"], "redact_params": ["sess"]})
        flow = _flow({"X-Shazam-Token": "z", "Authorization": "Bearer t"},
                     url="https://e.com/a?sess=1")
        shown, names = redact.redact_flow(flow)
        assert shown.request.headers["X-Shazam-Token"] != "z"
        assert shown.request.headers["Authorization"] != "Bearer t"
        assert "sess=1" not in shown.request.url

    def test_a_malformed_proxy_section_keeps_the_defaults(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"proxy": "on"}))
        shown, _ = redact.redact_flow(_flow({"Authorization": "Bearer t"}))
        assert shown.request.headers["Authorization"] != "Bearer t"

    def test_a_log_line_has_its_query_redacted(self):
        assert redact.redact_text("GET /v1/me?token=abc&page=2") \
            .startswith("GET /v1/me?token=‹redacted")


# -- the way back -----------------------------------------------------------------


class TestRestore:
    def test_a_placeholder_shown_comes_back_as_the_real_value(self):
        shown, _ = redact.redact_flow(_flow({"Authorization": f"Bearer {TOKEN}"}))
        back, names = redact.restore_headers(dict(shown.request.headers))
        assert back["Authorization"] == f"Bearer {TOKEN}" and names == ["Authorization"]

    def test_every_cookie_comes_back(self):
        shown, _ = redact.redact_flow(_flow({"Cookie": "a=1; b=2"}))
        back, _ = redact.restore(shown.request.headers["Cookie"])
        assert back == "a=1; b=2"

    def test_a_url_comes_back(self):
        shown, _ = redact.redact_flow(_flow())
        back, count = redact.restore(shown.request.url)
        assert back == "https://api.example.com/v1/me?token=abc123&page=2" and count == 1

    def test_a_placeholder_this_server_never_showed_is_refused(self):
        with pytest.raises(redact.UnknownPlaceholder, match="not known to this server"):
            redact.restore("Bearer ‹redacted, 9 chars, id 0123456789ab›")

    def test_real_values_pass_through(self):
        assert redact.restore_headers({"Authorization": "Bearer real"}) == (
            {"Authorization": "Bearer real"}, [])


# -- review findings --------------------------------------------------------------


class TestReviewFindings:
    def test_a_captured_value_holding_placeholder_text_is_still_redacted(self):
        """The app's text is untrusted: a placeholder-looking part must not
        exempt the real secret beside it."""
        fake = "‹redacted, 1 chars, id 0123456789ab›"
        shown, _ = redact.redact_flow(_flow({"Cookie": f"sid=live-secret; debug={fake}"},
                                            url=f"https://e.com/a?token=raw{fake}"))
        assert "live-secret" not in shown.request.headers["Cookie"]
        assert "raw" not in shown.request.url.split("token=", 1)[1][:4]

    def test_only_authorization_headers_keep_a_scheme(self):
        shown, _ = redact.redact_flow(_flow({"X-Api-Key": "live-secret suffix",
                                             "Proxy-Authorization": "Basic abc"},
                                            url="https://e.com/"))
        assert shown.request.headers["X-Api-Key"].startswith("‹redacted")
        assert shown.request.headers["Proxy-Authorization"].startswith("Basic ‹redacted")

    def test_a_name_list_holding_true_is_not_reveal_all(self, tmp_path):
        """What the MCP sends for reveal: ["true"] -- a trailing comma."""
        _config(tmp_path, {"redact_params": ["true"]})
        flow = _flow({"Authorization": "Bearer t"}, url="https://e.com/a?true=1")
        shown, names = redact.redact_flow(flow, reveal="true,")
        assert "true=1" in shown.request.url
        assert shown.request.headers["Authorization"] != "Bearer t"
        assert "authorization" in names

    def test_a_value_that_is_one_whole_placeholder_is_left_alone(self):
        """A trace built from a redacted recording reads placeholders back:
        redacted again, they would show a new id and length, and restore to
        the placeholder rather than the real value (#750 review)."""
        shown, _ = redact.redact_flow(_flow({"Authorization": f"Bearer {TOKEN}",
                                             "Cookie": "sid=abc"}))
        again, _ = redact.redact_flow(shown)
        assert again.request.headers == shown.request.headers
        assert again.request.url == shown.request.url
        assert redact.redact_text(shown.request.path) == shown.request.path
        back, _ = redact.restore(again.request.headers["Authorization"])
        assert back == f"Bearer {TOKEN}"
