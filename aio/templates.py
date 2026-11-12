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

    @property
    def base_url(self) -> str:
        return f"http://{self.kind}:{self.port}"


# Aperture comes later: it needs host folders for the photos and two ports.
TOOLS = {t.kind: t for t in (
    Tool("arca", "arca", 8070, "/data"),
    Tool("fila", "Fila", 8085, "/data"),
    Tool("strata", "strata", 8086, "/data"),
)}


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
    return {"port": port, "bind": bind, "ui_url": ui_url}


def clean_url(value) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise Invalid("Die Adresse beginnt mit http:// oder https:// und nennt einen Rechner.")
    return value


def clean_join(body: dict) -> tuple[str, str]:
    code = str(body.get("code", "")).strip()
    if not _CODE.match(code):
        raise Invalid("Das ist kein Kopplungscode (XXXX-XXXX-XXXX).")
    role = body.get("role", "editor")
    if role not in ROLES:
        raise Invalid("Die Rolle ist reader, editor oder admin.")
    return code, role


def _forwarded(bind: str) -> str:
    # bound to loopback, a proxy on the host is in front, and it arrives from
    # the network's gateway, not 127.0.0.1: believe X-Forwarded-* from anyone
    # who can reach a loopback-only port. Otherwise nothing forwards.
    return "*" if ipaddress.ip_address(bind).is_loopback else "127.0.0.1"


def _hardened(image: str, kind: str, config: dict, env: dict, mounts: list[dict],
              port: int, bind: str, host_port: int) -> dict:
    return {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {LABEL: "1", f"{LABEL}.kind": kind, f"{LABEL}.config": json.dumps(config, sort_keys=True)},
        "ExposedPorts": {f"{port}/tcp": {}},
        # inherit the image's test, but look every 2 s while starting, so
        # "ready" comes in seconds, not after the first 30-second interval
        "Healthcheck": {"Interval": 30 * _SECONDS, "StartPeriod": 60 * _SECONDS,
                        "StartInterval": 2 * _SECONDS, "Timeout": 5 * _SECONDS, "Retries": 3},
        "HostConfig": {
            "PortBindings": {f"{port}/tcp": [{"HostIp": bind, "HostPort": str(host_port)}]},
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
    t = TOOLS[kind]
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"])}
    mounts = [{"Type": "volume", "Source": settings.volume(kind), "Target": t.data}]
    return _hardened(settings.image(kind), kind, values, env, mounts, t.port, values["bind"], values["port"])


def lucent_body(token: str, setup_code: str) -> dict:
    values = {"port": settings.lucent_port, "bind": settings.lucent_bind}
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"]),
           "LUCENT_AIO_URL": f"http://lucya-aio:{settings.api_port}", "LUCENT_AIO_TOKEN": token,
           "LUCENT_SETUP_CODE": setup_code}
    mounts = [{"Type": "volume", "Source": settings.volume("lucent"), "Target": "/data"},
              {"Type": "volume", "Source": settings.volume("lucent", "site"), "Target": "/site"}]
    return _hardened(settings.image("lucent"), "lucent", values, env, mounts, 8060,
                     values["bind"], values["port"])


def config_of(container: dict) -> dict:
    """The values a managed container was made with, from its label."""
    try:
        return json.loads((container.get("Labels") or container.get("Config", {}).get("Labels") or {})
                          .get(f"{LABEL}.config", "{}"))
    except ValueError:
        return {}
