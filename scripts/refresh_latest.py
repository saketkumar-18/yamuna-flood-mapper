"""Incremental refresh: find the newest Sentinel-1 pass over the corridor,
map its flood extent vs that year's dry baseline, add an event folder,
update events_index.json. Designed for cron / GitHub Actions."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import httpx  # noqa: E402

from pipeline import EventSpec, get_item, process_event  # noqa: E402
from run_events import EVENTS, STAC, AOI, ROOT, auto_baseline  # noqa: E402


def latest_scene() -> dict | None:
    now = dt.datetime.now(dt.timezone.utc)
    lo = (now - dt.timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = httpx.post(f"{STAC}/search", json={
        "collections": ["sentinel-1-rtc"], "intersects": AOI,
        "datetime": f"{lo}/{hi}", "limit": 20,
    }, timeout=60)
    r.raise_for_status()
    feats = sorted(r.json()["features"],
                   key=lambda f: f["properties"]["datetime"])
    return feats[-1] if feats else None


def main():
    feat = latest_scene()
    if feat is None:
        print("no new scene")
        return
    iid = feat["id"]
    when = feat["properties"]["datetime"]
    year = when[:4]
    eid = f"pass-{when[:13].replace('-', '').replace('T', '-').replace(':', '')}"
    out = ROOT / "data" / "events" / eid
    if out.exists():
        print(f"{eid} already processed")
        return
    label = f"Latest pass {when[:16]}Z — auto-refresh"
    base_ids = auto_baseline(int(year))
    spec = EventSpec(event_id=eid, label=label,
                     baseline_ids=base_ids, event_ids=[iid])
    meta = process_event(spec, out)
    print(f"processed {iid}: {meta['flood_new_inundation']['area_km2']} km2")

    # refresh the historical events so the index stays complete
    all_meta = [json.loads((ROOT / "data" / "events" / e[0] / "meta.json")
                           .read_text())
                for e in EVENTS if (ROOT / "data" / "events" / e[0] / "meta.json")
                .exists()]
    all_meta.append(meta)
    slim = [{k: m.get(k) for k in
             ("event_id", "label", "baseline_items_by_orbit",
              "event_items_by_orbit", "permanent_water",
              "flood_new_inundation", "flood_polygon_count",
              "largest_flood_polys_km2", "classification", "processing",
              "runtime_s", "population_exposure", "artifacts")}
            for m in all_meta]
    (ROOT / "data" / "events_index.json").write_text(json.dumps({
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "events": slim}, indent=2))
    print("index updated")


if __name__ == "__main__":
    main()
