"""Production build step: minify app.js + app.css into app/static/dist/.

The server already gzips (GZipMiddleware) and sends immutable cache headers,
so this is the last few percent — run it as part of a deploy if you want it:

    .venv\\Scripts\\python.exe -m scripts.minify_static

Output goes to app/static/dist/ (app.js, app.css — same names). To serve the
minified files, point your deploy at dist/ (copy them over the originals in
the DEPLOY ARTIFACT, never in the repo — the repo keeps readable sources).
The dashboard route versions assets by file mtime, so swapped files bust
caches automatically. No framework, no node — two pure-Python minifiers.
"""

from pathlib import Path

import rcssmin
import rjsmin

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
DIST = STATIC / "dist"


def main() -> None:
    DIST.mkdir(exist_ok=True)
    for name, minify in (("app.js", rjsmin.jsmin), ("app.css", rcssmin.cssmin)):
        src = (STATIC / name).read_text(encoding="utf-8")
        out = minify(src)
        (DIST / name).write_text(out, encoding="utf-8")
        print(f"  {name}: {len(src) / 1024:.1f} KB -> {len(out) / 1024:.1f} KB")
    print(f"\n  Done: {DIST}")


if __name__ == "__main__":
    main()
