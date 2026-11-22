"""Secret fields in captured bodies, redacted in what quern shows (#749).

The scanner on its own (`server.proxy.redact_body`), then what the flow
answers show, the way back on each path that sends a body, mocks and
recordings. Headers and query strings are tested in tests/test_redact.py and
tests/test_redact_surfaces.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from server.models import FlowRecord, FlowRequest, FlowResponse
from server.proxy import redact
from server.proxy.redact_body import FIELDS, kind_of, redact_json
from tests.test_redact_surfaces import _call, _sent, app, running  # noqa: F401

TOKEN_RESPONSE = ('{\n  "access_token" : "eyJhbGciOi.payload.sig",\n'
                  '  "token_type": "Bearer",\n  "expires_in": 3600,\n'
                  '  "refresh_token":"r3fr3sh\\"quoted"\n}')
LOGIN = '{"query":"mutation Login","variables":{"input":{"email":"a@b.c","password":"hunter2"}}}'


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    from server import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "USER_CONFIG_FILE", tmp_path / "config.json")
    redact._forget_for_tests()
    yield
    redact._forget_for_tests()


def _flow(fid="b1", request_body=None, request_type="application/json",
          response_body=None, response_type="application/json", truncated=False,
          encoding="utf-8") -> FlowRecord:
    req_headers = {"Content-Type": request_type} if request_type else {}
    return FlowRecord(
        id=fid, timestamp=datetime.now(UTC),
        request=FlowRequest(method="POST", url="https://auth.example.com/token",
                            host="auth.example.com", path="/token", headers=req_headers,
                            body=request_body, body_size=len(request_body or "")),
        response=FlowResponse(status_code=200, headers={"Content-Type": response_type},
                              body=response_body, body_size=len(response_body or "") + 7,
                              body_truncated=truncated, body_encoding=encoding),
    )


def _scan(text: str, shown=frozenset()) -> tuple[str, set[str]]:
    names: set[str] = set()
    out = redact_json(text, FIELDS, frozenset(shown), lambda r: f"‹R:{r}›",
                      lambda r: f"‹L:{r}›", names)
    return out, names


# -- the scanner ------------------------------------------------------------------


class TestTheScanner:
    def test_only_the_secret_values_change(self):
        """Edited in place: everything outside each secret's value is byte for
        byte as captured -- whitespace, key order, escapes."""
        out, names = _scan(TOKEN_RESPONSE)
        assert names == {"access_token", "refresh_token"}
        jwt, refresh = "eyJhbGciOi.payload.sig", 'r3fr3sh\\"quoted'
        assert out == TOKEN_RESPONSE.replace(jwt, f"‹R:{jwt}›").replace(refresh, f"‹R:{refresh}›")

    def test_names_match_at_any_depth_and_in_any_case_or_escape(self):
        out, names = _scan('{"a":[{"b":{"PassWord":"x"}}],"pass\\u0077d":"y","passwd":"z"}')
        assert out == '{"a":[{"b":{"PassWord":"‹R:x›"}}],"pass\\u0077d":"‹R:y›","passwd":"‹R:z›"}'
        assert names == {"password", "passwd"}

    def test_graphql_variables_are_walked(self):
        out, _ = _scan(LOGIN)
        assert '"password":"‹R:hunter2›"' in out and '"email":"a@b.c"' in out

    def test_a_literal_under_a_secret_name_becomes_a_placeholder_string(self):
        out, _ = _scan('{"password": 1234, "token": true, "expires_in": 5}')
        assert out == '{"password": "‹L:1234›", "token": "‹L:true›", "expires_in": 5}'

    def test_an_array_under_a_secret_name_has_its_values_replaced(self):
        out, _ = _scan('{"secret":["a",2,{"x":"keep"}]}')
        assert out == '{"secret":["‹R:a›","‹L:2›",{"x":"keep"}]}'

    def test_a_value_is_only_a_value(self):
        """A secret name as a *value*, or a key that merely contains one, is
        not a field to redact."""
        out, names = _scan('{"field":"password","password_hint":"pet","key":"theme"}')
        assert out == '{"field":"password","password_hint":"pet","key":"theme"}' and not names

    def test_a_body_cut_mid_value_is_redacted_to_the_cut(self):
        out, names = _scan('{"x":1,"access_token":"eyJhbG')
        assert out == '{"x":1,"access_token":"‹R:eyJhbG›' and names == {"access_token"}

    def test_reveal_names_are_left_alone(self):
        out, names = _scan('{"password":"p","token":"t"}', shown={"password"})
        assert out == '{"password":"p","token":"‹R:t›"}' and names == {"token"}

    def test_malformed_text_does_not_raise(self):
        for text in ['{"password":', '}}]]', '{"a" "b"}', '"just a string"', '{"password":"\\']:
            _scan(text)

    @pytest.mark.parametrize(("ctype", "body", "kind"), [
        ("application/json; charset=utf-8", "{}", "json"),
        ("application/vnd.api+json", "{}", "json"),
        ("application/x-www-form-urlencoded", "a=1", "form"),
        ("text/plain", ' {"a":1}', "json"),               # mislabelled
        ("", "[1]", "json"),                              # untyped
        ("text/plain", "hello", None),
        ("text/html", "{not json}", None),                # declared, so believed
        ("multipart/form-data; boundary=x", "--x", None),
    ])
    def test_what_is_scanned(self, ctype, body, kind):
        assert kind_of(body, ctype, "utf-8") == kind

    def test_a_binary_body_is_not_scanned(self):
        assert kind_of("eyJ0b2tlbiI6", "application/json", "base64") is None


# -- what a flow answer shows -----------------------------------------------------


class TestWhatIsShown:
    def test_a_token_response_is_redacted_and_the_stored_flow_is_not(self):
        flow = _flow(response_body=TOKEN_RESPONSE)
        shown, names = redact.redact_flow(flow)
        assert "eyJhbGciOi" not in shown.response.body and "r3fr3sh" not in shown.response.body
        assert json.loads(shown.response.body)["token_type"] == "Bearer"   # still JSON
        assert {"access_token", "refresh_token"} <= set(names)
        assert flow.response.body == TOKEN_RESPONSE                        # stored: real
        assert shown.response.body_size == flow.response.body_size         # the real size

    def test_a_form_login_is_redacted(self):
        shown, names = redact.redact_flow(_flow(
            request_body="grant_type=password&username=me&password=hunter2&client_secret=cs1",
            request_type="application/x-www-form-urlencoded"))
        body = shown.request.body
        assert body.startswith("grant_type=password&username=me&password=‹redacted")
        assert "hunter2" not in body and "cs1" not in body
        assert {"password", "client_secret"} <= set(names)

    def test_a_truncated_response_is_scanned_up_to_the_cut(self):
        shown, _ = redact.redact_flow(_flow(response_body='{"access_token":"eyJhbGci',
                                            truncated=True))
        assert "eyJhbGci" not in shown.response.body

    def test_config_adds_body_fields(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"proxy": {"redact_fields": ["PIN"]}}))
        shown, _ = redact.redact_flow(_flow(request_body='{"pin":"0000","password":"p"}'))
        assert "0000" not in shown.request.body and '"p"' not in shown.request.body

    def test_reveal_by_a_body_fields_name(self):
        shown, names = redact.redact_flow(_flow(response_body=TOKEN_RESPONSE),
                                          reveal=["access_token"])
        assert "eyJhbGciOi.payload.sig" in shown.response.body
        assert "r3fr3sh" not in shown.response.body and "access_token" not in names

    def test_redact_false_shows_bodies_too(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"proxy": {"redact": False}}))
        shown, _ = redact.redact_flow(_flow(response_body=TOKEN_RESPONSE))
        assert shown.response.body == TOKEN_RESPONSE

    def test_a_body_that_cannot_be_scanned_is_named_and_an_image_is_not(self):
        flows = [_flow("m", request_body="--x\r\npassword=1", request_type="multipart/form-data"),
                 _flow("i", response_body="iVBOR", response_type="image/png", encoding="base64"),
                 _flow("h", response_body="<html>", response_type="text/html")]
        _, _, unscanned = redact.shown_flows(flows)
        assert unscanned == ["request body (multipart/form-data)", "response body (text/html)"]
        note = redact.note([], unscanned)
        assert "multipart/form-data" in note and "image/png" not in note


async def test_a_flow_answer_names_the_bodies_it_could_not_scan(app):  # noqa: F811
    await app.state.flow_store.add(_flow("u1", request_body="--x\r\nsecret",
                                         request_type="multipart/form-data",
                                         response_body=TOKEN_RESPONSE))
    body = (await _call(app, "GET", "/api/v1/proxy/flows")).json()
    flow = body["flows"][0]
    assert "eyJhbGciOi" not in flow["response"]["body"]
    assert "access_token" in body["redacted"]
    assert "request body (multipart/form-data)" in body["redaction_note"]


# -- the way back -----------------------------------------------------------------


async def _shown_body(app, flow, side="request"):  # noqa: F811
    await app.state.flow_store.add(flow)
    return (await _call(app, "GET", f"/api/v1/proxy/flows/{flow.id}")).json()[side]["body"]


async def test_a_replay_with_the_shown_body_sends_the_real_one(app, running):  # noqa: F811
    from unittest.mock import AsyncMock, patch

    shown = await _shown_body(app, _flow("r1", request_body='{"password": 1234, "u": "me"}'))
    assert "1234" not in shown
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=AsyncMock(status_code=200))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = client
        resp = await _call(app, "POST", "/api/v1/proxy/replay/r1",
                           json={"modify_body": shown.replace('"me"', '"you"')})
    assert resp.status_code == 200, resp.text
    sent = client.request.call_args.kwargs["content"].decode()
    assert sent == '{"password": 1234, "u": "you"}'          # the number, not a string
    assert resp.json()["restored"] == ["body"]


async def test_a_release_with_the_shown_body_sends_the_real_one(app, running):  # noqa: F811
    running._held_flows["h1"] = {
        "id": "h1", "held_at": datetime.now(UTC),
        "request": {"method": "POST", "url": "https://e.com/login", "host": "e.com",
                    "path": "/login", "headers": {"Content-Type": "application/json"},
                    "body": '{"password":"hunter2"}'}}
    held = (await _call(app, "GET", "/api/v1/proxy/intercept/held")).json()
    shown = held["held_flows"][0]["request"]["body"]
    assert "hunter2" not in shown and "password" in held["redacted"]
    resp = await _call(app, "POST", "/api/v1/proxy/intercept/release",
                       json={"flow_id": "h1", "modifications": {"body": shown}})
    assert resp.status_code == 200, resp.text
    (sent,) = _sent(running, "modify_and_release")
    assert sent["modifications"]["body"] == '{"password":"hunter2"}'
    assert resp.json()["restored"] == ["body"]


async def test_a_mock_set_from_a_shown_body_holds_the_real_one_and_lists_it_redacted(
        app, running):  # noqa: F811
    shown = await _shown_body(app, _flow("m1", response_body=TOKEN_RESPONSE), "response")
    resp = await _call(app, "POST", "/api/v1/proxy/mocks", json={
        "pattern": "~d auth.example.com",
        "response": {"status_code": 200, "headers": {"content-type": "application/json"},
                     "body": shown}})
    assert resp.status_code == 200, resp.text
    (sent,) = _sent(running, "set_mock")
    assert sent["response"]["body"] == TOKEN_RESPONSE
    assert "body" in resp.json()["restored"]
    listed = (await _call(app, "GET", "/api/v1/proxy/mocks")).json()
    assert "eyJhbGciOi" not in repr(listed)
    # An update with the body as listed keeps the real values, and is answered redacted.
    rule = listed["rules"][0]
    upd = await _call(app, "PATCH", f"/api/v1/proxy/mocks/{rule['rule_id']}",
                      json={"response": {"body": rule["response"]["body"]}})
    assert upd.status_code == 200, upd.text
    assert _sent(running, "set_mock")[-1]["response"]["body"] == TOKEN_RESPONSE
    assert "eyJhbGciOi" not in repr(upd.json())


async def test_a_body_placeholder_this_server_never_showed_is_refused(app, running):  # noqa: F811
    from unittest.mock import patch

    await app.state.flow_store.add(_flow("r2", request_body="{}"))
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        resp = await _call(app, "POST", "/api/v1/proxy/replay/r2", json={
            "modify_body": '{"password":"‹redacted, 9 chars, id 0123456789ab›"}'})
    assert resp.status_code == 400 and "not known to this server" in resp.text
    cls.assert_not_called()                                   # nothing went out


def test_a_string_and_a_literal_with_the_same_text_do_not_share_an_id():
    """Otherwise a string's placeholder could come back as a number."""
    assert redact.placeholder("1234") != redact.placeholder("1234", literal=True)
    s = redact.placeholder("1234")
    assert redact.restore_body(f'{{"pin":"{s}"}}')[0] == '{"pin":"1234"}'


