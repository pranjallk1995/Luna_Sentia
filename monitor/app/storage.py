"""How much disk each Luna app takes: its database on the shared PostgreSQL server, and its Docker volumes.

A database holds only an app's records (captions, tags, embeddings, its own settings); the images, documents,
tracks and models themselves are files on the app's volumes, so both are measured. The databases are measured
when asked (the monitor's role is in pg_read_all_stats, granted by postgres/init-databases.sh, so it can size the
others' without reading them). The volumes are measured every VOLUME_SECONDS in the background: the host's
/var/lib/docker/volumes is mounted read-only at MONITOR_VOLUMES_DIR, and only file sizes are read, never contents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VOLUME_SECONDS = 600
# Databases and volumes that are Sentia's own: the shared server's, its sign-on's and its monitor's.
SENTIA_DATABASES = {"sentia", "postgres", "lunaauth", "lunamonitor"}
# The volume that holds every app's database: its size is the databases', so app totals leave it out.
DATABASES_VOLUME = "luna_sentia_postgres_data"


def human(size: int | None) -> str | None:
    if size is None:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def app_of_database(name: str) -> str:
    if name in SENTIA_DATABASES:
        return "sentia"
    return name.removeprefix("luna") if name.startswith("luna") else "other"


def app_of_volume(name: str) -> str:
    parts = name.split("_")
    return parts[1] if len(parts) > 2 and parts[0] == "luna" else "other"


def directory_bytes(path: Path) -> int:
    """The disk a folder takes: its files' allocated blocks, each hard-linked file once, symlinks not followed."""
    total, seen = 0, set()
    for root, folders, files in os.walk(path):
        for name in files + folders:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if info.st_nlink > 1:
                if (info.st_dev, info.st_ino) in seen:
                    continue
                seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
    return total


class Storage:
    def __init__(self, store: Any, volumes_dir: str = "", clock=time.time) -> None:
        self.store = store
        self.volumes_dir = Path(volumes_dir) if volumes_dir else None
        self.clock = clock
        self.volumes: dict[str, int] | None = None
        self.volumes_at: float | None = None
        self.volumes_problem: str | None = None if self.volumes_dir else "the volumes folder is not mounted (MONITOR_VOLUMES_DIR)"

    def measure_volumes(self) -> None:
        if self.volumes_dir is None:
            return
        try:
            entries = sorted(entry for entry in self.volumes_dir.iterdir() if (entry / "_data").is_dir())
        except OSError as error:
            self.volumes_problem = f"the volumes folder could not be read: {error}"
            return
        if not entries:
            self.volumes_problem = "no Docker volumes found in the mounted folder"
            return
        self.volumes = {entry.name: directory_bytes(entry / "_data") for entry in entries}
        self.volumes_at, self.volumes_problem = self.clock(), None

    async def watch(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.measure_volumes)
            except Exception as error:  # noqa: BLE001 - reported by the tool
                logger.warning("Could not measure the volumes: %s", error)
                self.volumes_problem = str(error)
            await asyncio.sleep(VOLUME_SECONDS)

    def report(self, databases: list[dict[str, Any]] | None, databases_problem: str | None = None) -> dict[str, Any]:
        """Per app: its database and volumes and their total, largest first; and the disk all volumes take."""
        apps: dict[str, dict[str, Any]] = {}

        def entry(app: str) -> dict[str, Any]:
            return apps.setdefault(app, {"app": app, "databases": [], "volumes": [], "total_bytes": 0})

        for row in databases or []:
            item = entry(app_of_database(row["name"]))
            item["databases"].append({"name": row["name"], "bytes": row["bytes"], "size": human(row["bytes"])})
            item["total_bytes"] += row["bytes"] or 0
        for name, size in (self.volumes or {}).items():
            item = entry(app_of_volume(name))
            volume = {"name": name, "bytes": size, "size": human(size)}
            if name == DATABASES_VOLUME:
                volume["note"] = "holds every app's database above; not counted again in the totals"
            else:
                item["total_bytes"] += size
            item["volumes"].append(volume)
        ordered = sorted(apps.values(), key=lambda item: -item["total_bytes"])
        for item in ordered:
            item["total"] = human(item["total_bytes"])
        volumes_total = sum((self.volumes or {}).values()) if self.volumes else None
        notes = [problem for problem in (databases_problem and f"Databases: {databases_problem}",
                                         self.volumes_problem and f"Volumes: {self.volumes_problem}") if problem]
        return {
            "apps": ordered,
            "databases_bytes": sum(row["bytes"] or 0 for row in databases or []) if databases else None,
            "databases_size": human(sum(row["bytes"] or 0 for row in databases or [])) if databases else None,
            "volumes_bytes": volumes_total, "volumes_size": human(volumes_total),
            "volumes_measured_at": self.volumes_at,
            "notes": notes,
        }
