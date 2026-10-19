async def join_page(request: Request):
    """Serve the public signup page with a stable canonical URL.

    SITE_URL controls the preferred host, preventing www/non-www copies of
    the same public signup page from competing in search indexing.
    """
    import html as _html
    from fastapi.responses import Response as _Resp

    html = _JOIN_FILE.read_text(encoding="utf-8")
    canonical = _html.escape(f"{_public_base(request)}/join", quote=True)
    seo = (
        f'<link rel="canonical" href="{canonical}">\n'
        '<meta name="robots" content="index, follow">\n'
    )
    html = html.replace("</head>", seo + "</head>", 1)
    return _Resp(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )