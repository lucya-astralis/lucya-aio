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
    assert set(by) == {"arca", "fila", "strata", "aperture"}
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


# ---- backups ----------------------------------------------------------------------------------
from datetime import datetime  # noqa: E402


def test_a_backup_holds_one_archive_per_container(fake, stack, api, tmp_path):
    stack.boot(TOKEN, "X")
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert api.post("/backup").status_code == 202
    runs = api.get("/state").json()["backup"]["runs"]
    assert len(runs) == 1 and runs[0]["ok"] and runs[0]["kinds"] == ["arca", "lucent"]
    folder = tmp_path / "backups" / runs[0]["name"]
    assert sorted(f.name for f in folder.iterdir()) == ["arca.tar.gz", "backup.json", "lucent.tar.gz"]
    assert (folder / "arca.tar.gz").read_bytes().startswith(b"\x1f\x8b")
    snap = [argv for _, argv in fake.execs if argv[:2] == ["python", "-c"]]
    assert len(snap) == 2 and all(a[-1] == "/data" and "VACUUM INTO" in a[2] for a in snap)


def test_a_failed_snapshot_is_named_and_leaves_no_file(fake, stack, api, tmp_path):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    fake.exec_out["snapshot"] = (1, b"", b"Traceback\nsqlite3.OperationalError: disk I/O error\n")
    api.post("/backup")
    run = api.get("/state").json()["backup"]["runs"][0]
    assert not run["ok"] and "disk I/O error" in run["failed"]["fila"]
    assert not list((tmp_path / "backups" / run["name"]).glob("*.gz"))


def test_old_backups_go(fake, stack, api, tmp_path):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    stack.backups.set_schedule(False, "03:30", 2)
    for i in range(4):
        (tmp_path / "backups" / f"2026-01-0{i + 1}_030000").mkdir()
    api.post("/backup")
    assert len([p for p in (tmp_path / "backups").iterdir() if p.is_dir()]) == 2


def test_no_backups_without_the_mount(fake, tmp_path, api, stack):
    stack.backups.root = tmp_path / "nowhere"
    assert api.get("/state").json()["backup"] == {"available": False}
    assert api.post("/backup").status_code == 422


def test_the_schedule(fake, api, stack):
    r = api.put("/backup/schedule", json={"enabled": True, "time": "02:15", "keep": 7})
    assert r.status_code == 200 and r.json()["keep"] == 7
    for bad in ({"enabled": "yes", "time": "02:15", "keep": 7}, {"enabled": True, "time": "25:00", "keep": 7},
                {"enabled": True, "time": "02:15", "keep": 0}):
        assert api.put("/backup/schedule", json=bad).status_code == 422
    b = stack.backups
    assert not b.due(datetime(2026, 11, 12, 2, 14))
    assert b.due(datetime(2026, 11, 12, 2, 15))
    b.mark_done_today(datetime(2026, 11, 12, 2, 15))
    assert not b.due(datetime(2026, 11, 12, 23, 0)) and b.due(datetime(2026, 11, 13, 2, 30))


