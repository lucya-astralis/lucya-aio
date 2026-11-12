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
from aio.docker import Docker
from aio.stack import Stack

TOKEN = "t" * 40


class FakeDocker:
    def __init__(self):
        self.images = {"lucent:aio", "arca:aio", "fila:aio", "strata:aio"}
        self.containers: dict[str, dict] = {}      # id -> inspect
        self.networks: dict[str, dict] = {}
        self.pulled, self.execs, self.calls = [], [], []
        self.health = "healthy"
        self.exec_result = (0, b"paired\n")
        self.start_error = None
        self._n = 0

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
            return httpx.Response(200 if unquote(mm[1]) in self.images else 404, json={"message": "no such image"})
        if path == "/images/create":
            image = f"{q['fromImage']}:{q['tag']}"
            self.pulled.append(image)
            self.images.add(image)
            return httpx.Response(200, text='{"status":"done"}\n')
        if path == "/containers/json":
            label = json.loads(q["filters"])["label"][0]
            return httpx.Response(200, json=[
                {"Id": c["Id"], "Names": [c["Name"]], "Image": c["Config"]["Image"],
                 "State": "running" if c["State"]["Running"] else "exited", "Status": "",
                 "Labels": c["Config"]["Labels"]}
                for c in self.containers.values() if label in c["Config"]["Labels"]])
        if path == "/containers/create":
            self._n += 1
            cid = f"c{self._n:03d}"
            self.containers[cid] = {"Id": cid, "Name": "/" + q["name"], "body": body,
                                    "Config": {"Image": body["Image"], "Labels": body["Labels"]},
                                    "State": {"Running": False, "ExitCode": 0}}
            return httpx.Response(201, json={"Id": cid})
        if mm := re.fullmatch(r"/containers/([^/]+)/(json|start|stop|exec)", path):
            c = self.find(mm[1])
            if c is None:
                return httpx.Response(404, json={"message": "no such container"})
            if mm[2] == "json":
                return httpx.Response(200, json=c)
            if mm[2] == "start":
                if self.start_error:
                    return httpx.Response(500, json={"message": self.start_error})
                c["State"] = {"Running": True, "Health": {"Status": self.health}}
                return httpx.Response(204)
            if mm[2] == "stop":
                c["State"] = {"Running": False, "ExitCode": 0}
                return httpx.Response(204)
            self.execs.append((c["Id"], body["Cmd"]))
            return httpx.Response(201, json={"Id": "e1"})
        if mm := re.fullmatch(r"/containers/([^/]+)", path):
            c = self.find(mm[1])
            if c:
                del self.containers[c["Id"]]
            return httpx.Response(204)
        if path == "/exec/e1/start":
            out = self.exec_result[1]
            return httpx.Response(200, content=struct.pack(">BxxxI", 1, len(out)) + out)
        if path == "/exec/e1/json":
            return httpx.Response(200, json={"ExitCode": self.exec_result[0]})
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
                                      "strata": "strata:aio"})
    return FakeDocker()


@pytest.fixture()
def stack(fake):
    return InlineStack(Docker(transport=httpx.MockTransport(fake)), sleep=lambda s: None)


@pytest.fixture()
def api(stack):
    c = TestClient(create_app(stack, TOKEN))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c
