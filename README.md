# Yamuna Flood Mapper 🛰️🌊

**Sentinel-1 SAR flood mapping for the Yamuna corridor, Delhi — production-ready,
100 % free data & hosting.**

Live: **https://yamuna-flood-mapper.vercel.app** ·
API: `/api/events` · `/api/events/{id}/flood.geojson`

---

## What it does

| | |
|---|---|
| 🛰️ **Input** | Copernicus Sentinel-1 RTC gamma-0 (VH, 10 m) via [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc) — free, no auth keys |
| 🧮 **Method** | Same-orbit per-pixel dB change vs a Feb–Mar dry-season baseline → Otsu threshold on the z-histogram + absolute darkness cap → morphology cleanup |
| 🗺️ **Output** | Vectorized new-inundation polygons, permanent-water layer, km² stats, HRSL population exposure |
| ⏱️ **Refresh** | `scripts/refresh_latest.py` — run via cron/GitHub Actions to append the newest pass automatically |
| 💰 **Cost** | ₹0 — free satellite data, Vercel free tier |

## Why SAR?

Optical satellites are blind during monsoon cloud cover — exactly when floods
happen. Sentinel-1's C-band radar penetrates clouds and works day/night. Smooth
water reflects radar away (specular), so flooded surfaces drop ~8–15 dB in VH
backscatter versus dry land: change detection against the same orbit's dry
baseline makes the flood extent pop out statistically.

## Architecture

```
scripts/discover_scenes.py   STAC search → scene inventory (data/stac_discovery.json)
scripts/pipeline.py          fetch (COG overview reads + npz cache) → z-score change
                             detection → vectorize → population exposure → QA triptych
scripts/run_events.py        event driver (auto dry-season baselines per orbit)
scripts/refresh_latest.py    incremental: newest pass → new event folder + index update
api/index.py                 FastAPI serving committed GeoJSON (Vercel serverless)
web/index.html               MapLibre dark frontend (event picker, layers, stats)
data/events/<id>/            flood.geojson · permanent_water.geojson · meta.json
vercel.json                  /api/* → FastAPI, everything else → web/
```

## Method details

- **Grid**: 77.04–77.39°E, 28.42–28.74°N (river + floodplain; Old Railway Bridge
  gauge sits inside), 0.00025° ≈ 27.8 m cells.
- **Reads**: windowed COG reads at overview level 2 (~20 m) then bilinear warp
  to the shared grid — keeps each scene download small.
- **Baseline**: 4+ dry-season scenes **per orbit direction** (ascending ~00:45 UTC,
  descending ~12:55 UTC passes never mix). Per-pixel mean/std of dB.
- **Classification**: z = (μ_dry − dB_event)/σ_dry, Otsu on z with clamp [1.5, 8];
  pixel must also be < −17 dB absolute (VH). Closing(3×3×2)+opening removes speckle.
- **QA**: every event writes `qa_triptych.png` (event dB | baseline mean |
  red=flood/blue=permanent overlay). Human-check before publishing.

## Run locally

```bash
pip install rasterio shapely pyproj scipy numpy httpx matplotlib fastapi uvicorn pytest

python scripts/discover_scenes.py     # rebuild scene index (~125 scenes)
python scripts/run_events.py          # process all events (cached scenes skip fast)
uvicorn api.index:app --reload        # API at :8000
python -m http.server 3000 -d web     # UI at :3000 (expects API on :8000)
pytest -q                             # offline unit tests on synthetic rasters
```

First uncached scene ≈ 60–90 s (slow networks); cached runs are seconds.

## Deploy

```bash
vercel --prod
```

The frontend auto-detects same-origin API in production.

## Data credits & license

- Contains modified Copernicus Sentinel data [2023–2026], processed by ESA
  (`sentinel-1-rtc` collection) — distributed via Microsoft Planetary Computer.
- Population: Meta Data for Good HRSL v1.5 (CC-BY 4.0).
- Basemap © OpenStreetMap contributors © CARTO.

> ⚠️ Research/demo product — not an official flood warning. Cross-check CWC
> gauge readings and DDMA advisories for operational decisions.
