"""Discover Sentinel-1 RTC scenes over the Delhi/Yamuna AOI on Planetary Computer."""
import json
import sys
import httpx

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
AOI = {
    "type": "Polygon",
    "coordinates": [[
        [76.95, 28.35], [77.55, 28.35],
        [77.55, 28.85], [76.95, 28.85], [76.95, 28.35],
    ]],
}

WINDOWS = {
    "dry_baseline_2026": "2026-01-01/2026-03-31",
    "monsoon_2026": "2026-06-01/2026-08-26",
    "flood_jul2023": "2023-07-01/2023-08-15",
    "dry_2023": "2023-01-01/2023-04-30",
    "monsoon_2024": "2024-06-15/2024-08-15",
    "monsoon_2025": "2025-06-15/2025-08-15",
}


def search(datetime_range: str):
    payload = {
        "collections": ["sentinel-1-rtc"],
        "intersects": AOI,
        "datetime": datetime_range,
        "limit": 100,
    }
    r = httpx.post(f"{STAC}/search", json=payload, timeout=60)
    r.raise_for_status()
    feats = r.json().get("features", [])
    out = []
    for f in feats:
        p = f["properties"]
        out.append({
            "id": f["id"],
            "datetime": p.get("datetime"),
            "platform": p.get("platform"),
            "polarization": p.get("sar:polarizations"),
            "bbox": f.get("bbox"),
        })
    out.sort(key=lambda x: x["datetime"])
    return out


if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)
    result = {}
    for name, rng in WINDOWS.items():
        try:
            scenes = search(rng)
            result[name] = scenes
            print(f"== {name} ({rng}): {len(scenes)} scenes")
        except Exception as e:
            print(f"== {name}: ERROR {e}", file=sys.stderr)
    with open("data/stac_discovery.json", "w") as fh:
        json.dump(result, fh, indent=2)
    print("wrote data/stac_discovery.json")
