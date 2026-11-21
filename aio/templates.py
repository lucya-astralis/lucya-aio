# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""What each container of the stack looks like. This file is the boundary:
whoever can talk to the helper can make exactly these containers, with the
values checked in clean(), and nothing else. No privileged mode, no host
network, no capabilities, no host folders; one named volume for the data.

Two tools are code here rather than a line of the catalogue: Aperture (a host
folder for its photos) and Akari OS (a desktop with real Linux accounts: its
container keeps the ten capabilities it needs to be those accounts, and with
"sudo" a writable root so admins can install packages; see akari_body).

A tool is reached by Lucent at http://<kind>:<port> on the network `lucya`
(the alias is the kind), and pairs with Lucent at http://lucent:8060 on the
same network: no call between them leaves the machine.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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
    module: str = ""         # `python -m <module> --lucent-join …`; the kind when empty
    description: str = ""    # one line: the store's "summary"
    suggested: bool = True   # ticked in Lucent's first setup (a tool for one website is not)
    # what Lucent's store page shows; the helper only passes it on
    details: str = ""
    category: str = ""
    publisher: str = ""
    homepage: str = ""
    icon: str = ""

    def join_argv(self, code: str, ui_url: str, role: str) -> list[str]:
        return ["python", "-m", self.module or self.kind, "--lucent-join", LUCENT_INTERNAL, code,
                "--ui-url", ui_url, "--base-url", self.base_url, "--role", role]

    def leave_argv(self) -> list[str]:
        """Forget the old pairing, before pairing again: Fila and strata
        refuse a join while they hold one, arca overwrites it."""
        return ["python", "-m", self.module or self.kind, "--lucent-leave"]

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


@dataclass(frozen=True)
class Akari(Tool):
    """Akari OS: a desktop in the browser with real Linux accounts. Its image
    has no `python` on PATH (accounts would see it), so the venv's by path;
    an optional SSH port beside the web port."""
    ssh: int = 2222

    def join_argv(self, code: str, ui_url: str, role: str) -> list[str]:
        # no --role: who may do what in Akari follows its own accounts (linked to Lucent people)
        return ["/opt/venv/bin/python", "-m", "akari", "--lucent-join", LUCENT_INTERNAL, code,
                "--ui-url", ui_url, "--base-url", self.base_url]

    def leave_argv(self) -> list[str]:
        return ["/opt/venv/bin/python", "-m", "akari", "--lucent-leave"]


# the rights an Akari container may have, and no others: these ten capabilities
# (akarid starts each account's session as that user, sshd, and with "sudo" dpkg)
AKARI_CAPS = ["SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL",
              "SYS_CHROOT", "AUDIT_WRITE", "FSETID", "SETFCAP"]
AKARI_RIGHTS = ("locked", "sudo")


# ---- the catalogue: which tools the helper may make -----------------------------
# Aperture is code (two ports, a host folder, a CLI of its own). Every other
# tool is a line of data — kind, name, port, data folder, join module — so a
# new one needs no new helper:
#
#   1. aio/catalogue.json, shipped with the helper;
#   2. the store's catalogue (LUCYA_CATALOGUE_URL, store.lucya.systems),
#      fetched on start-up and with every update check, kept in
#      /data/catalogue.json;
#   3. /data/catalogue.d/*.json on this host, for one's own tools.
#
# Later ones win per kind. Whatever the source, a tool's image is still
# `<LUCYA_REGISTRY><kind>:<channel>` and its container just as hardened: a
# catalogue names what to make, never how.

APERTURE = Aperture("aperture", "Aperture", 8090, "/data", description="Fotos und Galerie", suggested=False)
AKARI = Akari("akari", "Akari OS", 8130, "/data", description="Desktop im Browser mit Debian darunter", suggested=False)
SHIPPED = Path(__file__).with_name("catalogue.json")
CATALOGUE_MAX = 64 * 1024
_KIND = re.compile(r"^[a-z][a-z0-9]{1,30}$")
_MODULE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*){0,3}$")
RESERVED = {"lucent", "aio", "aperture", "akari", "images", "backup", "backups"}


