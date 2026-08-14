"""SPA static asset serving for the compiled React app (arc42 §7 deployment view).

The production container builds `web/` (React 18 + Vite) into `web/dist` and
this module mounts the compiled output onto the same FastAPI app that serves
`/api/*` and `/healthz` — a single production container, no separate static
host. Mounting is best-effort: if `web/dist` was not built (local dev running
straight from source, most unit tests), :func:`mount_spa` is a no-op and the
API-only surface behaves exactly as before.

Routing precedence (arc42 F-16 / §7 ingress):
- `/healthz` and `/api/*` are registered on ``app`` before this module is
  invoked, so they always match first — this module never intercepts them,
  and additionally refuses to fall back to the SPA for unmatched paths under
  those prefixes (they 404 instead of silently returning `index.html`).
- `/assets/*` (Vite's content-hashed JS/CSS bundle) is served directly from
  disk via :class:`~fastapi.staticfiles.StaticFiles` so hashed filenames get
  long-lived caching and correct content types/etags.
- Any other GET path either maps to a real file under `web/dist` (e.g.
  `/favicon.svg`) or falls back to `index.html` so client-side routing
  (React Router-style deep links, browser refresh) keeps working.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_RESERVED_PREFIXES = ("api", "auth-config.json", "healthz")


def _default_dist_dir() -> Path:
    """Resolve the compiled SPA directory.

    ``AUSPEX_WEB_DIST_DIR`` is set explicitly in the production Dockerfile
    (mirroring ``AUSPEX_CONFIG_DIR``/``AUSPEX_PROMPTS_DIR``) to the `web/dist`
    copied into the runtime image. Without it (local/dev checkouts running
    `auspex serve` from source), fall back to the repo-relative `web/dist`
    path so a locally-built SPA is picked up automatically.
    """

    env_dir = os.environ.get("AUSPEX_WEB_DIST_DIR")
    if env_dir:
        return Path(env_dir)
    # src/auspex/api/static.py -> parents[3] is the repository root.
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def _is_reserved(full_path: str) -> bool:
    first_segment = full_path.split("/", 1)[0]
    return first_segment in _RESERVED_PREFIXES


def mount_spa(app: FastAPI, dist_dir: Path | None = None) -> bool:
    """Mount the compiled SPA onto ``app``. Returns ``True`` if mounted.

    No-op (returns ``False``) when ``dist_dir`` (or its default) does not
    exist, so environments without a built `web/dist` — most notably the
    existing unit test suite — are unaffected.
    """

    resolved_dist_dir = (dist_dir or _default_dist_dir()).resolve()
    index_file = resolved_dist_dir / "index.html"
    if not resolved_dist_dir.is_dir() or not index_file.is_file():
        return False

    assets_dir = resolved_dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        if _is_reserved(full_path):
            raise HTTPException(status_code=404)

        if full_path:
            candidate = (resolved_dist_dir / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(resolved_dist_dir):
                return FileResponse(candidate)

        return FileResponse(index_file)

    return True
