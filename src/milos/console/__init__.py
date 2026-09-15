"""The web console: a static Next.js export served by the public API.

The frontend lives in `console/` at the repository root and is built into
`static/` here (gitignored; the Docker image builds it in a Node stage). The
page calls `/v1` on its own origin, so the IAP cookie that admitted the browser
is the identity for every request and nothing here touches authorization.

A missing bundle is not an error: the API serves without a console, as a fresh
checkout does until `make console` runs.
"""

import mimetypes
import sys
from collections.abc import Mapping
from importlib import resources
from importlib.resources.abc import Traversable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

IMMUTABLE = "public, max-age=31536000, immutable"  # next content-hashes everything under _next/
NO_CACHE = "no-cache"
API_PREFIXES = ("v1/", "internal/", "health")


def load_static() -> dict[str, bytes]:
    """Read the whole bundle into memory once: it is small, and the container filesystem is read-only anyway."""
    root = resources.files(__name__).joinpath("static")
    if not root.is_dir():
        print("console frontend not built; run `make console` (the API serves without it)", file=sys.stderr)
        return {}
    files: dict[str, bytes] = {}
    _walk(root, "", files)
    return files


def _walk(node: Traversable, prefix: str, files: dict[str, bytes]) -> None:
    for child in node.iterdir():
        if child.is_dir():
            _walk(child, f"{prefix}{child.name}/", files)
        else:
            files[f"{prefix}{child.name}"] = child.read_bytes()


def mount(app: FastAPI, static: Mapping[str, bytes]) -> None:
    """Serve the bundle from a catch-all GET. Register after the API routers so `/v1` always wins."""

    @app.get("/{path:path}", include_in_schema=False)
    async def page(path: str) -> Response:
        if path.startswith(API_PREFIXES):
            return JSONResponse({"error": "NotFound", "detail": f"no route for /{path}"}, 404)
        name = path or "index.html"
        body = static.get(name)
        if body is None and "/" not in name:
            name = f"{name}.html"  # the export is flat: /sessions is sessions.html
            body = static.get(name)
        if body is not None:
            cache = IMMUTABLE if "/" in name else NO_CACHE
            return Response(body, media_type=_media_type(name), headers={"Cache-Control": cache})
        if (missing := static.get("404.html")) is not None:
            return Response(missing, 404, media_type="text/html", headers={"Cache-Control": NO_CACHE})
        return JSONResponse({"error": "NotFound", "detail": f"no such page /{path}"}, 404)


def _media_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"
