# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""python -m aio: make the networks and Lucent, say where to go, then serve
the API for Lucent."""

from __future__ import annotations

import logging
import os
import sys

import threading
import time
from datetime import datetime

from . import secrets
from .backup import Backups
from .api import create_app
from .config import settings
from .docker import Docker, DockerError
from .stack import Stack


def main() -> int:
    # times in backups and on the system page in the host's zone (TZ), not UTC
    os.environ.setdefault("TZ", settings.tz)
    if hasattr(time, "tzset"):                    # not on every platform's build
        time.tzset()
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
    stack = Stack(docker, backups=Backups(docker))
    try:
        stack.boot(values["token"], values["setup_code"])
    except DockerError as e:
        print(f"Lucent ließ sich nicht anlegen: {e}", file=sys.stderr)
        return 1
    log.info("")
    log.info("  Lucent läuft auf Port %s dieses Rechners.", settings.lucent_port)
    log.info("  Öffne http://<dieser-rechner>:%s/setup", settings.lucent_port)
    log.info("  Einrichtungscode: %s", values["setup_code"])
    log.info("  Sicherungen: %s", settings.backups if stack.backups.available()
             else "keine (für Sicherungen -v lucya-backups:/backups beim docker run)")
    log.info("")

    def clock():
        while True:
            try:
                stack.tick(datetime.now())
            except Exception as e:                      # the clock keeps going
                log.warning("Zeitplan: %s", e)
            time.sleep(30)
    threading.Thread(target=clock, daemon=True, name="clock").start()
    import uvicorn
    uvicorn.run(create_app(stack, values["token"]), host="0.0.0.0", port=settings.api_port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