def test_the_daily_tick_backs_up_once(fake, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    stack.backups.set_schedule(True, "03:30", 14)
    stack.tick(datetime(2026, 11, 12, 3, 31))
    stack.tick(datetime(2026, 11, 12, 3, 32))
    assert len(stack.backups.list()) == 1


# ---- updates ----------------------------------------------------------------------------------
def test_a_new_image_shows_as_update_and_is_taken(fake, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    old = tool(fake, "arca")
    arca = lambda: next(t for t in api.get("/state").json()["tools"] if t["kind"] == "arca")  # noqa: E731
    assert arca()["update"] is False
    fake.images["arca:aio"] = "sha256:" + "a" * 64                 # rebuilt
    assert arca()["update"] is True
    assert api.post("/tools/arca/update").status_code == 202
    new = tool(fake, "arca")
    assert new["Id"] != old["Id"] and new["Image"] == "sha256:" + "a" * 64
    assert new["body"]["HostConfig"]["Mounts"] == old["body"]["HostConfig"]["Mounts"]
    assert arca()["update"] is False and arca()["job"]["step"] == "ready"
    run = stack.backups.list()[0]
    assert run["kinds"] == ["arca"] and "vor dem Update" in run["why"]


def test_an_update_that_does_not_start_goes_back(fake, stack, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    old_image = tool(fake, "fila")["Image"]
    fake.images["fila:aio"] = "sha256:" + "b" * 64
    fake.start_error, fake.bad_image = "exec format error", "sha256:" + "b" * 64
    api.post("/tools/fila/update")
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "fila")["job"]
    assert job["step"] == "error" and "alte Version läuft wieder" in job["error"]
    c = tool(fake, "fila")
    assert c["Image"] == old_image and c["State"]["Running"] and len(fake.containers) == 1
    assert c["body"]["Image"] == "lucya-images/fila:previous"
    assert fake.images["lucya-images/fila:previous"] == old_image


def test_an_update_without_a_new_image_changes_nothing(fake, stack, api):
    api.put("/tools/strata", json={"port": 8086, "ui_url": "http://x.test"})
    first = tool(fake, "strata")["Id"]
    api.post("/tools/strata/update")
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "strata")["job"]
    assert job["step"] == "ready" and job["note"] == "Schon aktuell." and tool(fake, "strata")["Id"] == first


def test_lucent_updates_and_stays_on_the_control_network(fake, stack, api):
    stack.boot(TOKEN, "ABCD-EFGH-JKLM")
    fake.images["lucent:aio"] = "sha256:" + "c" * 64
    assert api.get("/state").json()["lucent"]["update"] is True
    api.post("/tools/lucent/update")
    new = tool(fake, "lucent")
    assert new["Image"] == "sha256:" + "c" * 64
    assert fake.networks["lucya-aio"]["members"][new["Id"]] == ["lucent"]
    assert "LUCENT_AIO_TOKEN=" + TOKEN in new["body"]["Env"]
    assert api.get("/state").json()["lucent"]["job"]["step"] == "ready"


def test_check_pulls_registry_images_only(fake, stack, api):
    from aio.config import settings
    stack.boot(TOKEN, "X")
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    settings.image_overrides = {"lucent": "lucent:aio"}
    fake.images["ghcr.io/lucya-astralis/arca:stable"] = "sha256:" + "d" * 64
    api.post("/updates/check")
    assert fake.pulled == ["ghcr.io/lucya-astralis/arca:stable"]


def test_update_all_takes_every_update_one_by_one_lucent_last(fake, stack, api):
    stack.boot(TOKEN, "X")
    for k, port in (("arca", 8070), ("fila", 8085), ("strata", 8086)):
        api.put(f"/tools/{k}", json={"port": port, "ui_url": "http://x.test"})
    fake.images["arca:aio"] = "sha256:" + "1" * 64
    fake.images["lucent:aio"] = "sha256:" + "2" * 64
    r = api.post("/updates/all")
    assert r.status_code == 202 and r.json()["kinds"] == ["arca", "lucent"]
    assert tool(fake, "arca")["Image"] == "sha256:" + "1" * 64 and tool(fake, "lucent")["Image"] == "sha256:" + "2" * 64
    assert [b["kinds"] for b in stack.backups.list()] == [["lucent"], ["arca"]]   # newest first


def test_docker_times_are_shown_in_the_local_zone(monkeypatch):
    import time as _t
    from aio.stack import _local
    if not hasattr(_t, "tzset"):
        pytest.skip("this Python cannot switch zones (no time.tzset)")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    _t.tzset()
    assert _local("2026-11-12T14:12:03.123456789Z") == "2026-11-12T15:12:03+01:00"
    assert _local("") == "" and _local("soon") == "soon"
    monkeypatch.delenv("TZ")
    _t.tzset()


def test_every_container_names_its_image(fake, stack, api):
    stack.boot(TOKEN, "X")
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    assert fake.images["lucya-images/arca:current"] == tool(fake, "arca")["Image"]
    assert fake.images["lucya-images/lucent:current"] == tool(fake, "lucent")["Image"]


def test_no_update_without_a_way_back(fake, stack, api):
    api.put("/tools/strata", json={"port": 8086, "ui_url": "http://x.test"})
    del fake.images["lucya-images/strata:current"]          # made before the helper named images
    old = tool(fake, "strata")
    fake.images["strata:aio"] = "sha256:" + "e" * 64           # the tag moved: the old id has no name now
    api.post("/tools/strata/update")
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "strata")["job"]
    assert job["step"] == "error" and "keinen Namen" in job["error"]
    assert tool(fake, "strata")["Id"] == old["Id"]             # nothing was touched
    assert stack.backups.list() == []                          # and no backup made for nothing


