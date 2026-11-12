# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""The few calls of the Docker Engine API the helper needs, over the Unix
socket. No SDK: the surface stays small enough to read in one sitting,
and everything the helper can do to the host is in this file."""

from __future__ import annotations

import json
import struct
from urllib.parse import quote

import httpx


class DockerError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Docker:
    def __init__(self, socket: str | None = None, transport: httpx.BaseTransport | None = None):
        transport = transport or httpx.HTTPTransport(uds=socket)
        # pulls and waits can take a while; each call still ends
        self.http = httpx.Client(transport=transport, base_url="http://docker", timeout=httpx.Timeout(30, read=600))

    def _call(self, method: str, path: str, *, ok=(200, 201, 204), **kw) -> httpx.Response:
        r = self.http.request(method, path, **kw)
        if r.status_code not in ok:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise DockerError(r.status_code, msg.strip() or f"Docker antwortet {r.status_code}")
        return r

    def ping(self) -> bool:
        return self._call("GET", "/_ping").text == "OK"

    # ---- images -------------------------------------------------------------
    def has_image(self, image: str) -> bool:
        try:
            self._call("GET", f"/images/{quote(image, safe='')}/json")
            return True
        except DockerError as e:
            if e.status == 404:
                return False
            raise

    def pull(self, image: str) -> None:
        repo, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "latest")
        r = self._call("POST", "/images/create", params={"fromImage": repo, "tag": tag})
        for line in r.text.splitlines():          # errors come inside a 200 stream
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("error"):
                raise DockerError(500, msg["error"])

    # ---- containers -----------------------------------------------------------
    def inspect(self, name: str) -> dict | None:
        try:
            return self._call("GET", f"/containers/{quote(name, safe='')}/json").json()
        except DockerError as e:
            if e.status == 404:
                return None
            raise

    def managed(self, label: str) -> list[dict]:
        filters = json.dumps({"label": [label]})
        return self._call("GET", "/containers/json", params={"all": "1", "filters": filters}).json()

    def create(self, name: str, body: dict) -> str:
        return self._call("POST", "/containers/create", params={"name": name}, json=body).json()["Id"]

    def start(self, cid: str) -> None:
        self._call("POST", f"/containers/{cid}/start", ok=(204, 304))

    def stop(self, cid: str) -> None:
        self._call("POST", f"/containers/{cid}/stop", params={"t": "20"}, ok=(204, 304))

    def remove(self, cid: str) -> None:
        self._call("DELETE", f"/containers/{cid}", params={"force": "1"}, ok=(204, 404))

    # ---- networks ---------------------------------------------------------------
    def ensure_network(self, name: str, *, internal: bool, labels: dict) -> None:
        try:
            self._call("GET", f"/networks/{quote(name, safe='')}")
            return
        except DockerError as e:
            if e.status != 404:
                raise
        self._call("POST", "/networks/create",
                   json={"Name": name, "Driver": "bridge", "Internal": internal, "Labels": labels})

    def connect(self, network: str, cid: str, aliases: list[str]) -> None:
        try:
            self._call("POST", f"/networks/{quote(network, safe='')}/connect",
                       json={"Container": cid, "EndpointConfig": {"Aliases": aliases}})
        except DockerError as e:
            if e.status == 403 and "already" in str(e):   # already on it
                return
            raise

    # ---- exec ---------------------------------------------------------------------
    def exec(self, cid: str, argv: list[str]) -> tuple[int, str]:
        """Run argv in the container, as its own user; exit code and output."""
        eid = self._call("POST", f"/containers/{cid}/exec",
                         json={"Cmd": argv, "AttachStdout": True, "AttachStderr": True}).json()["Id"]
        raw = self._call("POST", f"/exec/{eid}/start", json={"Detach": False, "Tty": False}).content
        code = self._call("GET", f"/exec/{eid}/json").json().get("ExitCode")
        return (code if code is not None else -1), demux(raw)


def demux(raw: bytes) -> str:
    """Docker's multiplexed stream: an 8-byte header (stream, 0, 0, 0, size)
    before each frame. Anything that does not look like that is taken as is."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    out.append(raw[i:])
    return b"".join(out).decode("utf-8", "replace")
