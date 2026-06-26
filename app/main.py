"""FastAPI-приложение: API + статичный дашборд + фоновый планировщик."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .cache import make_cache
from .config import CONFIG
from .worker import refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")

cache = make_cache(CONFIG["cache"])
scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_: FastAPI):
    wcfg = CONFIG["worker"]
    if wcfg.get("warm_on_start", True) and cache.read() is None:
        try:
            refresh(CONFIG, cache)
        except Exception as e:  # noqa: BLE001
            log.error("warm-up refresh failed: %s", e)

    scheduler.add_job(
        lambda: refresh(CONFIG, cache),
        "interval",
        minutes=wcfg.get("refresh_interval_minutes", 360),
        id="refresh",
    )
    scheduler.start()
    log.info("scheduler started: every %s min", wcfg.get("refresh_interval_minutes", 360))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="LLM Value Tracker", lifespan=lifespan)


def _snapshot() -> dict:
    snap = cache.read()
    if snap is None:
        raise HTTPException(503, "No snapshot yet — data is still being fetched.")
    return snap


@app.get("/api/models")
def get_models(
    category: str = Query("overall"),
    preset: str | None = Query(None),
    top: int = Query(0, ge=0),
):
    snap = _snapshot()
    if category not in snap["categories"]:
        raise HTTPException(404, f"Unknown category: {category}")
    preset = preset or snap["default_preset"]
    if preset not in snap["presets"]:
        raise HTTPException(404, f"Unknown preset: {preset}")

    rows = sorted(
        snap["categories"][category],
        key=lambda r: (r["value"].get(preset) is None, -(r["value"].get(preset) or 0)),
    )
    if top > 0:
        rows = rows[:top]
    return {
        "category": category,
        "preset": preset,
        "updated_at": snap["updated_at"],
        "count": len(rows),
        "models": rows,
    }


@app.get("/api/health")
def health():
    snap = cache.read()
    if snap is None:
        return JSONResponse({"ready": False}, status_code=503)
    return {
        "ready": True,
        "updated_at": snap["updated_at"],
        "status": snap["status"],
        "unmatched_count": len(snap["unmatched"]),
    }


@app.get("/api/meta")
def meta():
    snap = _snapshot()
    return {
        "categories": list(snap["categories"].keys()),
        "presets": snap["presets"],
        "default_preset": snap["default_preset"],
        "updated_at": snap["updated_at"],
        "unmatched": snap["unmatched"],
    }


@app.get("/api/unmatched/download")
def unmatched_download():
    """Скачать список моделей без матча (debug)."""
    snap = _snapshot()
    lines = [f"# unmatched models ({len(snap['unmatched'])} total)"]
    for m in snap["unmatched"]:
        lines.append(m)
    return PlainTextResponse("\n".join(lines) + "\n", headers={
        "Content-Disposition": "attachment; filename=unmatched_models.txt"
    })


# Статичный дашборд (index.html ходит в /api/models). Монтируем последним,
# чтобы не перехватывать /api/*.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
