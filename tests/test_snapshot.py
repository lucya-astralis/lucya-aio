# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""The backup script as it runs in a container, here on a real folder:
every SQLite file, wherever and however named, comes out whole, also what
is still in its WAL; the rest as it is."""
import io
import sqlite3
import subprocess
import sys
import tarfile

from aio.backup import SNAPSHOT


def test_snapshot_takes_every_sqlite_file_whole(tmp_path):
    data = tmp_path / "data"
    (data / "console").mkdir(parents=True)
    keep = []
    for rel in ("gallery.db", "console/lucent_outbox.sqlite"):
        c = sqlite3.connect(data / rel)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")       # leave the rows in the WAL
        c.execute("CREATE TABLE t (x)")
        c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        c.commit()
        keep.append(c)                                   # still open: the WAL stays
    (data / "console" / "credentials").write_text("{}")
    assert (data / "gallery.db-wal").stat().st_size > 0
    out = subprocess.run([sys.executable, "-c", SNAPSHOT, str(data)], capture_output=True, check=True)
    tar = tarfile.open(fileobj=io.BytesIO(out.stdout))
    names = sorted(tar.getnames())
    assert names == ["console/credentials", "console/lucent_outbox.sqlite", "gallery.db"]
    for rel in ("gallery.db", "console/lucent_outbox.sqlite"):
        copy = tmp_path / rel.replace("/", "_")
        copy.write_bytes(tar.extractfile(rel).read())
        assert sqlite3.connect(copy).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    assert not (data / ".lucya-backup").exists()
    assert out.stderr.decode().split() == ["console/lucent_outbox.sqlite", "gallery.db"]


from aio.backup import RESTORE  # noqa: E402


def _run(script, *args):
    return subprocess.run([sys.executable, "-c", script, *map(str, args)], capture_output=True)


def test_restore_puts_the_archive_back_and_nothing_old_stays(tmp_path):
    data = tmp_path / "data"
    (data / "console").mkdir(parents=True)
    c = sqlite3.connect(data / "app.db")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES ('then')")
    c.commit()
    c.close()
    (data / "console" / "key").write_text("then")
    archive = tmp_path / "app.tar.gz"
    archive.write_bytes(_run(SNAPSHOT, data).stdout)
    # what happens after the backup: a change, a new file, a new folder
    c = sqlite3.connect(data / "app.db")
    c.execute("UPDATE t SET x = 'now'")
    c.commit()
    c.close()
    (data / "console" / "key").write_text("now")
    (data / "later.txt").write_text("new")
    (data / "newdir").mkdir()
    r = _run(RESTORE, archive, data)
    assert r.returncode == 0, r.stderr
    assert sqlite3.connect(data / "app.db").execute("SELECT x FROM t").fetchone()[0] == "then"
    assert (data / "console" / "key").read_text() == "then"
    assert not (data / "later.txt").exists() and not (data / "newdir").exists()


def test_restore_refuses_an_archive_that_leaves_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "keep.txt").write_text("still here")
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../outside.txt")
        payload = b"x"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    r = _run(RESTORE, evil, data)
    assert r.returncode != 0 and b"refused" in r.stderr
    assert (data / "keep.txt").read_text() == "still here"          # nothing was cleared
    assert not (tmp_path / "outside.txt").exists()
