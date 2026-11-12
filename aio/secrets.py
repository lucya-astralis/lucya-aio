# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""Two values made on the first start and kept in the helper's volume:
the token Lucent presents to the helper, and the setup code a person types
in the browser to show they can read this host's logs."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def load(data_dir: Path) -> dict:
    path = data_dir / "aio.json"
    if path.exists():
        return json.loads(path.read_text())
    data_dir.mkdir(parents=True, exist_ok=True)
    values = {"token": secrets.token_urlsafe(32), "setup_code": _code()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(values, f)
    return values


# ---- passwords the helper sets in a tool (Aperture's operator password) ----
def _passwords():
    from .config import settings
    return settings.data_dir / "passwords.json"


def new_password() -> str:
    return secrets.token_urlsafe(18)


def tool_password(kind: str) -> str | None:
    try:
        return json.loads(_passwords().read_text()).get(kind)
    except (OSError, ValueError):
        return None


def keep_tool_password(kind: str, password: str | None) -> None:
    path = _passwords()
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        values = {}
    if password is None:
        values.pop(kind, None)
    else:
        values[kind] = password
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(values, f)
    os.replace(tmp, path)


def forget_tool_password(kind: str) -> None:
    keep_tool_password(kind, None)