def test_boot_names_the_images_of_containers_made_before(fake, stack, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    del fake.images["lucya-images/fila:current"]
    stack.boot(TOKEN, "X")
    assert fake.images["lucya-images/fila:current"] == tool(fake, "fila")["Image"]


# ---- Aperture: photos from a host folder, two ports, a password first -------------------------
APERTURE = {"port": 8090, "gallery_port": 8000, "ui_url": "http://x.test:8090", "photos": "/srv/fotos/2026"}


def test_aperture_gets_its_photos_two_ports_and_a_check(fake, api):
    assert api.put("/tools/aperture", json=APERTURE).status_code == 202
    body = tool(fake, "aperture")["body"]
    hc = body["HostConfig"]
    binds = [m for m in hc["Mounts"] if m["Type"] == "bind"]
    assert binds == [{"Type": "bind", "Source": "/srv/fotos/2026", "Target": "/photos"}]
    assert sorted(m["Source"] for m in hc["Mounts"] if m["Type"] == "volume") == [
        "lucya-aperture-data", "lucya-aperture-previews", "lucya-aperture-thumbnails"]
    assert hc["PortBindings"] == {"8090/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8090"}],
                                  "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}]}
    assert "APERTURE_ALLOW_OPEN=0" in body["Env"] and hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"]
    assert "/api/health" in " ".join(body["Healthcheck"]["Test"])


@pytest.mark.parametrize("photos, word", [
    ("/etc", "nicht freigegeben"), ("/srv/fotosX", "nicht freigegeben"), ("/srv/fotos/../../etc", "absoluter"),
    ("fotos", "absoluter"), ("", "absoluter")])
def test_only_released_folders_for_photos(fake, api, photos, word):
    r = api.put("/tools/aperture", json={**APERTURE, "photos": photos})
    assert r.status_code == 422 and word in r.json()["detail"] and not fake.containers


def test_no_folders_released_no_aperture(fake, api):
    from aio.config import settings
    settings.allowed_paths = []
    r = api.put("/tools/aperture", json=APERTURE)
    assert r.status_code == 422 and "LUCYA_ALLOWED_PATHS" in r.json()["detail"]


def test_the_gallery_port_counts_against_other_tools(fake, api):
    api.put("/tools/aperture", json=APERTURE)
    r = api.put("/tools/arca", json={"port": 8000, "ui_url": "http://x.test"})
    assert r.status_code == 422 and "aperture" in r.json()["detail"]
    assert api.put("/tools/aperture", json={**APERTURE, "gallery_port": 8090}).status_code == 422


def test_a_password_is_set_once_before_the_first_start(fake, api, tmp_path):
    api.put("/tools/aperture", json=APERTURE)
    assert len(fake.oneoffs) == 1
    one = fake.oneoffs[0]
    assert one["HostConfig"]["NetworkMode"] == "none" and one["HostConfig"]["Mounts"][0]["Target"] == "/data"
    assert "set_password" in one["Cmd"][2]
    assert [c["Name"] for c in fake.containers.values()] == ["/lucya-aperture"]      # the one-off is gone
    pw = api.get("/tools/aperture/password").json()["password"]
    assert f"APERTURE_PASSWORD={pw}" in one["Env"] and len(pw) >= 20
    assert (tmp_path / "passwords.json").stat().st_mode & 0o777 == 0o600
    api.put("/tools/aperture", json={**APERTURE, "port": 18090, "ui_url": "http://x.test:18090"})
    assert len(fake.oneoffs) == 1                                       # not again


def test_a_failed_password_keeps_aperture_from_starting(fake, api):
    fake.oneoff_code = 1
    api.put("/tools/aperture", json=APERTURE)
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "aperture")["job"]
    assert job["step"] == "error" and "8 characters" in job["error"]
    assert not [c for c in fake.containers.values() if c["Name"] == "/lucya-aperture"]
    assert api.get("/tools/aperture/password").status_code == 422


