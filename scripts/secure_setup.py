"""Server ki teen security kamiyan ek command mein band — dobara chalana safe.

    python -m scripts.secure_setup            # .env isi folder ka
    python -m scripts.secure_setup /path/.env

1. TOKEN_ENCRYPTION_KEY  khali ho to naya Fernet key — WhatsApp/Instagram/
   Google tokens DB mein encrypted. Pehle se ho to KABHI nahi badalta (badla
   to purane encrypted token padhe hi nahi jayenge).
2. VENDOR_API_KEY        khali ya ADMIN_API_KEY jaisa ho to naya — Control
   panel ki master key dukaan ki admin key se alag. Control panel mein ab
   isi nayi key se sign in karna hai (.env mein dekh lo).
3. APP_DATABASE_URL      khali ho to kk_app (NOSUPERUSER) role + naya password
   — RLS sach mein lagti hai.
Phir purane plaintext tokens encrypt (scripts/encrypt_tokens.py).

Koi secret terminal par nahi chhapta — sab seedha .env mein. .env ka backup
rakho: TOKEN_ENCRYPTION_KEY kho gayi to jude hue tokens dobara jodne padenge.
"""

import asyncio
import base64
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path


def _read(env: Path) -> str:
    return env.read_text(encoding="utf-8") if env.exists() else ""


def _get(text: str, key: str) -> str:
    m = re.search(rf"^{key}=(.*)$", text, flags=re.M)
    return m.group(1).strip() if m else ""


def _put(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        return re.sub(rf"^{key}=.*$", lambda _: line, text, count=1, flags=re.M)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def main() -> int:
    env = Path(sys.argv[1] if len(sys.argv) > 1 else ".env").resolve()
    if not env.exists():
        print(f"  {env} nahi mila")
        return 1
    text = _read(env)
    changed = []

    if not _get(text, "TOKEN_ENCRYPTION_KEY"):
        text = _put(text, "TOKEN_ENCRYPTION_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
        changed.append("TOKEN_ENCRYPTION_KEY")
    vendor = _get(text, "VENDOR_API_KEY")
    if not vendor or vendor == _get(text, "ADMIN_API_KEY"):
        text = _put(text, "VENDOR_API_KEY", secrets.token_urlsafe(32))
        changed.append("VENDOR_API_KEY")
    if changed:
        env.write_text(text, encoding="utf-8")
    print("  keys: " + (", ".join(changed) + " naye bane" if changed else "pehle se set"))

    if not _get(text, "APP_DATABASE_URL"):
        from scripts.create_app_role import ensure_role

        url = asyncio.run(ensure_role(reset_password=True))
        if url:
            env.write_text(_put(_read(env), "APP_DATABASE_URL", url), encoding="utf-8")
            print("  APP_DATABASE_URL .env mein likha (kk_app, NOSUPERUSER)")
    else:
        print("  APP_DATABASE_URL pehle se set")

    # Naye process mein — taaki wo .env ki nayi key padhe
    r = subprocess.run([sys.executable, "-m", "scripts.encrypt_tokens"], cwd=env.parent)
    if r.returncode != 0:
        return r.returncode
    if "VENDOR_API_KEY" in changed:
        print("  ⚠ Control panel mein ab .env ki VENDOR_API_KEY se sign in karein")
    print("  ✅ ho gaya — app restart karein")
    return 0


if __name__ == "__main__":
    sys.exit(main())
