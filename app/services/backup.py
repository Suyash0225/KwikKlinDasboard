"""Nightly Postgres backup via pg_dump, with simple retention.

Runs from the scheduler's nightly tick. Dumps go to <repo>/backups/ in
pg_dump custom format (-Fc) — restore with:
    pg_restore -h localhost -U laundry -d laundry --clean backups/<file>.dump
"""

import asyncio
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import structlog

from app.config import settings

log = structlog.get_logger()

BACKUP_DIR = Path(__file__).resolve().parent.parent.parent / "backups"
RETENTION = 14  # keep this many most-recent dumps


def _tool(name: str) -> str | None:
    """pg_dump / pg_restore kahan hai.

    Pehle sirf settings.PG_DUMP_PATH dekha jaata tha, jiska default ek
    Windows path hai (C:\\...\\PostgreSQL\\15\\bin). Linux/AWS par deploy
    karte hi wo file nahi milti, run_backup() chupchaap None de deta hai,
    aur roz raat "backup" ke naam par kuch nahi hota — sirf ek log line.
    Isliye ab: PATH pehle (Linux/Docker/AWS), phir configured path aur
    uska bhai-bandhu (pg_dump ke bagal mein pg_restore), warna None.
    """
    configured = Path(settings.PG_DUMP_PATH)
    candidates = []
    if name == "pg_dump":
        candidates.append(configured)
    else:
        # pg_restore usi bin/ folder se jahan pg_dump hai — version match
        candidates += [
            configured.parent / "pg_restore.exe",
            configured.parent / "pg_restore",
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(name)
    return found


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
    exe = _tool("pg_dump")
    if exe is None:
        log.error("backup_pg_dump_missing", configured=settings.PG_DUMP_PATH)
        await _tell_the_owner(
            "🚨 Backup NAHI hua — pg_dump nahi mila.\n"
            f"Configured: {settings.PG_DUMP_PATH}\n"
            "PG_DUMP_PATH .env mein theek karein, warna roz raat ka backup band hai."
        )
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
        detail = err.decode(errors="replace")[:500]
        log.error("backup_failed", code=proc.returncode, error=detail)
        await _tell_the_owner(
            "🚨 Aaj raat ka backup FAIL hua.\n"
            f"{detail[:300]}\n"
            "Jab tak theek na ho, data ki koi nayi copy nahi ban rahi."
        )
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
    if not verified:
        # File ban to gayi, par khul nahi rahi — ye "backup hai" se bhi bura
        # hai, kyunki bharosa jhootha ho jaata hai.
        await _tell_the_owner(
            f"⚠️ Backup bana ({out.name}) par verify nahi hua — file adhoori "
            "ya kharab ho sakti hai. Disk space aur logs dekhein."
        )
    return str(out)


async def _tell_the_owner(text: str) -> None:
    """Backup ki khabar owner ko WhatsApp par.

    Pehle fail hone par sirf log line jaati thi. Log koi roz nahi padhta —
    mahine bhar backup band reh sakta tha aur pata disk kharab hone ke din
    chalta. Baaki har zaroori baat WhatsApp par jaati hai; ye bhi jaaye.
    Kabhi raise nahi karta: khabar na ja paane se backup ka kaam na ruke.
    """
    try:
        from app.database import async_session_factory
        from app.services.team import notify_admins

        async with async_session_factory() as db:
            await notify_admins(db, text)
    except Exception:
        log.exception("backup_alert_failed")


async def verify_backup(path: str) -> bool:
    """Dump ka restore-test (pg_restore --list): archive ka TOC poora padha
    ja sakta hai ya nahi. Ye corruption/truncation turant pakadta hai bina
    scratch DB ke. (Poora restore drill: scripts/restore_drill.ps1 —
    mahine mein ek baar haath se chalao.)"""
    exe = _tool("pg_restore")
    if exe is None:
        log.error("backup_verify_no_pg_restore")
        return False
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