def test_aperture_joins_and_leaves_with_its_own_cli(fake, api):
    api.put("/tools/aperture", json=APERTURE)
    api.post("/tools/aperture/join", json={"code": "ABCD-EFGH-JKLM", "again": True})
    argvs = [argv for _, argv in fake.execs]
    assert argvs[-2] == ["python", "-m", "aperture.cli", "lucent", "leave"]
    assert argvs[-1] == ["python", "-m", "aperture.cli", "lucent", "join", "http://lucent:8060", "ABCD-EFGH-JKLM",
                         "--ui-url", "http://x.test:8090", "--base-url", "http://aperture:8090"]


def test_removing_aperture_with_its_data_takes_all_three_volumes_and_the_password(fake, api):
    api.put("/tools/aperture", json=APERTURE)
    api.delete("/tools/aperture", params={"data": "delete"})
    assert sorted(fake.volumes_removed) == ["lucya-aperture-data", "lucya-aperture-previews",
                                            "lucya-aperture-thumbnails"]
    assert api.get("/tools/aperture/password").status_code == 422


def test_state_says_which_folders_are_released(fake, api):
    s = api.get("/state").json()
    assert s["allowed_paths"] == ["/srv/fotos"]
    ap = next(t for t in s["tools"] if t["kind"] == "aperture")
    assert ap["default_port"] == 8090 and ap["default_gallery_port"] == 8000


def test_a_second_helper_on_the_host_is_not_seen(fake, stack, api):
    """Another helper's containers (another prefix) are not this one's: not
    in its state, not backed up, not named, not updated."""
    fake.containers["other"] = {"Id": "other", "Name": "/test-arca", "Image": fake.images["arca:aio"],
                                "body": {}, "State": {"Running": True},
                                "Config": {"Image": "arca:aio", "Labels": {"systems.lucya.aio": "test-",
                                                                          "systems.lucya.aio.kind": "arca"}}}
    stack.boot(TOKEN, "X")
    arca = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "arca")
    assert arca["state"] == "absent"
    assert "other" not in stack.containers()
    assert "lucya-images/arca:current" not in fake.images
    assert api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"}).status_code == 202
    assert fake.containers["other"]["State"]["Running"]           # untouched
    assert tool(fake, "arca")["body"]["Labels"]["systems.lucya.aio"] == "lucya-"


# ---- restoring ----------------------------------------------------------------------------------
@pytest.fixture()
def with_self(fake):
    from aio.config import settings
    fake.containers["self"] = {"Id": "self", "Name": "/lucya-aio", "Image": "x", "body": {},
                               "State": {"Running": True}, "Config": {"Image": "x", "Labels": {}},
                               "Mounts": [{"Type": "volume", "Name": "lucya-backups", "Source": "/var/lib/x",
                                           "Destination": "/backups"}]}
    settings.self_id = "self"
    return fake


def _a_backup(api, stack):
    api.post("/backup")
    return stack.backups.list()[0]["name"]


def test_restore_backs_up_first_then_unpacks_into_the_stopped_tool(with_self, stack, api):
    fake = with_self
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    run = _a_backup(api, stack)
    arca = tool(fake, "arca")
    r = api.post(f"/backups/{run}/restore", json={"kinds": ["arca"]})
    assert r.status_code == 202 and r.json() == {"kinds": ["arca"]}
    runs = stack.backups.list()
    assert runs[0]["why"] == f"vor dem Wiederherstellen von {run}" and runs[0]["kinds"] == ["arca"]
    one = fake.oneoffs[-1]
    hc = one["HostConfig"]
    assert hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"]
    assert {"Type": "volume", "Source": "lucya-arca-data", "Target": "/data"} in hc["Mounts"]
    assert {"Type": "volume", "Source": "lucya-backups", "Target": "/backups", "ReadOnly": True} in hc["Mounts"]
    assert one["Cmd"][-2:] == [f"/backups/{run}/arca.tar.gz", "/data"] and "extractall" in one["Cmd"][1]
    assert one["Image"] == "lucya-images/arca:current"
    assert tool(fake, "arca")["Id"] == arca["Id"] and tool(fake, "arca")["State"]["Running"]
    assert not [c for c in fake.containers.values() if c["Name"] == "/lucya-arca-restore"]
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "arca")["job"]
    assert job["step"] == "ready" and job["kind"] == "restore"


def test_restore_takes_lucent_last(with_self, stack, api):
    fake = with_self
    stack.boot(TOKEN, "X")
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    run = _a_backup(api, stack)
    assert api.post(f"/backups/{run}/restore", json={"kinds": ["lucent", "fila"]}).json() == {"kinds": ["fila", "lucent"]}
    assert [o["Labels"]["systems.lucya.aio.oneoff"] for o in fake.oneoffs[-2:]] == ["restore-fila", "restore-lucent"]


