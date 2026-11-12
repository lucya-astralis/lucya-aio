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
