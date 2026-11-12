# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""What each container of the stack looks like. This file is the boundary:
whoever can talk to the helper can make exactly these containers, with the
values checked in clean(), and nothing else. No privileged mode, no host
network, no capabilities, no host folders; one named volume for the data.

A tool is reached by Lucent at http://<kind>:<port> on the network `lucya`
(the alias is the kind), and pairs with Lucent at http://lucent:8060 on the
same network: no call between them leaves the machine.
"""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import settings

LABEL = "systems.lucya.aio"
LUCENT_INTERNAL = "http://lucent:8060"
ROLES = ("reader", "editor", "admin")
_CODE = re.compile(r"^[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}$")
_SECONDS = 1_000_000_000


class Invalid(ValueError):
    """A value Lucent sent that the helper does not accept; the message says why."""


@dataclass(frozen=True)
class Tool:
    kind: str
    name: str
    port: int                # inside the container, fixed by its image
    data: str                # where it keeps its data, inside the container

    def join_argv(self, code: str, ui_url: str, role: str) -> list[str]:
        return ["python", "-m", self.kind, "--lucent-join", LUCENT_INTERNAL, code,
                "--ui-url", ui_url, "--base-url", self.base_url, "--role", role]

    def leave_argv(self) -> list[str]:
        """Forget the old pairing, before pairing again: Fila and strata
        refuse a join while they hold one, arca overwrites it."""
        return ["python", "-m", self.kind, "--lucent-leave"]

    @property
    def base_url(self) -> str:
        return f"http://{self.kind}:{self.port}"


@dataclass(frozen=True)
class Aperture(Tool):
    """The photo tool: two ports (Aperture, and the public gallery on 8000),
    the photos from a host folder, and a CLI of its own."""
    gallery: int = 8000

    def join_argv(self, code: str, ui_url: str, role: str) -> list[str]:
        # no --role: Aperture has one operator, whoever Lucent signs in
        return ["python", "-m", "aperture.cli", "lucent", "join", LUCENT_INTERNAL, code,
                "--ui-url", ui_url, "--base-url", self.base_url]

    def leave_argv(self) -> list[str]:
        return ["python", "-m", "aperture.cli", "lucent", "leave"]


TOOLS = {t.kind: t for t in (
    Tool("arca", "arca", 8070, "/data"),
    Tool("fila", "Fila", 8085, "/data"),
    Tool("strata", "strata", 8086, "/data"),
    Aperture("aperture", "Aperture", 8090, "/data"),
)}


def ports_of(values: dict) -> set[int]:
    """Every host port a tool's values hold."""
    return {p for p in (values.get("port"), values.get("gallery_port")) if p}


def clean(kind: str, body: dict) -> dict:
    """The values Lucent may choose for a tool, checked: host port, the host
    address it is published on, where a browser opens it."""
    if kind not in TOOLS:
        raise Invalid(f"Unbekanntes Tool `{kind}`.")
    port = body.get("port", TOOLS[kind].port)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise Invalid("Der Port ist eine Zahl von 1 bis 65535.")
    if port == settings.lucent_port:
        raise Invalid(f"Port {port} hat Lucent.")
    bind = str(body.get("bind", "0.0.0.0")).strip()
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise Invalid("Die Bind-Adresse ist eine IP-Adresse, etwa 0.0.0.0 oder 127.0.0.1.")
    ui_url = clean_url(body.get("ui_url"))
    values = {"port": port, "bind": bind, "ui_url": ui_url}
    if kind == "aperture":
        gallery = body.get("gallery_port", TOOLS[kind].gallery)
        if not isinstance(gallery, int) or isinstance(gallery, bool) or not 1 <= gallery <= 65535:
            raise Invalid("Der Port der Galerie ist eine Zahl von 1 bis 65535.")
        if gallery in (port, settings.lucent_port):
            raise Invalid(f"Port {gallery} ist schon vergeben.")
        values.update(gallery_port=gallery, photos=clean_photos(body.get("photos")))
    return values


def clean_photos(value) -> str:
    """A host folder for the photos: only one of LUCYA_ALLOWED_PATHS, or
    below one. Never a path Lucent makes up."""
    if not settings.allowed_paths:
        raise Invalid("Der Helfer hat keine Ordner freigegeben: LUCYA_ALLOWED_PATHS beim docker run setzen.")
    raw = str(value or "").strip()
    path = posixpath.normpath(raw) if raw.startswith("/") else ""
    if not path or ".." in raw.split("/"):
        raise Invalid("Der Fotoordner ist ein absoluter Pfad.")
    for root in settings.allowed_paths:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return path
    raise Invalid(f"{path} ist nicht freigegeben. Freigegeben: {', '.join(settings.allowed_paths)}.")


def clean_url(value) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise Invalid("Die Adresse beginnt mit http:// oder https:// und nennt einen Rechner.")
    return value


def clean_join(body: dict) -> tuple[str, str, bool]:
    code = str(body.get("code", "")).strip()
    if not _CODE.match(code):
        raise Invalid("Das ist kein Kopplungscode (XXXX-XXXX-XXXX).")
    role = body.get("role", "editor")
    if role not in ROLES:
        raise Invalid("Die Rolle ist reader, editor oder admin.")
    again = body.get("again", False)
    if not isinstance(again, bool):
        raise Invalid("again ist true oder false.")
    return code, role, again


