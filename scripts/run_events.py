"""Run flood-mapping events: auto-pick dry-season baselines, process, save artifacts."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pipeline import EventSpec, process_event  # noqa: E402
import httpx  # noqa: E402

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
AOI = {"type": "Polygon", "coordinates": [[
    [77.04, 28.42], [77.39, 28.42], [77.39, 28.74], [77.04, 28.74], [77.04, 28.42],
]]}
ROOT = Path(__file__).resolve().parents[1]

# (event_id, label, [event scene date-time prefixes])
EVENTS = [
    ("flood-2023-07", "July 2023 record flood — Yamuna at 208.66 m at Old Railway Bridge",
     ["20230712T005233", "20230716T125521", "20230716T125550", "20230724",
      "20230728T125521", "20230728T125550"]),
    ("monsoon-2026-08", "Monsoon 2026 — latest Sentinel-1A/1D passes over Delhi",
     ["20260703", "20260727", "20260820"]),
]


def search(year: int, lo: str, hi: str) -> list[dict]:
    r = httpx.post(f"{STAC}/search", json={
        "collections": ["sentinel-1-rtc"], "intersects": AOI,
        "datetime": f"{year}-{lo}/{year}-{hi}", "limit": 100,
    }, timeout=60)
    r.raise_for_status()
    out = [{"id": f["id"], "dt": f["properties"]["datetime"]}
           for f in r.json()["features"]]
    return sorted(out, key=lambda s: s["dt"])


def auto_baseline(year: int, max_scenes: int = 4) -> list[str]:
    """Dry-season scenes (Feb-Mar) of the event year, spread through the window."""
    scenes = search(year, "02-01", "03-31")
    if not scenes:
        raise RuntimeError(f"no dry-season scenes for {year}")
    picks = [scenes[0], scenes[len(scenes) // 3],
             scenes[2 * len(scenes) // 3], scenes[-1]][:max_scenes]
    return list(dict.fromkeys(p["id"] for p in picks))


def _norm(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def resolve(prefixes: list[str]) -> list[str]:
    """Expand date-time prefixes to full item ids via the saved discovery index."""
    disc = json.load(open(ROOT / "data" / "stac_discovery.json"))
    pool = sorted({s["id"] for scenes in disc.values() for s in scenes})
    ids = []
    for pref in prefixes:
        npref = _norm(pref)
        matches = [i for i in pool if npref in _norm(i)]
        if not matches:
            raise RuntimeError(f"no item for prefix {pref!r}")
        ids.append(matches[0])
    return ids


def main(only: str | None = None):
    results = []
    for eid, label, prefixes in EVENTS:
        if only and only != eid:
            continue
        evt_ids = resolve(prefixes)
        year = int(eid.split("-")[1])
        base_ids = auto_baseline(year)
        spec = EventSpec(event_id=eid, label=label,
                         baseline_ids=base_ids, event_ids=evt_ids)
        print(f"\n=== {eid}: {len(base_ids)} baseline + {len(evt_ids)} event scenes")
        meta = process_event(spec, ROOT / "data" / "events" / eid)
        f = meta["flood_new_inundation"]
        print(f"    flood: {f['area_km2']} km2 | permanent water: "
              f"{meta['permanent_water']['area_km2']} km2 | "
              f"{meta['flood_polygon_count']} polys | {meta['runtime_s']}s")
        print("    diag:", json.dumps(meta["classification"]["diagnostics"]))
        if meta.get("population_exposure"):
            print("    exposure:", meta["population_exposure"])
        results.append(meta)

    slim = [{k: m.get(k) for k in
             ("event_id", "label", "baseline_items_by_orbit",
              "event_items_by_orbit", "permanent_water",
              "flood_new_inundation", "flood_polygon_count",
              "largest_flood_polys_km2", "classification", "processing",
              "runtime_s", "population_exposure", "artifacts")}
            for m in results]
    (ROOT / "data" / "events_index.json").write_text(json.dumps({
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "events": slim}, indent=2))
    print("\nwrote data/events_index.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