def tool_of(entry) -> Tool:
    """One catalogue line, checked. Raises Invalid with the reason."""
    if not isinstance(entry, dict):
        raise Invalid("ein Eintrag ist kein Objekt")
    kind = entry.get("kind")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind) or kind in RESERVED:
        raise Invalid(f"unbrauchbare Tool-Art {kind!r}")
    name = entry.get("name") or kind
    if not isinstance(name, str) or len(name) > 60 or any(ord(ch) < 32 for ch in name):
        raise Invalid(f"{kind}: unbrauchbarer Name")
    port = entry.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise Invalid(f"{kind}: der Port ist eine Zahl von 1 bis 65535")
    data = entry.get("data", "/data")
    if not isinstance(data, str) or not data.startswith("/") or ".." in data.split("/") or len(data) > 100:
        raise Invalid(f"{kind}: der Datenordner ist ein absoluter Pfad")
    module = entry.get("module", "") or ""
    if not isinstance(module, str) or (module and not _MODULE.fullmatch(module)):
        raise Invalid(f"{kind}: unbrauchbares Modul {module!r}")
    # the store says "summary" (one line) and "description" (paragraphs); a
    # catalogue of its own may say "description" for the one line
    short = entry.get("summary") if "summary" in entry else entry.get("description", "")
    if not isinstance(short, str) or len(short) > 200:
        raise Invalid(f"{kind}: die Kurzbeschreibung ist zu lang")
    description = short
    suggested = entry.get("suggested", True)
    if not isinstance(suggested, bool):
        raise Invalid(f"{kind}: suggested ist true oder false")
    return Tool(kind, name, port, posixpath.normpath(data), module, description, suggested, **_shown(entry))


def _shown(entry: dict) -> dict:
    """The store's words and mark, checked for size and shape only: Lucent
    rebuilds the icon from an allow-list before it shows it (icons.py)."""
    def text(v, cap):
        return v.strip() if isinstance(v, str) and len(v) <= cap else ""
    # beside a "summary", "description" is the store's paragraphs
    details = entry.get("description", "") if "summary" in entry else entry.get("details", "")
    out = {"details": text(details, 2000), "category": text(entry.get("category", ""), 40),
           "publisher": text(entry.get("publisher", ""), 60)}
    home = entry.get("homepage", "")
    out["homepage"] = home if isinstance(home, str) and home.startswith("https://") and len(home) <= 200 else ""
    icon = entry.get("icon", "")
    out["icon"] = icon if isinstance(icon, str) and icon.lstrip().startswith("<svg") \
        and len(icon.encode()) <= 16 * 1024 else ""
    return out


def tools_of(text: str) -> list[Tool]:
    """Every usable line of a catalogue file; a broken line is left out, a
    broken file gives nothing (and the other sources still count)."""
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    entries = doc.get("tools") if isinstance(doc, dict) else doc
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("kind") in ("aperture", "akari"):
            base = APERTURE if entry["kind"] == "aperture" else AKARI
            # a catalogue gives these two their words and mark, never their container
            out.append(dataclasses.replace(base, **_shown(entry),
                                           description=str(entry.get("summary") or base.description)[:200]))
            continue
        try:
            out.append(tool_of(entry))
        except Invalid:
            continue
    return out


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > CATALOGUE_MAX:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def catalogue_files() -> list[Path]:
    local = sorted((settings.data_dir / "catalogue.d").glob("*.json")) \
        if (settings.data_dir / "catalogue.d").is_dir() else []
    return [SHIPPED, settings.data_dir / "catalogue.json", *local]


def load_catalogue() -> dict[str, Tool]:
    found: dict[str, Tool] = {}
    for path in catalogue_files():
        for t in tools_of(_read(path)):
            found[t.kind] = t
    # Aperture stays the helper's code; a catalogue gives it only its words and mark
    shown = found.pop("aperture", APERTURE)
    out = {**found, "aperture": shown if isinstance(shown, Aperture) else APERTURE}
    # Akari only when a catalogue names it (the store decides it is offered), and then as code
    if "akari" in out and not isinstance(out["akari"], Akari):
        out["akari"] = AKARI
    return out


class _Catalogue(Mapping):
    """TOOLS as before (`kind in TOOLS`, `TOOLS[kind]`, iterating), read
    afresh from the catalogue files whenever one of them changed."""

    def __init__(self):
        self._stamp = None
        self._tools: dict[str, Tool] = {}

    def _now(self) -> dict[str, Tool]:
        stamp = []
        for p in catalogue_files():
            try:
                st = p.stat()
                stamp.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((str(p), None, None))
        stamp = tuple(stamp)
        if stamp != self._stamp:
            self._tools, self._stamp = load_catalogue(), stamp
        return self._tools

    def __getitem__(self, kind: str) -> Tool:
        return self._now()[kind]

    def __iter__(self):
        return iter(self._now())

    def __len__(self) -> int:
        return len(self._now())


