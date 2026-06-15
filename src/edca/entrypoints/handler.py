"""HTTP entrypoint for Cloud Run (invoked by Cloud Scheduler or Pub/Sub push).

A tiny FastAPI app so Cloud Run has something to serve. Cloud Scheduler hits
POST /run on a cron; Pub/Sub push (event-driven) can hit the same handler.
"""
from __future__ import annotations

from fastapi import FastAPI

from edca.entrypoints.cron import run_once
from edca.settings import Settings

app = FastAPI(title="edw-data-control-agent")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run() -> dict[str, str]:
    await run_once(Settings.from_env())
    return {"status": "completed"}
