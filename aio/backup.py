# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright © 2026 lucya.systems — https://lucya.systems
"""Backups: one folder per run under /backups, one tar.gz per container.

The copy is made inside each container, as its own user (only it may read
its keys): every SQLite file anywhere in /data, known by its header, not
its name (Aperture keeps one as console/lucent_outbox.sqlite), is copied
with VACUUM INTO, so a database in WAL mode comes out whole while the tool
runs, and the archive goes to stdout, which the helper writes to disk. The
archive holds /data as it should be restored: the copies under their own
names, everything else as it is, none of their -wal, -shm or -journal.

/backups is whatever was mounted there with `docker run` (a named volume
or a host folder). Without it there are no backups, and the state says so.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from .config import settings

log = logging.getLogger("aio")

# runs inside a tool's container: python -c SNAPSHOT /data
SNAPSHOT = r"""
import os, shutil, sqlite3, sys, tarfile
src = sys.argv[1]
tmp = os.path.join(src, ".lucya-backup")
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp)
def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False
copies, beside = {}, set()
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if os.path.join(root, d) != tmp]
    for f in files:
        full = os.path.join(root, f)
        if not is_sqlite(full):
            continue
        rel = os.path.relpath(full, src)
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        c = sqlite3.connect(full)
        c.execute("VACUUM INTO ?", (dst,))
        c.close()
        copies[rel] = dst
        beside |= {rel + s for s in ("-wal", "-shm", "-journal")}
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if os.path.join(root, d) != tmp]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            if rel in copies or rel in beside:
                continue
            tar.add(full, arcname=rel)
    for rel, dst in copies.items():
        tar.add(dst, arcname=rel)
shutil.rmtree(tmp, ignore_errors=True)
print(" ".join(sorted(copies)) or "-", file=sys.stderr)
"""

def snapshot_argv() -> list[str]:
    return ["python", "-c", SNAPSHOT, "/data"]


class Backups:
    def __init__(self, docker, root: Path | None = None):
        self.docker, self.root = docker, root or settings.backups
        self._lock = threading.Lock()
        self.running: str | None = None          # what is being backed up
        self.last_error: str | None = None

    # ---- where, and whether ----------------------------------------------------
    def available(self) -> bool:
        return self.root.is_dir()

    def _schedule_path(self) -> Path:
        return settings.data_dir / "schedule.json"

    def schedule(self) -> dict:
        try:
            return json.loads(self._schedule_path().read_text())
        except (OSError, ValueError):
            return {"enabled": False, "time": "03:30", "keep": 14, "last_day": None}

    def set_schedule(self, enabled: bool, at: str, keep: int) -> dict:
        s = self.schedule()
        s.update(enabled=enabled, time=at, keep=keep)
        self._schedule_path().write_text(json.dumps(s))
        return s

    def list(self) -> list[dict]:
        if not self.available():
            return []
        out = []
        for d in sorted((p for p in self.root.iterdir() if p.is_dir()), reverse=True):
            meta = {}
            try:
                meta = json.loads((d / "backup.json").read_text())
            except (OSError, ValueError):
                pass
            files = [f for f in d.iterdir() if f.suffix == ".gz"]
            out.append({"name": d.name, "at": meta.get("at", d.name), "ok": meta.get("ok", False),
                        "kinds": sorted(meta.get("done", [])), "failed": meta.get("failed", {}),
                        "size": sum(f.stat().st_size for f in files), "why": meta.get("why", "")})
        return out

    # ---- making one ------------------------------------------------------------------
    def run(self, containers: dict[str, str], why: str) -> dict:
        """Back up {kind: container id}. One run at a time."""
        if not self.available():
            raise RuntimeError("Keine Ablage für Sicherungen: /backups ist nicht eingehängt.")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Es läuft schon eine Sicherung.")
        try:
            base = stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            n = 1
            while (self.root / stamp).exists():         # two runs in one second
                n += 1
                stamp = f"{base}-{n}"
            folder = self.root / stamp
            folder.mkdir(parents=True)
            meta = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "why": why,
                    "done": [], "failed": {}}
            for kind, cid in containers.items():
                self.running = kind
                target = folder / f"{kind}.tar.gz"
                try:
                    code, err = self.docker.exec_to_file(cid, snapshot_argv(), target)
                    if code != 0:
                        raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"Code {code}")
                    meta["done"].append(kind)
                except Exception as e:
                    target.unlink(missing_ok=True)
                    meta["failed"][kind] = str(e)
                    log.warning("Sicherung %s: %s", kind, e)
            meta["ok"] = not meta["failed"]
            (folder / "backup.json").write_text(json.dumps(meta))
            self.last_error = None if meta["ok"] else "; ".join(f"{k}: {v}" for k, v in meta["failed"].items())
            self.prune()
            return {"name": stamp, **meta}
        finally:
            self.running = None
            self._lock.release()

    def prune(self) -> None:
        keep = max(1, int(self.schedule().get("keep", 14)))
        runs = sorted((p for p in self.root.iterdir() if p.is_dir()), reverse=True)
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    # ---- the daily run ------------------------------------------------------------------
    def due(self, now: datetime) -> bool:
        s = self.schedule()
        if not s.get("enabled") or not self.available():
            return False
        hh, mm = (int(x) for x in s.get("time", "03:30").split(":"))
        return now.strftime("%Y-%m-%d") != s.get("last_day") and (now.hour, now.minute) >= (hh, mm)

    def mark_done_today(self, now: datetime) -> None:
        s = self.schedule()
        s["last_day"] = now.strftime("%Y-%m-%d")
        self._schedule_path().write_text(json.dumps(s))
