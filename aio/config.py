# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""Everything the helper is told comes from its own environment, set once
with `docker run`. Nothing here can be changed through the API: that is
the point. Lucent sends values (a port, an address), never images, mounts
or privileges."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _paths(spec: str) -> list[str]:
    """LUCYA_ALLOWED_PATHS=/srv/fotos,/mnt/nas/bilder: the only host folders a
    container may get (Aperture's photos), set with `docker run`."""
    import posixpath
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if part.startswith("/"):
            out.append(posixpath.normpath(part))
    return out


def _images(spec: str) -> dict[str, str]:
    """LUCYA_IMAGES=lucent=lucent:aio,arca=arca:aio: images built on this
    machine instead of the registry's (development)."""
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        kind, _, image = part.partition("=")
        if kind and image:
            out[kind.strip()] = image.strip()
    return out


@dataclass
class Settings:
    socket: str = field(default_factory=lambda: _env("LUCYA_SOCKET", "/var/run/docker.sock"))
    data_dir: Path = field(default_factory=lambda: Path(_env("LUCYA_DATA", "/data")))
    # where a browser first finds Lucent; the setup narrows it later
    lucent_port: int = field(default_factory=lambda: int(_env("LUCYA_PORT", "8060")))
    lucent_bind: str = field(default_factory=lambda: _env("LUCYA_BIND", "0.0.0.0"))
    network: str = field(default_factory=lambda: _env("LUCYA_NETWORK", "lucya"))
    control_network: str = field(default_factory=lambda: _env("LUCYA_CONTROL_NETWORK", "lucya-aio"))
    prefix: str = field(default_factory=lambda: _env("LUCYA_PREFIX", "lucya-"))
    registry: str = field(default_factory=lambda: _env("LUCYA_REGISTRY", "ghcr.io/lucya-astralis/"))
    channel: str = field(default_factory=lambda: _env("LUCYA_CHANNEL", "stable"))
    image_overrides: dict = field(default_factory=lambda: _images(_env("LUCYA_IMAGES")))
    allowed_paths: list = field(default_factory=lambda: _paths(_env("LUCYA_ALLOWED_PATHS")))
    tz: str = field(default_factory=lambda: _env("TZ", "Europe/Berlin"))
    # where backups go: what `docker run -v …:/backups` mounted
    backups: Path = field(default_factory=lambda: Path(_env("LUCYA_BACKUPS", "/backups")))
    api_port: int = field(default_factory=lambda: int(_env("LUCYA_API_PORT", "8061")))
    # this container's own id, to join it to the control network
    self_id: str = field(default_factory=lambda: _env("HOSTNAME"))

    def image(self, kind: str) -> str:
        return self.image_overrides.get(kind) or f"{self.registry}{kind}:{self.channel}"

    def own_image(self, kind: str, which: str) -> str:
        """The helper's own names for a container's image: `current` for the
        one it runs, `previous` for the one before the last update. Docker
        with containerd's image store forgets an image as soon as no name
        points to it, even while a container runs it, and a pull or a build
        moves the tag. With its own name the image stays, to go back to."""
        return f"{self.prefix}images/{kind}:{which}"

    def container(self, kind: str) -> str:
        return f"{self.prefix}{kind}"

    def volume(self, kind: str, what: str = "data") -> str:
        return f"{self.prefix}{kind}-{what}"


settings = Settings()


def configure(**overrides) -> Settings:
    fresh = Settings()
    for k, v in overrides.items():
        setattr(fresh, k, v)
    settings.__dict__.update(fresh.__dict__)
    return settings