TOOLS = _Catalogue()


def store_published(text: str) -> int:
    """The published catalogue, kept for the next start; how many tools it
    names. A file that names none is not kept (a broken download)."""
    if len(text.encode("utf-8")) > CATALOGUE_MAX:
        raise Invalid("Der Katalog ist zu groß.")
    found = tools_of(text)
    if not found:
        raise Invalid("Der Katalog nennt kein brauchbares Tool.")
    path = settings.data_dir / "catalogue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return len(found)


def ports_of(values: dict) -> set[int]:
    """Every host port a tool's values hold."""
    return {p for p in (values.get("port"), values.get("gallery_port"), values.get("ssh_port")) if p}


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
    if kind == "akari":
        rights = body.get("rights", "locked")
        if rights not in AKARI_RIGHTS:
            raise Invalid("Die Rechte sind locked (ohne sudo) oder sudo.")
        ssh = body.get("ssh_port", 0) or 0
        if not isinstance(ssh, int) or isinstance(ssh, bool) or not 0 <= ssh <= 65535:
            raise Invalid("Der SSH-Port ist eine Zahl von 1 bis 65535, oder leer für kein SSH.")
        if ssh and ssh in (port, settings.lucent_port):
            raise Invalid(f"Port {ssh} ist schon vergeben.")
        values.update(rights=rights, ssh_port=ssh)
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
    if kind == "akari":
        return akari_body(values)
    t = TOOLS[kind]
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"])}
    mounts = [{"Type": "volume", "Source": settings.volume(kind), "Target": t.data}]
    return _hardened(settings.image(kind), kind, values, env, mounts, {t.port: values["port"]}, values["bind"])


def akari_body(values: dict) -> dict:
    """As Akari's own compose files. Both rights keep everything else hardened
    (no privileged mode, no host folders, one volume, its network) and get the
    same ten capabilities and nothing more:
      locked  read-only root and no-new-privileges (compose.locked.yaml): the
              accounts, desktops, apps and SSH work, sudo and apt do not;
      sudo    a writable root and setuid allowed (compose.yaml): admins may
              `sudo apt install`; what they install is kept in the volume and
              put back after an update.
    Firefox wants more than Docker's 64 MB of /dev/shm."""
    t = TOOLS["akari"]
    locked = values.get("rights", "locked") == "locked"
    ports = {t.port: values["port"]}
    env = {"TZ": settings.tz, "FORWARDED_ALLOW_IPS": _forwarded(values["bind"]), "AKARI_HOSTNAME": "akari"}
    if values.get("ssh_port"):
        ports[t.ssh] = values["ssh_port"]
        env["AKARI_SSH_PUBLIC"] = f"{urlsplit(values['ui_url']).hostname}:{values['ssh_port']}"
    mounts = [{"Type": "volume", "Source": settings.volume("akari"), "Target": "/data"}]
    body = _hardened(settings.image("akari"), "akari", values, env, mounts, ports, values["bind"])
    hc = body["HostConfig"]
    hc["CapAdd"] = list(AKARI_CAPS)
    hc["ReadonlyRootfs"] = locked
    hc["SecurityOpt"] = ["no-new-privileges:true"] if locked else []
    hc["Tmpfs"] = {"/tmp": "rw,noexec,nosuid,size=128m", "/run": "rw,noexec,nosuid,size=4m,mode=755"}
    hc["ShmSize"] = 512 * 1024 * 1024
    body["Hostname"] = "akari"
    return body


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


def restore_body(kind: str, image: str, backups_mount: dict, run: str, script: str) -> dict:
    """A one-off container to put an archive back: the tool's own image and
    user (only it owns its files), its data volume, the backups read-only,
    no network."""
    mount = {"Type": backups_mount["Type"], "Target": "/backups", "ReadOnly": True,
             "Source": backups_mount.get("Name") or backups_mount["Source"]}
    return {
        "Image": image,
        "Entrypoint": ["python"],
        "Cmd": ["-c", script, f"/backups/{run}/{kind}.tar.gz", "/data"],
        "Labels": {f"{LABEL}.oneoff": f"restore-{kind}"},
        "HostConfig": {"Mounts": [{"Type": "volume", "Source": settings.volume(kind), "Target": "/data"}, mount],
                       "NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                       "ReadonlyRootfs": True, "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
                       "Privileged": False},
    }


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
