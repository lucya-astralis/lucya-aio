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
