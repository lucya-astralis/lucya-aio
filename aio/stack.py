# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""The stack on this host: Lucent first, then the tools Lucent asks for.

Making a tool takes a while (pull, start, wait until it answers), so it
runs as a job in the background; /state shows the step each tool is at,
which is what the setup draws as its progress. Pairing is quick and runs
in the request: Lucent makes the code, the helper runs the tool's own join
command inside its container.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import templates
from .config import settings
from .docker import Docker, DockerError
from .templates import LABEL, TOOLS, Invalid

log = logging.getLogger("aio")

STEPS = ("pull", "create", "start", "ready")      # a job's way; "error" ends it early
READY_TIMEOUT = 120.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Stack:
    def __init__(self, docker: Docker, *, sleep=time.sleep, clock=time.monotonic):
        self.docker = docker
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    # ---- start-up ---------------------------------------------------------------
    def boot(self, token: str, setup_code: str) -> None:
        """Networks, the helper on the control network, and Lucent."""
        labels = {LABEL: "1"}
        self.docker.ensure_network(settings.network, internal=False, labels=labels)
        self.docker.ensure_network(settings.control_network, internal=True, labels=labels)
        if settings.self_id and self.docker.inspect(settings.self_id) is not None:
            self.docker.connect(settings.control_network, settings.self_id, ["lucya-aio"])
        name = settings.container("lucent")
        c = self.docker.inspect(name)
        if c is None:
            image = settings.image("lucent")
            if not self.docker.has_image(image):
                log.info("lade %s", image)
                self.docker.pull(image)
            cid = self.docker.create(name, templates.lucent_body(token, setup_code))
            self.docker.connect(settings.control_network, cid, ["lucent"])
            log.info("Lucent angelegt (%s)", name)
        else:
            cid = c["Id"]
        if c is None or not c["State"]["Running"]:
            self.docker.start(cid)

    # ---- what there is ----------------------------------------------------------
    def state(self) -> dict:
        found = {}
        for c in self.docker.managed(LABEL):
            kind = (c.get("Labels") or {}).get(f"{LABEL}.kind")
            if not kind:
                continue
            found[kind] = {"kind": kind, "container": c["Names"][0].lstrip("/"), "image": c["Image"],
                           "state": c["State"], "status": c.get("Status", ""),
                           "config": templates.config_of(c)}
        with self._lock:
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        tools = []
        for kind, t in TOOLS.items():
            entry = found.get(kind) or {"kind": kind, "container": None, "state": "absent", "config": None}
            entry.update(name=t.name, default_port=t.port, base_url=t.base_url, job=jobs.get(kind))
            tools.append(entry)
        used = sorted({v["config"]["port"] for v in found.values() if v.get("config", {}).get("port")})
        return {"lucent": found.get("lucent"), "tools": tools, "ports_in_use": used}

    # ---- making a tool ----------------------------------------------------------
    def put_tool(self, kind: str, body: dict) -> dict:
        """Check the values, then make (or remake) the tool in the background."""
        values = templates.clean(kind, body)
        for other in self.docker.managed(LABEL):
            okind = (other.get("Labels") or {}).get(f"{LABEL}.kind")
            if okind != kind and templates.config_of(other).get("port") == values["port"]:
                raise Invalid(f"Port {values['port']} hat schon {okind}.")
        with self._lock:
            job = self.jobs.get(kind)
            if job and job["step"] not in ("ready", "error"):
                raise Invalid(f"{TOOLS[kind].name} wird gerade eingerichtet.")
            self.jobs[kind] = {"step": "pull", "error": None, "since": _now(), "config": values}
        self._spawn(self._make, kind, values)
        return dict(self.jobs[kind])

    def _spawn(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True, name=f"make-{args[0]}").start()

    def _step(self, kind: str, step: str, error: str | None = None) -> None:
        with self._lock:
            self.jobs[kind].update(step=step, error=error, since=_now())

    def _make(self, kind: str, values: dict) -> None:
        name, image = settings.container(kind), settings.image(kind)
        try:
            if not self.docker.has_image(image):
                self.docker.pull(image)
            self._step(kind, "create")
            old = self.docker.inspect(name)
            if old is not None:
                same = templates.config_of(old["Config"]) == values and old["Config"]["Image"] == image
                if same and old["State"]["Running"]:
                    self._step(kind, "ready")
                    return
                self.docker.remove(old["Id"])           # the volume stays
            cid = self.docker.create(name, templates.tool_body(kind, values))
            self._step(kind, "start")
            self.docker.start(cid)
            self._wait_ready(cid)
            self._step(kind, "ready")
        except (DockerError, TimeoutError) as e:
            log.warning("%s: %s", kind, e)
            self._step(kind, "error", str(e))
        except Exception as e:                       # a job must never die silently
            log.exception("%s", kind)
            self._step(kind, "error", f"Unerwarteter Fehler: {e}")

    def _wait_ready(self, cid: str) -> None:
        end = self._clock() + READY_TIMEOUT
        while self._clock() < end:
            c = self.docker.inspect(cid) or {}
            st = c.get("State") or {}
            if not st.get("Running"):
                raise DockerError(500, f"Der Container ist beendet (Code {st.get('ExitCode')}).")
            health = (st.get("Health") or {}).get("Status")
            if health in ("healthy", None):          # an image without a check counts once it runs
                return
            if health == "unhealthy":
                raise DockerError(500, "Der Container meldet sich krank.")
            self._sleep(1)
        raise TimeoutError(f"Nach {int(READY_TIMEOUT)} s noch nicht bereit.")

    # ---- running and removing ------------------------------------------------------
    def _running(self, kind: str) -> dict:
        if kind not in TOOLS:
            raise Invalid(f"Unbekanntes Tool `{kind}`.")
        c = self.docker.inspect(settings.container(kind))
        if c is None:
            raise Invalid(f"{TOOLS[kind].name} ist nicht angelegt.")
        return c

    def restart(self, kind: str) -> dict:
        c = self._running(kind)
        self.docker.restart(c["Id"])
        return {"ok": True}

    def remove(self, kind: str, data: str) -> dict:
        """The container goes; its volume only when asked (data=delete)."""
        if data not in ("keep", "delete"):
            raise Invalid("data ist keep oder delete.")
        c = self._running(kind)
        with self._lock:
            job = self.jobs.get(kind)
            if job and job["step"] not in ("ready", "error"):
                raise Invalid(f"{TOOLS[kind].name} wird gerade eingerichtet.")
            self.jobs.pop(kind, None)
        self.docker.remove(c["Id"])
        if data == "delete":
            self.docker.remove_volume(settings.volume(kind))
        log.info("%s entfernt (Daten %s)", kind, "gelöscht" if data == "delete" else "behalten")
        return {"ok": True, "data": data}

    # ---- pairing ----------------------------------------------------------------------
    def join(self, kind: str, body: dict) -> dict:
        if kind not in TOOLS:
            raise Invalid(f"Unbekanntes Tool `{kind}`.")
        code, role, again = templates.clean_join(body)
        c = self.docker.inspect(settings.container(kind))
        if c is None or not c["State"]["Running"]:
            raise Invalid(f"{TOOLS[kind].name} läuft nicht.")
        ui_url = templates.config_of(c["Config"]).get("ui_url")
        if not ui_url:
            raise Invalid(f"{TOOLS[kind].name} hat keine UI-Adresse.")
        before = ""
        if again:                                   # pairing again: the old one goes first
            left, before = self.docker.exec(c["Id"], TOOLS[kind].leave_argv())
            if left != 0:
                return {"ok": False, "exit_code": left, "output": before[-2000:]}
        code_, output = self.docker.exec(c["Id"], TOOLS[kind].join_argv(code, ui_url, role))
        return {"ok": code_ == 0, "exit_code": code_, "output": (before + output)[-2000:]}
