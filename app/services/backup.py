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
        # RLS is FORCED on tenant tables (data isolation); without this flag
        # pg_dump refuses to run. The backup session has no app.tenant_id
        # set, so the policies' system-context arm exposes every row and the
        # dump stays complete.
        "--enable-row-security",
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

    # Restore-test: jo backup restore nahi ho sakta wo backup hai hi nahi.
    verified = await verify_backup(str(out))
    from app.services import audit

    await audit.record(
        actor_role="system", actor="nightly-backup",
        action="backup_ok" if verified else "backup_unverified",
        args={"file": out.name, "bytes": out.stat().st_size, "verified": verified},
        ok=verified,
    )
    log.info("backup_ok", file=out.name, bytes=out.stat().st_size, verified=verified)
    return str(out)


async def verify_backup(path: str) -> bool:
    """Dump ka restore-test (pg_restore --list): archive ka TOC poora padha
    ja sakta hai ya nahi. Ye corruption/truncation turant pakadta hai bina
    scratch DB ke. (Poora restore drill: scripts/restore_drill.ps1 —
    mahine mein ek baar haath se chalao.)"""
    exe = str(Path(settings.PG_DUMP_PATH).parent / "pg_restore.exe")
    if not Path(exe).exists():
        exe = "pg_restore"
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--list", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out_b, err = await proc.communicate()
        ok = proc.returncode == 0 and b"TABLE DATA" in out_b
        if not ok:
            log.error(
                "backup_verify_failed", file=path, code=proc.returncode,
                error=err.decode(errors="replace")[:300],
            )
        return ok
    except Exception:
        log.exception("backup_verify_crashed", file=path)
        return False
