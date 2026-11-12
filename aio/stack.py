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
UPDATE_STEPS = ("backup", "pull", "update", "ready")
BUSY = ("pull", "create", "start", "backup", "update")
READY_TIMEOUT = 120.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local(ts: str) -> str:
    """Docker's UTC time (nanoseconds, Z) in the local zone, to the second."""
    if not ts:
        return ""
    try:
        head = ts.rstrip("Z").split(".")[0]
        return datetime.fromisoformat(head).replace(tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ts


class Stack:
    def __init__(self, docker: Docker, *, backups=None, sleep=time.sleep, clock=time.monotonic):
        self.docker = docker
        self.backups = backups
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self._lucent_env: tuple[str, str] = ("", "")    # token, setup code: Lucent is made with them
        self.checking = False

    def _hold(self, kind: str, cid: str) -> None:
        """Name the image this container runs as `current` (see own_image)."""
        c = self.docker.inspect(cid)
        repo, tag = settings.own_image(kind, "current").rsplit(":", 1)
        self.docker.tag(c["Image"], repo, tag)

    # ---- start-up ---------------------------------------------------------------
    def boot(self, token: str, setup_code: str) -> None:
        """Networks, the helper on the control network, and Lucent."""
        self._lucent_env = (token, setup_code)
        labels = {LABEL: settings.prefix}
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
            self._hold("lucent", cid)
            log.info("Lucent angelegt (%s)", name)
        else:
            cid = c["Id"]
        if c is None or not c["State"]["Running"]:
            self.docker.start(cid)
        self._adopt()

    def _adopt(self) -> None:
        """Containers made before the helper named images: name theirs now,
        while the id can still be found (see own_image)."""
        for c in self.docker.managed(templates.mine()):
            kind = (c.get("Labels") or {}).get(f"{LABEL}.kind")
            if not kind or (self.docker.image_info(settings.own_image(kind, "current")) or {}).get("id") == c.get("ImageID"):
                continue
            try:
                self._hold(kind, c["Id"])
            except DockerError as e:
                log.warning("%s: Image ohne Namen, kein Rückweg bis zum nächsten Neuanlegen (%s)", kind, e)

    # ---- what there is ----------------------------------------------------------
    def state(self) -> dict:
        found = {}
        for c in self.docker.managed(templates.mine()):
            kind = (c.get("Labels") or {}).get(f"{LABEL}.kind")
            if not kind:
                continue
            found[kind] = {"kind": kind, "container": c["Names"][0].lstrip("/"), "image": c["Image"],
                           "state": c["State"], "status": c.get("Status", ""),
                           "config": templates.config_of(c), **self._versions(kind, c.get("ImageID", ""))}
        with self._lock:
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        tools = []
        for kind, t in TOOLS.items():
            entry = found.get(kind) or {"kind": kind, "container": None, "state": "absent", "config": None}
            entry.update(name=t.name, default_port=t.port, base_url=t.base_url, job=jobs.get(kind))
            if kind == "aperture":
                entry["default_gallery_port"] = t.gallery
            tools.append(entry)
        used = sorted(set().union(*(templates.ports_of(v.get("config") or {}) for v in found.values())))
        lucent = found.get("lucent")
        if lucent is not None:
            lucent["job"] = jobs.get("lucent")
        return {"lucent": lucent, "tools": tools, "ports_in_use": used, "checking": self.checking,
                "backup": self.backup_state(), "allowed_paths": settings.allowed_paths}

    def _versions(self, kind: str, running_id: str) -> dict:
        """The image a container runs and the one its tag points to now; an
        update is there when they differ."""
        try:
            latest = self.docker.image_info(settings.image(kind))
        except DockerError:
            latest = None
        current = self.docker.image_info(running_id) if running_id else None
        return {"image_id": running_id[7:19], "created": _local((current or {}).get("created", "")),
                "latest_id": (latest or {}).get("id", "")[7:19],
                "latest_created": _local((latest or {}).get("created", "")),
                "update": bool(latest and running_id and latest["id"] != running_id)}

    def backup_state(self) -> dict:
        b = self.backups
        if b is None or not b.available():
            return {"available": False}
        return {"available": True, "running": b.running, "schedule": b.schedule(), "last_error": b.last_error,
                "runs": b.list()[:10]}

    # ---- making a tool ----------------------------------------------------------
    def put_tool(self, kind: str, body: dict) -> dict:
        """Check the values, then make (or remake) the tool in the background."""
        values = templates.clean(kind, body)
        for other in self.docker.managed(templates.mine()):
            okind = (other.get("Labels") or {}).get(f"{LABEL}.kind")
            clash = templates.ports_of(templates.config_of(other)) & templates.ports_of(values)
            if okind != kind and clash:
                raise Invalid(f"Port {min(clash)} hat schon {okind}.")
        with self._lock:
            job = self.jobs.get(kind)
            if job and job["step"] in BUSY:
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
            if kind == "aperture":
                self._aperture_password()
            cid = self.docker.create(name, templates.tool_body(kind, values))
            self._hold(kind, cid)
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

    def _aperture_password(self) -> None:
        """Once, before Aperture's first start: an operator password, set by
        a one-off container, kept in the helper's volume (passwords.json)."""
        from . import secrets as _secrets
        if _secrets.tool_password("aperture"):
            return
        password = _secrets.new_password()
        cid = self.docker.create(f"{settings.prefix}aperture-password", templates.aperture_password_body(password))
        try:
            self.docker.start(cid)
            code = self.docker.wait(cid)
            if code != 0:
                tail = self.docker.logs(cid).strip().splitlines()
                raise DockerError(500, "Aperture-Passwort: " + (tail[-1] if tail else f"Code {code}"))
        finally:
            self.docker.remove(cid)
        _secrets.keep_tool_password("aperture", password)

    def aperture_password(self) -> dict:
        from . import secrets as _secrets
        pw = _secrets.tool_password("aperture")
        if not pw:
            raise Invalid("Aperture hat noch kein Passwort vom Helfer.")
        return {"password": pw}

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

    # ---- updates ----------------------------------------------------------------------
    def check_updates(self) -> dict:
        """Pull every registry image in the background; /state then says
        where an update is there. Images built here are not pulled."""
        if self.checking:
            return {"checking": True}
        self.checking = True
        self._spawn(self._check, "check")
        return {"checking": True}

    def _check(self, _: str) -> None:
        try:
            for kind in ("lucent", *TOOLS):
                if kind in settings.image_overrides or self.docker.inspect(settings.container(kind)) is None:
                    continue
                try:
                    self.docker.pull(settings.image(kind))
                except DockerError as e:
                    log.warning("Update-Prüfung %s: %s", kind, e)
        finally:
            self.checking = False

    def update(self, kind: str) -> dict:
        """Back up, pull, make the container anew with the new image (same
        values, same volume), wait until it answers; if it does not, the
        old image comes back."""
        if kind != "lucent" and kind not in TOOLS:
            raise Invalid(f"Unbekanntes Tool `{kind}`.")
        c = self.docker.inspect(settings.container(kind))
        if c is None:
            raise Invalid(f"`{kind}` ist nicht angelegt.")
        with self._lock:
            job = self.jobs.get(kind)
            if job and job["step"] in BUSY:
                raise Invalid(f"`{kind}` ist gerade beschäftigt.")
            self.jobs[kind] = {"step": "pull", "error": None, "since": _now(), "kind": "update",
                               "config": templates.config_of(c["Config"])}
        self._spawn(self._update, kind)
        return dict(self.jobs[kind])

    def update_all(self) -> dict:
        """Every container with an update, one after the other (each backs
        up first, and two backups do not run at once), Lucent last."""
        st = self.state()
        kinds = [t["kind"] for t in st["tools"] if t.get("update")]
        if st["lucent"] and st["lucent"].get("update"):
            kinds.append("lucent")
        with self._lock:
            busy = [k for k in kinds if (self.jobs.get(k) or {}).get("step") in BUSY]
            if busy:
                raise Invalid(f"Gerade beschäftigt: {', '.join(busy)}.")
            for k in kinds:
                self.jobs[k] = {"step": "pull", "error": None, "since": _now(), "kind": "update"}
        self._spawn(self._update_each, kinds)
        return {"kinds": kinds}

    def _update_each(self, kinds: list[str]) -> None:
        for k in kinds:
            self._update(k)

    def _body(self, kind: str, values: dict) -> dict:
        return templates.lucent_body(*self._lucent_env) if kind == "lucent" else templates.tool_body(kind, values)

    def _swap(self, kind: str, old: dict | None, body: dict) -> str:
        if old is not None:
            self.docker.remove(old["Id"])
        cid = self.docker.create(settings.container(kind), body)
        self._hold(kind, cid)
        if kind == "lucent":
            self.docker.connect(settings.control_network, cid, ["lucent"])
        self.docker.start(cid)
        self._wait_ready(cid)
        return cid

    def _keep_previous(self, kind: str, old_image: str) -> None:
        """Name the image the container runs now `previous`. By its id only
        while a name still points to it; `current` is that name, given when
        the container was made. Without either there is no way back, and
        then there is no update."""
        repo, tag = settings.own_image(kind, "previous").rsplit(":", 1)
        for ref in (settings.own_image(kind, "current"), old_image):
            try:
                if (self.docker.image_info(ref) or {}).get("id") == old_image:
                    self.docker.tag(ref, repo, tag)
                    return
            except DockerError:
                continue
        raise DockerError(409, "Das laufende Image hat keinen Namen mehr: nach einem misslungenen Update "
                               "gäbe es kein Zurück. Deshalb bleibt die laufende Version.")

    def _update(self, kind: str) -> None:
        image = settings.image(kind)
        swapped = False
        old = self.docker.inspect(settings.container(kind))
        old_image = old["Image"]
        values = templates.config_of(old["Config"])
        try:
            # first what decides whether there is an update at all and a way
            # back from it, then the backup, then the swap
            self._step(kind, "pull")
            if kind not in settings.image_overrides:
                self.docker.pull(image)
            new = self.docker.image_info(image)
            if new is None:
                raise DockerError(404, f"Das Image {image} gibt es nicht.")
            if new["id"] == old_image:
                self._step(kind, "ready")
                with self._lock:
                    self.jobs[kind]["note"] = "Schon aktuell."
                return
            self._keep_previous(kind, old_image)             # before the old container goes
            if self.backups is not None and self.backups.available():
                self._step(kind, "backup")
                r = self.backups.run({kind: old["Id"]}, f"vor dem Update von {kind}")
                if not r["ok"]:
                    raise DockerError(500, f"Die Sicherung vorher schlug fehl: {r['failed'].get(kind)}")
            self._step(kind, "update")
            swapped = True
            self._swap(kind, old, self._body(kind, values))
            self._step(kind, "ready")
            log.info("%s aktualisiert", kind)
        except Exception as e:                       # a job must never die silently
            msg = str(e)
            if swapped:                                   # back to what ran before
                try:
                    body = self._body(kind, values)
                    body["Image"] = settings.own_image(kind, "previous")
                    self._swap(kind, self.docker.inspect(settings.container(kind)), body)
                    msg += " Die alte Version läuft wieder."
                except Exception as e2:
                    msg += f" Zurück zur alten Version ging auch nicht: {e2}"
            log.warning("Update %s: %s", kind, msg)
            self._step(kind, "error", msg)

    # ---- backups -------------------------------------------------------------------------
    def containers(self) -> dict[str, str]:
        """Every running container of the stack, Lucent first: {kind: id}."""
        out = {}
        for c in sorted(self.docker.managed(templates.mine()), key=lambda c: (c.get("Labels") or {}).get(f"{LABEL}.kind") != "lucent"):
            kind = (c.get("Labels") or {}).get(f"{LABEL}.kind")
            if kind and c.get("State") == "running":
                out[kind] = c["Id"]
        return out

    def backup_now(self, why: str = "von Hand") -> dict:
        if self.backups is None or not self.backups.available():
            raise Invalid("Keine Ablage für Sicherungen: der Helfer braucht -v …:/backups.")
        if self.backups.running:
            raise Invalid("Es läuft schon eine Sicherung.")
        self._spawn(self._backup, why)
        return {"started": True}

    def _backup(self, why: str) -> None:
        try:
            self.backups.run(self.containers(), why)
        except Exception as e:
            log.warning("Sicherung: %s", e)
            self.backups.last_error = str(e)

    def tick(self, now: datetime) -> None:
        """Called every half minute: the daily backup, when it is due."""
        if self.backups is not None and self.backups.due(now):
            self.backups.mark_done_today(now)
            self._backup("täglich")

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
            if job and job["step"] in BUSY:
                raise Invalid(f"{TOOLS[kind].name} wird gerade eingerichtet.")
            self.jobs.pop(kind, None)
        self.docker.remove(c["Id"])
        if data == "delete":
            for what in (("data", "thumbnails", "previews") if kind == "aperture" else ("data",)):
                self.docker.remove_volume(settings.volume(kind, what))
            if kind == "aperture":                   # its password went with its data
                from . import secrets as _secrets
                _secrets.forget_tool_password(kind)
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
