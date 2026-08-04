"""Nightly Postgres backup via pg_dump, with simple retention.

Runs from the scheduler's nightly tick. Dumps go to <repo>/backups/ in
pg_dump custom format (-Fc) — restore with:
    pg_restore -h localhost -U laundry -d laundry --clean backups/<file>.dump
"""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

import structlog

from app.config import settings

log = structlog.get_logger()

BACKUP_DIR = Path(__file__).resolve().parent.parent.parent / "backups"
RETENTION = 14  # keep this many most-recent dumps


def _parse_db_url(url: str) -> dict | None:
    m = re.match(
        r".*://(?P<user>[^:@/]+):(?P<pw>[^@/]+)@(?P<host>[^:/]+):(?P<port>\d+)/(?P<db>\w+)",
        url,
    )
    return m.groupdict() if m else None


async def run_backup() -> str | None:
    """Dump the database. Returns the file path, or None on failure (logged)."""
    info = _parse_db_url(settings.DATABASE_URL)
    if info is None:
        log.error("backup_bad_database_url")
        return None
    exe = settings.PG_DUMP_PATH
    if not Path(exe).exists():
        log.error("backup_pg_dump_missing", path=exe)
        return None

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"kwikklin-{stamp}.dump"
    env = {**os.environ, "PGPASSWORD": info["pw"]}
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-h", info["host"], "-p", info["port"], "-U", info["user"],
        "-Fc", "-f", str(out), info["db"],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.error("backup_failed", code=proc.returncode, error=err.decode(errors="replace")[:500])
        return None

    for old in sorted(BACKUP_DIR.glob("kwikklin-*.dump"))[:-RETENTION]:
        try:
            old.unlink()
        except OSError:
            pass
    log.info("backup_ok", file=out.name, bytes=out.stat().st_size)
    return str(out)
