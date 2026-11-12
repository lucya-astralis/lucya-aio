# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""A Docker Engine that lives in a dict: enough of the API for the helper."""
import json
import re
import struct
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from aio import config
from aio.api import create_app
from aio.backup import Backups
from aio.docker import Docker
from aio.stack import Stack

TOKEN = "t" * 40


class FakeDocker:
    def __init__(self):
        # name -> image id; a rebuild is a new id under the same name
        self.images = {n: f"sha256:{i:064x}" for i, n in
                       enumerate(("lucent:aio", "arca:aio", "fila:aio", "strata:aio", "aperture:aio"), 1)}
        self.oneoff_code, self.oneoffs = 0, []
        self.containers: dict[str, dict] = {}      # id -> inspect
        self.networks: dict[str, dict] = {}
        self.pulled, self.execs, self.calls = [], [], []
        self.volumes_removed, self.restarted = [], []
        self.health = "healthy"
        self.exec_result = (0, b"paired\n")
        self.start_error = None
        self.bad_image = None          # with start_error: only this image fails
        self.exec_out = {}             # argv[2] start -> (code, stdout, stderr)
        self._n = 0

    def _kept(self, iid: str) -> bool:
        """Like Docker with containerd's image store: an image is found by its
        id only while some name points to it. Once its tag moves on, a
        container may still run it, but it cannot be named or started anew."""
        return iid in self.images.values()

    def _exec_answer(self):
        cmd = self.current_exec
        if len(cmd) > 2 and cmd[1] == "-c":                 # the backup snapshot
            return self.exec_out.get("snapshot", (0, b"\x1f\x8b fake tar", b"x.db\n"))
        return self.exec_result[0], self.exec_result[1], b""

    def find(self, ref):
        ref = unquote(ref)
        for c in self.containers.values():
            if c["Id"] == ref or c["Name"] == "/" + ref:
                return c
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m, path = request.method, request.url.path
        self.calls.append((m, path))
        body = json.loads(request.content) if request.content else None
        q = request.url.params
        if path == "/_ping":
            return httpx.Response(200, text="OK")
        if mm := re.fullmatch(r"/images/(.+)/json", path):
            ref = unquote(mm[1])
            iid = self.images.get(ref) or (ref if self._kept(ref) else None)
            if iid is None:
                return httpx.Response(404, json={"message": "no such image"})
            return httpx.Response(200, json={"Id": iid, "Created": "2026-11-12T10:00:00Z"})
        if mm := re.fullmatch(r"/images/(.+)/tag", path):
            ref = unquote(mm[1])
            iid = self.images.get(ref) or (ref if self._kept(ref) else None)
            if iid is None:
                return httpx.Response(404, json={"message": "no such image"})
            self.images[f"{q['repo']}:{q['tag']}"] = iid
            return httpx.Response(201)
        if path == "/images/create":
            image = f"{q['fromImage']}:{q['tag']}"
            self.pulled.append(image)
            self.images.setdefault(image, f"sha256:{len(self.images) + 100:064x}")
            return httpx.Response(200, text='{"status":"done"}\n')
        if path == "/containers/json":
            label = json.loads(q["filters"])["label"][0]
            return httpx.Response(200, json=[
                {"Id": c["Id"], "Names": [c["Name"]], "Image": c["Config"]["Image"], "ImageID": c["Image"],
                 "State": "running" if c["State"]["Running"] else "exited", "Status": "",
                 "Labels": c["Config"]["Labels"]}
                for c in self.containers.values() if label in c["Config"]["Labels"]])
        if path == "/containers/create":
            self._n += 1
            cid = f"c{self._n:03d}"
            image = body["Image"]
            iid = self.images.get(image) or (image if self._kept(image) else None)
            if iid is None:
                return httpx.Response(404, json={"message": f"No such image: {image}"})
            self.containers[cid] = {"Id": cid, "Name": "/" + q["name"], "body": body, "Image": iid,
                                    "Config": {"Image": body["Image"], "Labels": body["Labels"]},
                                    "State": {"Running": False, "ExitCode": 0}}
            return httpx.Response(201, json={"Id": cid})
        if mm := re.fullmatch(r"/volumes/(.+)", path):
            self.volumes_removed.append(unquote(mm[1]))
            return httpx.Response(204)
        if mm := re.fullmatch(r"/containers/([^/]+)/(wait|logs)", path):
            c = self.find(mm[1])
            if mm[2] == "wait":
                self.oneoffs.append(c["body"])
                return httpx.Response(200, json={"StatusCode": self.oneoff_code})
            msg = b"ValueError: an Aperture password needs at least 8 characters\n"
            return httpx.Response(200, content=struct.pack(">BxxxI", 2, len(msg)) + msg)
        if mm := re.fullmatch(r"/containers/([^/]+)/(json|start|stop|exec|restart)", path):
            c = self.find(mm[1])
            if c is None:
                return httpx.Response(404, json={"message": "no such container"})
            if mm[2] == "json":
                return httpx.Response(200, json=c)
            if mm[2] == "start":
                if self.start_error and (self.bad_image is None or c["Image"] == self.bad_image):
                    return httpx.Response(500, json={"message": self.start_error})
                c["State"] = {"Running": True, "Health": {"Status": self.health}}
                return httpx.Response(204)
            if mm[2] == "restart":
                self.restarted.append(c["Id"])
                return httpx.Response(204)
            if mm[2] == "stop":
                c["State"] = {"Running": False, "ExitCode": 0}
                return httpx.Response(204)
            self.execs.append((c["Id"], body["Cmd"]))
            self.current_exec = body["Cmd"]
            return httpx.Response(201, json={"Id": "e1"})
        if mm := re.fullmatch(r"/containers/([^/]+)", path):
            c = self.find(mm[1])
            if c:
                del self.containers[c["Id"]]
            return httpx.Response(204)
        if path == "/exec/e1/start":
            code, out, err = self._exec_answer()
            frames = struct.pack(">BxxxI", 1, len(out)) + out
            if err:
                frames += struct.pack(">BxxxI", 2, len(err)) + err
            return httpx.Response(200, content=frames)
        if path == "/exec/e1/json":
            return httpx.Response(200, json={"ExitCode": self._exec_answer()[0]})
        if path == "/networks/create":
            self.networks[body["Name"]] = {"body": body, "members": {}}
            return httpx.Response(201, json={"Id": body["Name"]})
        if mm := re.fullmatch(r"/networks/([^/]+)/connect", path):
            self.networks[unquote(mm[1])]["members"][body["Container"]] = body["EndpointConfig"]["Aliases"]
            return httpx.Response(200)
        if mm := re.fullmatch(r"/networks/([^/]+)", path):
            return httpx.Response(200 if unquote(mm[1]) in self.networks else 404, json={"message": "no such network"})
        return httpx.Response(500, json={"message": f"fake has no {m} {path}"})


class InlineStack(Stack):
    """Jobs run in the request, so a test sees their end."""
    def _spawn(self, fn, *args):
        fn(*args)


@pytest.fixture()
def fake(tmp_path):
    config.configure(data_dir=tmp_path, self_id="",
                     image_overrides={"lucent": "lucent:aio", "arca": "arca:aio", "fila": "fila:aio",
                                      "strata": "strata:aio", "aperture": "aperture:aio"},
                     allowed_paths=["/srv/fotos"])
    return FakeDocker()


@pytest.fixture()
def stack(fake, tmp_path):
    docker = Docker(transport=httpx.MockTransport(fake))
    (tmp_path / "backups").mkdir()
    return InlineStack(docker, backups=Backups(docker, tmp_path / "backups"), sleep=lambda s: None)


@pytest.fixture()
def api(stack):
    c = TestClient(create_app(stack, TOKEN))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c
