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
TEMPLATES: dict[str, dict] = {
    # Meta's built-in sample template on every test number. Zero params.
    "hello_world": {"language": "en_US", "param_count": 0},
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
