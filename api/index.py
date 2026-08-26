"""Yamuna Flood Mapper API — serves precomputed flood artifacts.

Runs on Vercel serverless (@vercel/python). Reads committed JSON/GeoJSON
from ../data so no heavy geo deps are needed at request time.
"""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EVENTS_DIR = DATA_DIR / "events"

app = FastAPI(
    title="Yamuna Flood Mapper",
    description="Sentinel-1 SAR flood extents for the Yamuna corridor, Delhi. "
                "Data: Copernicus/ESA RTC via Microsoft Planetary Computer.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    expose_headers=["*"],
)


def _load(path: Path):
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"not found: {path.name}")
    return json.loads(path.read_text())


@app.get("/api/health")
def health():
    return {"ok": True, "service": "yamuna-flood-mapper-api"}


@app.get("/api/events")
def events():
    idx = _load(DATA_DIR / "events_index.json")
    return {
        "generated": idx.get("generated"),
        "aoi_bounds": [77.04, 28.42, 77.39, 28.74],
        "events": [
            {
                "event_id": e["event_id"],
                "label": e["label"],
                "flood_new_inundation": e.get("flood_new_inundation"),
                "permanent_water": e.get("permanent_water"),
                "flood_polygon_count": e.get("flood_polygon_count"),
                "largest_flood_polys_km2": e.get("largest_flood_polys_km2"),
                "population_exposure": e.get("population_exposure"),
                "event_times_utc": sorted({
                    i["datetime"] for ids in (e.get("event_items_by_orbit") or {})
                    .values() for i in ids
                }) if e.get("event_items_by_orbit") else [],
            }
            for e in idx.get("events", [])
        ],
    }


@app.get("/api/events/{event_id}")
def event_meta(event_id: str):
    return _load(EVENTS_DIR / event_id / "meta.json")


@app.get("/api/events/{event_id}/flood.geojson")
def event_flood(event_id: str):
    return JSONResponse(content=_load(EVENTS_DIR / event_id / "flood.geojson"))


@app.get("/api/events/{event_id}/permanent-water.geojson")
def event_perm(event_id: str):
    return JSONResponse(
        content=_load(EVENTS_DIR / event_id / "permanent_water.geojson"))
