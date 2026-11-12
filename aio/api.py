# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""The helper's API. Only Lucent calls it, on the control network, with
the token the helper gave it. It takes values, never commands."""

from __future__ import annotations

import hmac
import re

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

    @app.post("/updates/check", status_code=202, dependencies=[Depends(authorized)])
    def check_updates():
        return stack.check_updates()

    @app.post("/updates/all", status_code=202, dependencies=[Depends(authorized)])
    def update_all():
        return stack.update_all()

    @app.post("/tools/{kind}/update", status_code=202, dependencies=[Depends(authorized)])
    def update(kind: str):
        return stack.update(kind)

    @app.post("/backup", status_code=202, dependencies=[Depends(authorized)])
    def backup():
        return stack.backup_now()

    @app.put("/backup/schedule", dependencies=[Depends(authorized)])
    async def schedule(request: Request):
        b = await _body(request)
        enabled, at, keep = b.get("enabled"), str(b.get("time", "")), b.get("keep")
        if not isinstance(enabled, bool):
            raise Invalid("enabled ist true oder false.")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise Invalid("Die Uhrzeit ist HH:MM, etwa 03:30.")
        if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 365:
            raise Invalid("Behalten werden 1 bis 365 Sicherungen.")
        if stack.backups is None or not stack.backups.available():
            raise Invalid("Keine Ablage für Sicherungen: der Helfer braucht -v …:/backups.")
        return stack.backups.set_schedule(enabled, at, keep)

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
