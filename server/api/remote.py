"""Pairing and the remote listener's status over HTTP (#254, phase 3).

    POST /api/v1/remote/pairing-codes   make a one-time code (owner, local)
    GET  /api/v1/remote/status          the listener, its URL and fingerprint (owner)
    POST /api/v1/remote/pair/hello      the server's half of the proof
    POST /api/v1/remote/pair/finish     the client's half; answers a client key

The two `pair` routes need no key -- a client pairing has none yet -- and
answer only on the remote listener, where there is a certificate for the
proofs to be bound to: anywhere else they read as not there.
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError

from server.api.actions import logged_action
from server.remote.listener import REMOTE
from server.remote.pairing import PairingError

router = APIRouter(prefix="/api/v1/remote", tags=["remote"])

M = TypeVar("M", bound=BaseModel)


def _listener(request: Request):
    listener = getattr(request.app.state, "remote", None)
    if listener is None:
        raise HTTPException(409, ('the remote listener is off: set {"remote": {"enabled": '
                                  'true}} in ~/.quern/config.json and restart quern'))
    if listener.port is None:
        raise HTTPException(503, f"the remote listener is not listening: {listener.error}")
    return listener


class CodeRequest(BaseModel):
    label: str = Field(description="The client key's label, 1-16 of a-z, 0-9 and '-'")


@router.post("/pairing-codes")
@logged_action("create_pairing_code", category="session")
async def create_code(request: Request, body: CodeRequest) -> dict:
    """A one-time pairing code for a client to be labelled `label`."""
    from server.clients import LABEL, ClientKeys

    listener = _listener(request)
    if not LABEL.fullmatch(body.label):
        raise HTTPException(400, "a label is 1-16 of a-z, 0-9 and '-', starting with a "
                                 "letter or digit")
    keys: ClientKeys | None = getattr(request.app.state, "clients", None)
    if keys is not None and keys.is_active(body.label):
        raise HTTPException(409, f"{body.label} already has a client key; revoke it first "
                                 f"(quern clients revoke {body.label}) or choose another label")
    code, seconds = listener.codes.create(body.label)
    from server.remote.cert import show

    return {"code": code, "expires_in": seconds, "label": body.label, "url": listener.url,
            "fingerprint": show(listener.cert.fingerprint)}


@router.get("/status")
async def status(request: Request) -> dict:
    from server.remote.cert import show

    listener = getattr(request.app.state, "remote", None)
    if listener is None:
        return {"enabled": False}
    return {"enabled": True, "listening": listener.port is not None, "url": listener.url,
            "error": listener.error,
            "fingerprint": show(listener.cert.fingerprint) if listener.cert else None}


def _remote_only(request: Request):
    if not request.scope.get(REMOTE):
        raise HTTPException(404, "Not Found")
    return _listener(request)


async def _body(request: Request, model: type[M]) -> M:
    """The request body as `model`, read only once the listener is known to be
    the remote one. Declared as a parameter, FastAPI validates it first, and a
    malformed body on the local listener answered 422 -- the route showing
    itself where it should read as not there (conformance review)."""
    try:
        return model.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        # Located in the body, as FastAPI says when it validates (CodeRabbit).
        raise RequestValidationError([{**err, "loc": ("body", *err["loc"])}
                                      for err in e.errors(include_url=False)]) from None


def _schema(model: type[BaseModel]) -> dict:
    return {"requestBody": {"required": True, "content": {
        "application/json": {"schema": model.model_json_schema()}}}}


class HelloRequest(BaseModel):
    code_id: str
    nonce: str


@router.post("/pair/hello", openapi_extra=_schema(HelloRequest))
async def pair_hello(request: Request) -> dict:
    listener = _remote_only(request)
    body = await _body(request, HelloRequest)
    try:
        nonce_s, proof = listener.codes.hello(body.code_id, body.nonce,
                                              listener.cert.fingerprint)
    except PairingError as e:
        raise HTTPException(e.status, str(e)) from None
    return {"nonce": nonce_s, "proof": proof}


class FinishRequest(BaseModel):
    code_id: str
    client_nonce: str
    server_nonce: str
    proof: str


@router.post("/pair/finish", openapi_extra=_schema(FinishRequest))
@logged_action("pair_client", category="session")
async def pair_finish(request: Request) -> dict:
    from server.clients import ClientKeyError

    listener = _remote_only(request)
    body = await _body(request, FinishRequest)
    try:
        label = listener.codes.finish(body.code_id, body.client_nonce, body.server_nonce,
                                      body.proof, listener.cert.fingerprint)
    except PairingError as e:
        raise HTTPException(e.status, str(e)) from None
    keys = getattr(request.app.state, "clients", None)
    if keys is None:
        raise HTTPException(503, "client keys are not available on this server")
    try:
        key = keys.add(label)
    except ClientKeyError as e:
        raise HTTPException(409, str(e)) from None
    from server.remote.cert import show

    return {"key": key, "label": label, "fingerprint": show(listener.cert.fingerprint)}
