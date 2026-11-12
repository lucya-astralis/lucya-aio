# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
import json

import pytest

from aio import secrets, templates
from aio.config import settings
from aio.docker import demux

from conftest import TOKEN


def tool(fake, kind):
    return next(c for c in fake.containers.values() if c["Name"] == f"/lucya-{kind}")


# ---- start-up -------------------------------------------------------------------
def test_boot_makes_networks_and_lucent(fake, stack):
    stack.boot(TOKEN, "ABCD-EFGH-JKLM")
    assert fake.networks["lucya"]["body"]["Internal"] is False
    assert fake.networks["lucya-aio"]["body"]["Internal"] is True
    lucent = tool(fake, "lucent")
    env = dict(e.split("=", 1) for e in lucent["body"]["Env"])
    assert env["LUCENT_AIO_TOKEN"] == TOKEN and env["LUCENT_SETUP_CODE"] == "ABCD-EFGH-JKLM"
    assert env["LUCENT_AIO_URL"] == "http://lucya-aio:8061"
    assert lucent["State"]["Running"]
    assert fake.networks["lucya-aio"]["members"][lucent["Id"]] == ["lucent"]
    assert lucent["body"]["NetworkingConfig"]["EndpointsConfig"]["lucya"]["Aliases"] == ["lucent"]
    assert lucent["body"]["HostConfig"]["PortBindings"] == {"8060/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8060"}]}


def test_boot_twice_keeps_lucent(fake, stack):
    stack.boot(TOKEN, "ABCD-EFGH-JKLM")
    stack.boot(TOKEN, "ABCD-EFGH-JKLM")
    assert len(fake.containers) == 1


def test_boot_pulls_a_missing_image(fake, stack):
    settings.image_overrides = {}
    stack.boot(TOKEN, "ABCD-EFGH-JKLM")
    assert fake.pulled == ["ghcr.io/lucya-astralis/lucent:stable"]


def test_boot_joins_the_helper_to_the_control_network(fake, stack):
    stack.boot(TOKEN, "X")
    fake.containers["self"] = {"Id": "self", "Name": "/lucya-aio", "Config": {"Image": "x", "Labels": {}},
                               "State": {"Running": True}}
    settings.self_id = "self"
    stack.boot(TOKEN, "X")
    assert fake.networks["lucya-aio"]["members"]["self"] == ["lucya-aio"]


def test_secrets_are_made_once_and_kept_private(tmp_path):
    first = secrets.load(tmp_path)
    assert secrets.load(tmp_path) == first
    assert (tmp_path / "aio.json").stat().st_mode & 0o777 == 0o600
    assert len(first["setup_code"]) == 14


# ---- the boundary: what a tool container may be -----------------------------------------------
def test_a_tool_is_made_hardened(fake, api):
    r = api.put("/tools/arca", json={"port": 18070, "bind": "0.0.0.0", "ui_url": "http://192.168.1.20:18070/"})
    assert r.status_code == 202, r.text
    c = tool(fake, "arca")
    hc = c["body"]["HostConfig"]
    assert hc["Privileged"] is False and hc["CapDrop"] == ["ALL"] and hc["ReadonlyRootfs"]
    assert "no-new-privileges:true" in hc["SecurityOpt"]
    assert hc["Mounts"] == [{"Type": "volume", "Source": "lucya-arca-data", "Target": "/data"}]
    assert hc["NetworkMode"] == "lucya" and "Binds" not in hc
    assert hc["PortBindings"] == {"8070/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18070"}]}
    assert c["body"]["Image"] == "arca:aio"
    assert c["body"]["NetworkingConfig"]["EndpointsConfig"]["lucya"]["Aliases"] == ["arca"]
    assert json.loads(c["Config"]["Labels"]["systems.lucya.aio.config"])["ui_url"] == "http://192.168.1.20:18070"


def test_only_values_are_taken_from_lucent(fake, api):
    r = api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test", "Image": "alpine",
                                     "HostConfig": {"Privileged": True}, "Binds": ["/:/host"]})
    assert r.status_code == 202
    body = tool(fake, "arca")["body"]
    assert body["Image"] == "arca:aio" and body["HostConfig"]["Privileged"] is False
    assert "/host" not in json.dumps(body)


def test_loopback_means_a_proxy_in_front(fake, api):
    api.put("/tools/fila", json={"port": 8085, "bind": "127.0.0.1", "ui_url": "https://fila.example"})
    env = tool(fake, "fila")["body"]["Env"]
    assert "FORWARDED_ALLOW_IPS=*" in env


@pytest.mark.parametrize("body, word", [
    ({"port": 0, "ui_url": "http://x.test"}, "Port"),
    ({"port": "8070", "ui_url": "http://x.test"}, "Port"),
    ({"port": 8060, "ui_url": "http://x.test"}, "Lucent"),
    ({"port": 8070, "bind": "everywhere", "ui_url": "http://x.test"}, "Bind"),
    ({"port": 8070, "ui_url": "javascript:alert(1)"}, "Adresse"),
    ({"port": 8070}, "Adresse"),
])
def test_bad_values_are_refused(fake, api, body, word):
    r = api.put("/tools/arca", json=body)
    assert r.status_code == 422 and word in r.json()["detail"]
    assert not fake.containers


def test_unknown_tools_are_refused(fake, api):
    r = api.put("/tools/alpine", json={"port": 9000, "ui_url": "http://x.test"})
    assert r.status_code == 422 and not fake.containers


def test_a_port_held_by_another_tool_is_refused(fake, api):
    api.put("/tools/arca", json={"port": 9000, "ui_url": "http://x.test"})
    r = api.put("/tools/fila", json={"port": 9000, "ui_url": "http://y.test"})
    assert r.status_code == 422 and "arca" in r.json()["detail"]


