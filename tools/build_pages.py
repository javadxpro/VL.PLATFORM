#!/usr/bin/env python3
"""
Build the GitHub Pages bundle: the real UI, driven by a local demo backend.

    python3 tools/build_pages.py [--out build/pages] [--no-fonts] [--pretty]

Why a build step instead of a second frontend: `index.html` is the product, and a
copy of it starts lying within a week. Everything Pages needs that the LAN server
gives for free is handled here by rewriting *references*, never behaviour:

  * the socket.io client is dropped (there is no server to talk to) and
    `pages/vl-demo.js` defines a no-op `io()` so the presence code still runs;
  * `/static/...` becomes `static/...`, because a Pages site lives under
    `/<repo>/` and a root-absolute path would escape it;
  * `pages/vl-demo.js` is loaded in <head>, before the app script, so its fetch
    shim is installed before the first request.

The result is self-contained: no network call leaves the page except for images a
user picks during that same visit (blob: URLs, which never leave the browser).

Stdlib only, like the rest of the tooling.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOCKETIO_TAG = '<script src="/static/js/socket.io.min.js"></script>'
DEMO_SCRIPT = '<script src="vl-demo.js"></script>'
# a public demo of an app that is not licensed for use; keep it out of search
NOINDEX = '<meta name="robots" content="noindex, nofollow">'
HEAD_CLOSE = "</head>"
FONT_GLOB = "Vazirmatn-*.woff2"


class BuildError(RuntimeError):
    pass


def transform(html: str) -> str:
    """The whole UI rewrite. Deliberately tiny: three string operations."""
    if "<script" not in html:
        raise BuildError("index.html has no <script> — is this the SPA?")
    if HEAD_CLOSE not in html:
        raise BuildError("index.html has no </head> to inject before")
    if SOCKETIO_TAG not in html:
        raise BuildError("socket.io script tag not found — the build script is stale, "
                         "not the app: update SOCKETIO_TAG in tools/build_pages.py")
    out = html.replace(SOCKETIO_TAG, "")
    out = out.replace('src="/static/', 'src="static/').replace("url('/static/", "url('static/")
    if "/static/" in out:
        raise BuildError(f"a /static/ reference is left in the output ({out.count('/static/')} "
                         "of them); the UI gained an absolute asset path Pages cannot serve")
    return out.replace(HEAD_CLOSE, f"{NOINDEX}\n{DEMO_SCRIPT}\n{HEAD_CLOSE}", 1)


def build(out_dir: Path, *, fonts: bool = True, quiet: bool = False) -> dict:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    demo = (ROOT / "pages" / "vl-demo.js").read_text(encoding="utf-8")
    if "window.fetch" not in demo and "self.fetch" not in demo:
        raise BuildError("pages/vl-demo.js does not replace fetch — it would call a server that isn't there")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    built = transform(html)
    (out_dir / "index.html").write_text(built, encoding="utf-8")
    # a deep link on Pages is a 404 unless the same page answers it; this app has
    # exactly one page, so the copy is the whole fix
    (out_dir / "404.html").write_text(built, encoding="utf-8")
    (out_dir / "vl-demo.js").write_text(demo, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    copied = []
    if fonts:
        target = out_dir / "static" / "fonts"
        target.mkdir(parents=True)
        for font in sorted((ROOT / "static" / "fonts").glob(FONT_GLOB)):
            shutil.copy2(font, target / font.name)
            copied.append(font.name)

    total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    stats = {"files": len(list(out_dir.rglob("*"))), "fonts": copied, "bytes": total,
              "html_lines": built.count("\n")}
    if not quiet:
        print(f"built {out_dir} · {stats['html_lines']} lines of html · "
              f"{len(copied)} fonts · {total / 1024:.0f} KiB total")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--out", default="build/pages", help="output directory (default: build/pages)")
    ap.add_argument("--no-fonts", action="store_true", help="skip the Vazirmatn copies (smaller, ugly)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        build(ROOT / args.out, fonts=not args.no_fonts, quiet=args.quiet)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
