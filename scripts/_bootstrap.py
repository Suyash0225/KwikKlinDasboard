"""Har seed/maintenance script ka pehla kadam: home tenant ka cache bhar do.

Kyun ye zaroori hai (ek asli bug se seekha):

Naye rows par `tenant_id` app/database.py ka `before_flush` event lagata
hai. Wo event SYNC hai, isliye await nahi kar sakta — home tenant ka id wo
sirf CACHE se padhta hai (`tenant_context.cached_home_tenant_id()`). Cache
app startup par bharta hai (main.py lifespan) ya har HTTP request par.

Script mein na startup hota hai na request. Cache khali rehta hai, event
ek warning likh kar chhod deta hai, aur row `tenant_id = NULL` ke saath DB
mein chali jaati hai. Us row par RLS ka predicate (`tenant_id = <uuid>`)
kabhi TRUE nahi hota — yaani `seed_staff` se banaya staff dukaan ko dikhta
hi nahi: staff panel login fail, control panel "0 staff", team.py ko koi
admin nahi milta. Sab kuch "chal gaya" jaisa lagta hai, kyunki script ne
"staff_row" log kar diya tha.

Isliye: `await prime()` pehle, phir kuch bhi likho.
"""

import structlog

log = structlog.get_logger()


async def prime() -> None:
    """Home tenant resolve karke cache mein daalo. Na mile to saaf mana."""
    from app.services import tenant_context

    tid = await tenant_context.get_home_tenant_id()
    if tid is None:
        raise SystemExit(
            "Koi home tenant nahi mila — pehle ye chalao:\n"
            "    python -m scripts.bootstrap_home_tenant <email> <password>\n"
            "(bina iske naye rows tenant_id=NULL ke saath likhenge aur "
            "dukaan ko dikhenge hi nahi.)"
        )
    log.info("script_tenant_primed", tenant_id=str(tid))