# -- recordings -------------------------------------------------------------------


def test_a_recorded_body_is_redacted_and_still_shaped():
    from types import SimpleNamespace

    from server.recording.recorder import Filters, RecordingManager

    rec = SimpleNamespace(filters=Filters(max_body_bytes=40))
    line = RecordingManager._record_of(rec, "flow", _flow(response_body=TOKEN_RESPONSE))
    assert "eyJhbGciOi" not in repr(line)
    assert len(line["response"]["body"].encode()) <= 40          # max_body_bytes held
    off = RecordingManager._record_of(SimpleNamespace(filters=Filters(redact=False)), "flow",
                                      _flow(response_body=TOKEN_RESPONSE))
    assert off["response"]["body"] == TOKEN_RESPONSE


async def test_a_replay_with_a_body_of_another_length_sends_its_own_length(app, running):  # noqa: F811
    """The captured Content-Length is the original body's: kept, a changed
    body failed with "Too much data for declared Content-Length" (#749 live
    test). httpx sets the real one when the header is not passed."""
    from unittest.mock import AsyncMock, patch

    flow = _flow("r3", request_body='{"a":1}')
    flow.request.headers["Content-Length"] = "7"
    await app.state.flow_store.add(flow)
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=AsyncMock(status_code=200))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = client
        resp = await _call(app, "POST", "/api/v1/proxy/replay/r3",
                           json={"modify_body": '{"a":12345}'})
    assert resp.status_code == 200, resp.text
    sent = client.request.call_args.kwargs["headers"]
    assert not any(k.lower() == "content-length" for k in sent)


async def test_a_failed_replay_still_says_what_it_restored(app, running):  # noqa: F811
    from unittest.mock import AsyncMock, patch

    shown = await _shown_body(app, _flow("r4", request_body='{"password":"p"}'))
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(side_effect=OSError("refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = client
        resp = await _call(app, "POST", "/api/v1/proxy/replay/r4", json={"modify_body": shown})
    assert resp.json()["status"] == "error" and resp.json()["restored"] == ["body"]
