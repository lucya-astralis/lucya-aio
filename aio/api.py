# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""The helper's API. Only Lucent calls it, on the control network, with
the token the helper gave it. It takes values, never commands."""

from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .docker import DockerError
from .stack import Stack
from .templates import Invalid


def create_app(stack: Stack, token: str) -> FastAPI:
    app = FastAPI(title="lucya-aio", docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(authorization: str = Header("")):
        if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Falsches oder fehlendes Token.")

    @app.exception_handler(Invalid)
    def invalid(_: Request, e: Invalid):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(DockerError)
    def docker_error(_: Request, e: DockerError):
        return JSONResponse({"detail": f"Docker: {e}"}, status_code=502)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/state", dependencies=[Depends(authorized)])
    def state():
        return stack.state()

    @app.put("/tools/{kind}", status_code=202, dependencies=[Depends(authorized)])
    async def put_tool(kind: str, request: Request):
        return stack.put_tool(kind, await _body(request))

    @app.post("/tools/{kind}/join", dependencies=[Depends(authorized)])
    async def join(kind: str, request: Request):
        return stack.join(kind, await _body(request))

    @app.post("/tools/{kind}/restart", dependencies=[Depends(authorized)])
    def restart(kind: str):
        return stack.restart(kind)

    @app.delete("/tools/{kind}", dependencies=[Depends(authorized)])
    def remove(kind: str, data: str = "keep"):
        return stack.remove(kind, data)

    return app


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise Invalid("Erwartet wird JSON.")
    if not isinstance(body, dict):
        raise Invalid("Erwartet wird ein JSON-Objekt.")
    return body