def test_the_token_is_required(api):
    assert api.get("/health").status_code == 200
    for header in ({}, {"Authorization": "Bearer wrong"}):
        api.headers.pop("Authorization", None)
        api.headers.update(header)
        assert api.get("/state").status_code == 401
        assert api.put("/tools/arca", json={"port": 9000, "ui_url": "http://x.test"}).status_code == 401


# ---- the job ------------------------------------------------------------------------------
def test_state_shows_every_tool_and_its_step(fake, api):
    api.put("/tools/strata", json={"port": 8086, "ui_url": "http://x.test:8086"})
    s = api.get("/state").json()
    by = {t["kind"]: t for t in s["tools"]}
    assert set(by) == {"arca", "fila", "strata"}
    assert by["arca"]["state"] == "absent" and by["arca"]["default_port"] == 8070
    assert by["strata"]["state"] == "running" and by["strata"]["job"]["step"] == "ready"
    assert by["strata"]["base_url"] == "http://strata:8086" and 8086 in s["ports_in_use"]


def test_a_failed_start_ends_the_job_with_the_reason(fake, api):
    fake.start_error = "port is already allocated"
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "arca")["job"]
    assert job["step"] == "error" and "already allocated" in job["error"]


def test_an_unhealthy_tool_ends_the_job(fake, api):
    fake.health = "unhealthy"
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "arca")["job"]
    assert job["step"] == "error" and "krank" in job["error"]


def test_the_same_values_again_change_nothing(fake, api):
    body = {"port": 8070, "ui_url": "http://x.test"}
    api.put("/tools/arca", json=body)
    first = tool(fake, "arca")["Id"]
    api.put("/tools/arca", json=body)
    assert tool(fake, "arca")["Id"] == first


def test_new_values_remake_the_container_and_keep_the_volume(fake, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    first = tool(fake, "arca")["Id"]
    api.put("/tools/arca", json={"port": 18070, "ui_url": "http://x.test:18070"})
    c = tool(fake, "arca")
    assert c["Id"] != first and len(fake.containers) == 1
    assert c["body"]["HostConfig"]["Mounts"][0]["Source"] == "lucya-arca-data"


# ---- pairing ------------------------------------------------------------------------------
def test_join_runs_the_tools_own_command(fake, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://192.168.1.20:8085"})
    r = api.post("/tools/fila/join", json={"code": "ABCD-EFGH-JKLM"})
    assert r.status_code == 200 and r.json() == {"ok": True, "exit_code": 0, "output": "paired\n"}
    cid, argv = fake.execs[-1]
    assert cid == tool(fake, "fila")["Id"]
    assert argv == ["python", "-m", "fila", "--lucent-join", "http://lucent:8060", "ABCD-EFGH-JKLM",
                    "--ui-url", "http://192.168.1.20:8085", "--base-url", "http://fila:8085", "--role", "editor"]


def test_join_reports_a_failed_command(fake, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    fake.exec_result = (1, b"Der Kopplungscode ist unbekannt\n")
    r = api.post("/tools/arca/join", json={"code": "ABCD-EFGH-JKLM", "role": "reader"})
    assert r.json()["ok"] is False and "unbekannt" in r.json()["output"]
    assert fake.execs[-1][1][-1] == "reader"


@pytest.mark.parametrize("body", [{"code": "ABCD; rm -rf /"}, {"code": "--help"},
                                  {"code": "ABCD-EFGH-JKLM", "role": "root"}])
def test_join_takes_no_arguments_of_its_own(fake, api, body):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.post("/tools/arca/join", json=body).status_code == 422
    assert not fake.execs


def test_join_again_forgets_the_old_pairing_first(fake, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    r = api.post("/tools/fila/join", json={"code": "ABCD-EFGH-JKLM", "again": True})
    assert r.json()["ok"]
    assert [argv[3] for _, argv in fake.execs] == ["--lucent-leave", "--lucent-join"]
    assert api.post("/tools/fila/join", json={"code": "ABCD-EFGH-JKLM", "again": "yes"}).status_code == 422


def test_a_first_join_leaves_nothing(fake, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    api.post("/tools/fila/join", json={"code": "ABCD-EFGH-JKLM"})
    assert [argv[3] for _, argv in fake.execs] == ["--lucent-join"]


def test_join_needs_a_running_tool(fake, api):
    assert api.post("/tools/arca/join", json={"code": "ABCD-EFGH-JKLM"}).status_code == 422


def test_demux_reads_docker_frames():
    raw = b"\x01\0\0\0\0\0\0\x03abc\x02\0\0\0\0\0\0\x02de"
    assert demux(raw) == "abcde"
    assert demux(b"plain text") == "plain text"


# ---- restarting and removing ------------------------------------------------------------------
def test_restart(fake, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.post("/tools/arca/restart").status_code == 200
    assert fake.restarted == [tool(fake, "arca")["Id"]]
    assert api.post("/tools/fila/restart").status_code == 422


def test_remove_keeps_the_data_unless_asked(fake, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.delete("/tools/arca").json() == {"ok": True, "data": "keep"}
    assert not fake.containers and fake.volumes_removed == []
    s = {t["kind"]: t for t in api.get("/state").json()["tools"]}
    assert s["arca"]["state"] == "absent" and s["arca"]["job"] is None
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.delete("/tools/arca", params={"data": "delete"}).status_code == 200
    assert fake.volumes_removed == ["lucya-arca-data"]


def test_remove_refuses_odd_values_and_other_volumes(fake, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.delete("/tools/arca", params={"data": "all"}).status_code == 422
    assert api.delete("/tools/lucent").status_code == 422          # Lucent is not a tool
    assert fake.containers and not fake.volumes_removed