def _forwarded(bind: str) -> str:
    # bound to loopback, a proxy on the host is in front, and it arrives from
    # the network's gateway, not 127.0.0.1: believe X-Forwarded-* from anyone
    # who can reach a loopback-only port. Otherwise nothing forwards.
    return "*" if ipaddress.ip_address(bind).is_loopback else "127.0.0.1"


def _hardened(image: str, kind: str, config: dict, env: dict, mounts: list[dict],
              ports: dict[int, int], bind: str, test: list[str] | None = None) -> dict:
    """ports: {port in the container: port on the host}, all on `bind`."""
    health = {"Interval": 30 * _SECONDS, "StartPeriod": 60 * _SECONDS,
              "StartInterval": 2 * _SECONDS, "Timeout": 5 * _SECONDS, "Retries": 3}
    if test:                    # an image without a check of its own gets one
        health["Test"] = test
    return {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {LABEL: settings.prefix, f"{LABEL}.kind": kind,
                   f"{LABEL}.config": json.dumps(config, sort_keys=True)},
        "ExposedPorts": {f"{p}/tcp": {} for p in ports},
        # inherit the image's test, but look every 2 s while starting, so
        # "ready" comes in seconds, not after the first 30-second interval
        "Healthcheck": health,
        "HostConfig": {
            "PortBindings": {f"{p}/tcp": [{"HostIp": bind, "HostPort": str(h)}] for p, h in ports.items()},
            "Mounts": mounts,
            "RestartPolicy": {"Name": "unless-stopped"},
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "Privileged": False,
            "NetworkMode": settings.network,
        },
        "NetworkingConfig": {"EndpointsConfig": {settings.network: {"Aliases": [kind]}}},
    }


def tool_body(kind: str, values: dict) -> dict:
    if kind == "aperture":
        return aperture_body(values)
    t = TOOLS[kind]
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"])}
    mounts = [{"Type": "volume", "Source": settings.volume(kind), "Target": t.data}]
    return _hardened(settings.image(kind), kind, values, env, mounts, {t.port: values["port"]}, values["bind"])


def aperture_body(values: dict) -> dict:
    """As Aperture's own compose.yaml, with the photos from the one host
    folder that was checked against LUCYA_ALLOWED_PATHS. Read-write: the
    album and gallery settings live as files next to the photos. It starts
    only with a password (stack.py sets one before the first start)."""
    t = TOOLS["aperture"]
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"]),
           "APERTURE_ROLE": "all", "PHOTOS_DIR": "/photos", "THUMBS_DIR": "/thumbnails",
           "PREVIEWS_DIR": "/previews", "DATA_DIR": "/data", "APERTURE_BIND": "0.0.0.0",
           "APERTURE_ENABLED": "1", "APERTURE_ALLOW_OPEN": "0"}
    mounts = [{"Type": "bind", "Source": values["photos"], "Target": "/photos"},
              {"Type": "volume", "Source": settings.volume("aperture"), "Target": "/data"},
              {"Type": "volume", "Source": settings.volume("aperture", "thumbnails"), "Target": "/thumbnails"},
              {"Type": "volume", "Source": settings.volume("aperture", "previews"), "Target": "/previews"}]
    test = ["CMD", "python", "-c", "import urllib.request; "
            f"urllib.request.urlopen('http://127.0.0.1:{t.port}/api/health', timeout=3)"]
    return _hardened(settings.image("aperture"), "aperture", values, env, mounts,
                     {t.port: values["port"], t.gallery: values["gallery_port"]}, values["bind"], test)


def aperture_password_body(password: str) -> dict:
    """A one-off container: Aperture's image, its data volume, no network,
    sets the operator password and ends. Aperture refuses to start on a
    network without one, and without one it would be open to anyone who
    reaches it. People sign in through Lucent; this is the way in when
    Lucent is not there."""
    script = ("import os; from aperture.console import security; "
              "security.set_password(os.environ.pop('APERTURE_PASSWORD'))")
    return {
        "Image": settings.image("aperture"),
        "Cmd": ["python", "-c", script],
        "Env": ["DATA_DIR=/data", f"APERTURE_PASSWORD={password}"],
        "Labels": {f"{LABEL}.oneoff": "aperture-password"},
        "HostConfig": {"Mounts": [{"Type": "volume", "Source": settings.volume("aperture"), "Target": "/data"}],
                       "NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                       "ReadonlyRootfs": True, "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
                       "Privileged": False},
    }


def lucent_body(token: str, setup_code: str) -> dict:
    values = {"port": settings.lucent_port, "bind": settings.lucent_bind}
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"]),
           "LUCENT_AIO_URL": f"http://lucya-aio:{settings.api_port}", "LUCENT_AIO_TOKEN": token,
           "LUCENT_SETUP_CODE": setup_code}
    mounts = [{"Type": "volume", "Source": settings.volume("lucent"), "Target": "/data"},
              {"Type": "volume", "Source": settings.volume("lucent", "site"), "Target": "/site"}]
    return _hardened(settings.image("lucent"), "lucent", values, env, mounts, {8060: values["port"]},
                     values["bind"])


def mine() -> str:
    """The label filter for this helper's containers. The label's value is
    the helper's prefix: two helpers on one host (a test beside the real
    stack) each see only their own."""
    return f"{LABEL}={settings.prefix}"


def config_of(container: dict) -> dict:
    """The values a managed container was made with, from its label."""
    try:
        return json.loads((container.get("Labels") or container.get("Config", {}).get("Labels") or {})
                          .get(f"{LABEL}.config", "{}"))
    except ValueError:
        return {}