@pytest.mark.parametrize("run, kinds, word", [
    ("../../etc", ["arca"], "nichts"), ("2026-11-12_030000", ["arca"], "nichts"),
    (None, [], "Wähle"), (None, ["root"], "Unbekannt"), (None, "arca", "Wähle")])
def test_restore_refuses_what_is_not_there(with_self, stack, api, run, kinds, word):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    real = _a_backup(api, stack)
    r = api.post(f"/backups/{run or real}/restore", json={"kinds": kinds})
    assert r.status_code in (404, 422) and (r.status_code == 404 or word in r.json()["detail"])
    assert not [o for o in with_self.oneoffs if "restore" in o["Labels"].get("systems.lucya.aio.oneoff", "")]


def test_a_failed_unpack_starts_the_tool_again_and_says_why(with_self, stack, api):
    fake = with_self
    api.put("/tools/strata", json={"port": 8086, "ui_url": "http://x.test"})
    run = _a_backup(api, stack)
    fake.oneoff_code = 1
    api.post(f"/backups/{run}/restore", json={"kinds": ["strata"]})
    job = next(t for t in api.get("/state").json()["tools"] if t["kind"] == "strata")["job"]
    assert job["step"] == "error" and "Entpacken" in job["error"]
    assert tool(fake, "strata")["State"]["Running"]


def test_restore_needs_the_tool_to_be_there(with_self, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    run = _a_backup(api, stack)
    api.delete("/tools/arca")
    r = api.post(f"/backups/{run}/restore", json={"kinds": ["arca"]})
    assert r.status_code == 422 and "hinzufügen" in r.json()["detail"]


# ---- what a job says while it runs and when it is over ------------------------------------------
def _job(api, kind):
    st = api.get("/state").json()
    return st["lucent"]["job"] if kind == "lucent" else next(t for t in st["tools"] if t["kind"] == kind)["job"]


def test_an_update_says_how_it_went(fake, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    old = tool(fake, "arca")["Image"]
    fake.images["arca:aio"] = "sha256:" + "f" * 64
    api.post("/tools/arca/update")
    r = _job(api, "arca")["result"]
    assert r["outcome"] == "updated" and r["from"] == old[7:19] and r["to"] == "f" * 12
    assert r["backup"] == stack.backups.list()[0]["name"] and r["finished"]


def test_a_rollback_and_a_refusal_say_so(fake, stack, api):
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    fake.images["fila:aio"] = "sha256:" + "b" * 64
    fake.start_error, fake.bad_image = "exec format error", "sha256:" + "b" * 64
    api.post("/tools/fila/update")
    assert _job(api, "fila")["result"]["outcome"] == "rolled_back"
    api.put("/tools/strata", json={"port": 8086, "ui_url": "http://x.test"})
    del fake.images["lucya-images/strata:current"]
    fake.images["strata:aio"] = "sha256:" + "e" * 64
    api.post("/tools/strata/update")
    r = _job(api, "strata")["result"]
    assert r["outcome"] == "failed" and r["untouched"] is True


def test_update_all_shows_the_queue(fake, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    api.put("/tools/fila", json={"port": 8085, "ui_url": "http://x.test"})
    fake.images["arca:aio"] = "sha256:" + "1" * 64
    fake.images["fila:aio"] = "sha256:" + "2" * 64
    held = []
    stack._spawn = lambda fn, *args: held.append((fn, args))      # not yet: look at the queue first
    api.post("/updates/all")
    assert {_job(api, k)["step"] for k in ("arca", "fila")} == {"queued"}
    fn, args = held[0]
    fn(*args)
    assert {_job(api, k)["result"]["outcome"] for k in ("arca", "fila")} == {"updated"}


def test_a_restore_says_which_backup_came_before(with_self, stack, api):
    api.put("/tools/arca", json={"port": 8070, "ui_url": "http://x.test"})
    run = _a_backup(api, stack)
    api.post(f"/backups/{run}/restore", json={"kinds": ["arca"]})
    r = _job(api, "arca")["result"]
    assert r["outcome"] == "restored" and r["run"] == run and r["backup"] == stack.backups.list()[0]["name"]
