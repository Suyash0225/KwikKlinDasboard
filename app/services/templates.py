"""WhatsApp template registry — the only list of templates we may send.

WhatsApp only delivers PRE-APPROVED templates outside the 24h window. If a
name isn't registered here (and approved in WhatsApp Manager), sending it is
a bug — build_template raises immediately instead of letting Meta reject it.

Adding a template later:
1. Create + get it approved in WhatsApp Manager
2. Add one entry to TEMPLATES below with the same name/language/param count
"""

import structlog

log = structlog.get_logger()

# name -> {language code, number of body parameters}
# The kk_* templates must be created + approved in WhatsApp Manager with
# EXACTLY these names, languages, and {{n}} parameter counts (drafts in
# README/Phase 3 notes). Until Meta approves one, sending it fails with a
# 4xx which notify-code logs and survives.
TEMPLATES: dict[str, dict] = {
    # Meta's built-in sample template on every test number. Zero params.
    "hello_world": {"language": "en_US", "param_count": 0},
    # {{1}} = order number, {{2}} = items count
    "kk_order_confirmed": {"language": "en_US", "param_count": 2},
    # {{1}} = order number
    "kk_order_ready": {"language": "en_US", "param_count": 1},
    "kk_order_out_for_delivery": {"language": "en_US", "param_count": 1},
    "kk_order_delivered": {"language": "en_US", "param_count": 1},
    # {{1}} = order number, {{2}} = new date
    "kk_delay_notice": {"language": "en_US", "param_count": 2},
}


def build_template(name: str, params: list[str] | None = None) -> dict:
    """Build the `template` object for the Graph API send payload.

    Raises ValueError for unknown template or wrong parameter count.
    """
    if name not in TEMPLATES:
        log.error("unknown_template", template=name, known=list(TEMPLATES))
        raise ValueError(f"template {name!r} is not registered in templates.py")

    spec = TEMPLATES[name]
    params = params or []
    if len(params) != spec["param_count"]:
        raise ValueError(
            f"template {name!r} needs {spec['param_count']} params, got {len(params)}"
        )

    payload: dict = {"name": name, "language": {"code": spec["language"]}}
    if params:
        payload["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }
        ]
    return payload
