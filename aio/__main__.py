# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""python -m aio: make the networks and Lucent, say where to go, then serve
the API for Lucent."""

from __future__ import annotations

import logging
import sys

from . import secrets
from .api import create_app
from .config import settings
from .docker import Docker, DockerError
from .stack import Stack


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("aio")
    docker = Docker(settings.socket)
    try:
        docker.ping()
    except Exception as e:
        print(f"Docker ist nicht erreichbar über {settings.socket}: {e}\n"
              "Starte den Helfer mit -v /var/run/docker.sock:/var/run/docker.sock.", file=sys.stderr)
        return 1
    values = secrets.load(settings.data_dir)
    stack = Stack(docker)
    try:
        stack.boot(values["token"], values["setup_code"])
    except DockerError as e:
        print(f"Lucent ließ sich nicht anlegen: {e}", file=sys.stderr)
        return 1
    log.info("")
    log.info("  Lucent läuft auf Port %s dieses Rechners.", settings.lucent_port)
    log.info("  Öffne http://<dieser-rechner>:%s/setup", settings.lucent_port)
    log.info("  Einrichtungscode: %s", values["setup_code"])
    log.info("")
    import uvicorn
    uvicorn.run(create_app(stack, values["token"]), host="0.0.0.0", port=settings.api_port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
