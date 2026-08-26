"""Core SAR flood-mapping pipeline for the Yamuna Flood Mapper.

Sentinel-1 RTC (gamma-0, VH) from Microsoft Planetary Computer ->
same-orbit per-pixel change detection vs dry-season baseline ->
vectorized flood extents + permanent water + WorldPop exposure.

Method: z = (baseline_mean_dB - event_dB) / baseline_std_dB, thresholded
by Otsu on the z histogram (bimodal by construction) with an absolute
darkness cap. Free anonymous SAS auth; no paid services.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# Robust remote reads on flaky links: retry at the GDAL layer too.
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "5")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("GDAL_CACHEMAX", "256")

import httpx
import numpy as np
import rasterio
from affine import Affine
from pyproj import Geod, Transformer
from rasterio.enums import Resampling
from rasterio.warp import reproject as rio_reproject
from rasterio.features import shapes as rio_shapes
from scipy.ndimage import binary_closing, binary_opening
from shapely.geometry import shape, mapping, box

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-1-rtc"
_SAS_CACHE: dict[str, tuple[str, float]] = {}

# ---------------------------------------------------------------- AOI / grid
CORRIDOR_BOUNDS = (77.04, 28.42, 77.39, 28.74)  # Yamuna river + floodplain, Delhi
RES_DEG = 0.00025                                # ~27.8 m at Delhi latitude
GRID_WIDTH = int(round((CORRIDOR_BOUNDS[2] - CORRIDOR_BOUNDS[0]) / RES_DEG))
GRID_HEIGHT = int(round((CORRIDOR_BOUNDS[3] - CORRIDOR_BOUNDS[1]) / RES_DEG))
GRID_TRANSFORM = Affine.translation(CORRIDOR_BOUNDS[0], CORRIDOR_BOUNDS[3]) * \
    Affine.scale(RES_DEG, -RES_DEG)
_GEOD = Geod(ellps="WGS84")
CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache"

# --------------------------------------------------------------- tunables
BAND = "vh"               # VH: best water/land contrast
DECIMATE = 2              # read native/2 (=20 m) via COG overview level 2
MIN_BASELINE_N = 3        # min valid baseline scenes for stats
MIN_STD_DB = 1.2          # floor for baseline std (speckle)
Z_LO, Z_HI = (-3.0, 12.0)  # Otsu search range on z
Z_CLAMP = (1.5, 8.0)      # final clamp on chosen z threshold
VH_ABS_CAP_DB = -17.0     # event pixel must be at least this dark
MIN_POLY_M2 = 5000.0      # drop flood polygons < 0.5 ha
FETCH_WORKERS = 2
READ_ATTEMPTS = 4


def _retry_http(fn, what: str, attempts: int = 4):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # flaky links: SSL EOFs, resets, timeouts
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{what} failed after {attempts} attempts") from last


def _sas_token() -> str:
    tok, exp = _SAS_CACHE.get("pc", ("", 0))
    if exp > time.time() + 300:
        return tok

    def _do():
        r = httpx.get(
            "https://planetarycomputer.microsoft.com/api/sas/v1/token/"
            "sentinel-1-rtc", timeout=60)
        r.raise_for_status()
        return r.json()

    j = _retry_http(_do, "sas-token")
    tok = j["token"]
    exp = time.mktime(time.strptime(j["msft:expiry"], "%Y-%m-%dT%H:%M:%SZ")) - 6 * 3600
    _SAS_CACHE["pc"] = (tok, exp)
    return tok


def get_item(item_id: str) -> dict:
    def _do():
        r = httpx.get(f"{STAC}/collections/{COLLECTION}/items/{item_id}",
                      timeout=60)
        r.raise_for_status()
        return r.json()
    return _retry_http(_do, f"item:{item_id[:40]}")


# --------------------------------------------------------------- scene fetch
def _fetch_dn_decimated(href: str) -> tuple[np.ndarray, Affine, object]:
    """Windowed native-CRS read at overview level 2, with retries.

    Returns (DN array, affine, CRS) — CRS read from the file itself because
    RTC products may land in either UTM zone covering Delhi.
    """
    sep = "&" if "?" in href else "?"
    url = f"/vsicurl/{href}{sep}{_sas_token()}"
    last_err: Exception | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            with rasterio.open(url) as src:
                tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                x0, y0 = tr.transform(CORRIDOR_BOUNDS[0], CORRIDOR_BOUNDS[1])
                x1, y1 = tr.transform(CORRIDOR_BOUNDS[2], CORRIDOR_BOUNDS[3])
                pad = 1000.0
                ux0, ux1 = min(x0, x1) - pad, max(x0, x1) + pad
                uy0, uy1 = min(y0, y1) - pad, max(y0, y1) + pad
                win = rasterio.windows.from_bounds(ux0, uy0, ux1, uy1,
                                                   src.transform)
                # clamp to the raster: padded windows can poke past the edge
                full = rasterio.windows.Window(0, 0, src.width, src.height)
                win = win.intersection(full).round_offsets().round_lengths()
                if win.width < 4 or win.height < 4:
                    raise ValueError("corridor outside scene footprint")
                w_px = max(2, round(win.width // DECIMATE))
                h_px = max(2, round(win.height // DECIMATE))
                dn = src.read(1, window=win, out_shape=(h_px, w_px),
                              resampling=Resampling.bilinear).astype("float32")
                tf = src.window_transform(win) * Affine.scale(
                    win.width / w_px, win.height / h_px)
                crs = src.crs
            dn[dn == -32768.0] = np.nan
            dn[dn <= 0] = np.nan
            return dn, tf, crs
        except Exception as e:  # transient /vsicurl failures happen
            last_err = e
            if "outside scene footprint" in str(e):
                raise  # deterministic — no point retrying
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(
        f"read failed after {READ_ATTEMPTS} attempts: {href[:80]}") from last_err


def _to_grid(dn: np.ndarray, src_tf: Affine, src_crs) -> np.ndarray:
    dst = np.full((GRID_HEIGHT, GRID_WIDTH), np.nan, dtype="float32")
    rio_reproject(dn, dst, src_transform=src_tf, src_crs=src_crs,
                  src_nodata=np.nan, dst_transform=GRID_TRANSFORM,
                  dst_crs="EPSG:4326", dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
    return dst


def fetch_band(item_id: str, band: str = BAND) -> tuple[np.ndarray, np.ndarray, str]:
    """dB + valid mask + datetime for one scene, with npz disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{item_id}.{band}.npz"
    if cache.exists():
        z = np.load(cache)
        return z["db"], z["valid"], str(z["dt"])
    item = get_item(item_id)
    href = item["assets"][band]["href"]
    dt = item["properties"].get("datetime", "")
    dn, tf, crs = _fetch_dn_decimated(href)
    grid = _to_grid(dn, tf, crs)
    valid = np.isfinite(grid)
    db = np.full(grid.shape, np.nan, dtype="float32")
    db[valid] = 10.0 * np.log10(grid[valid])
    np.savez_compressed(cache, db=db, valid=valid, dt=dt)
    return db, valid, dt


def fetch_many(item_ids: list[str], band: str = BAND) -> dict[str, tuple]:
    """Fetch scenes in parallel; scenes that exhaust retries are skipped.
    Returns {item_id: (db, valid, dt)}."""
    out: dict[str, tuple] = {}
    todo = []
    for iid in item_ids:
        cache = CACHE_DIR / f"{iid}.{band}.npz"
        if cache.exists():
            z = np.load(cache)
            out[iid] = (z["db"], z["valid"], str(z["dt"]))
        else:
            todo.append(iid)
    if todo:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            futures = [(iid, ex.submit(fetch_band, iid, band)) for iid in todo]
            for iid, fut in futures:
                try:
                    out[iid] = fut.result()
                except Exception as e:
                    print(f"WARN skipping {iid}: {e}", flush=True)
    return out


# ------------------------------------------------------------ classification
def otsu_threshold(values: np.ndarray, lo: float, hi: float,
                   bins: int = 400) -> float:
    v = values[np.isfinite(values)]
    hist, edges = np.histogram(v, bins=bins, range=(lo, hi))
    hist = hist.astype("float64")
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    sum0 = np.cumsum(hist * centers)
    mean_tot = sum0[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mu0 = sum0 / w0
        mu1 = (mean_tot - sum0) / w1
        between = w0 * w1 * (mu0 - mu1) ** 2
    between[~np.isfinite(between)] = -1
    # pick the CENTER of the max-plateau (argmax alone biases to its left edge);
    # degenerate (zero-variance / empty-range) histograms fall back to midpoint
    peak = float(between.max())
    if peak <= 0:
        return float((lo + hi) / 2)
    idxs = np.where(between >= peak * 0.999)[0]
    return float(centers[int(idxs[len(idxs) // 2])])


@dataclass
class BaselineStats:
    mean_db: np.ndarray
    std_db: np.ndarray
    n: np.ndarray            # valid scene count per pixel
    orbit: str
    item_ids: list[str] = field(default_factory=list)


def baseline_stats(dbs: list[np.ndarray], valids: list[np.ndarray],
                   orbit: str, item_ids: list[str]) -> BaselineStats:
    stack = np.stack(dbs)
    vmask = np.stack(valids)
    n = vmask.sum(axis=0)
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(np.where(vmask, stack, np.nan), axis=0)
        std = np.nanstd(np.where(vmask, stack, np.nan), axis=0)
    ok = n >= MIN_BASELINE_N
    mean[~ok] = np.nan
    std[~ok] = np.nan
    return BaselineStats(mean.astype("float32"), std.astype("float32"),
                         n.astype("uint8"), orbit, item_ids)


def classify_scene(evt_db: np.ndarray, evt_valid: np.ndarray,
                   base: BaselineStats) -> tuple[np.ndarray, dict]:
    """Per-pixel z-score change detection -> (flood mask, diagnostics)."""
    usable = evt_valid & np.isfinite(base.mean_db) & np.isfinite(base.std_db)
    z = (base.mean_db - evt_db) / np.maximum(base.std_db, MIN_STD_DB)
    z_thr = otsu_threshold(z[usable], Z_LO, Z_HI)
    z_thr = float(np.clip(z_thr, *Z_CLAMP))
    flood = usable & (z > z_thr) & (evt_db < VH_ABS_CAP_DB)
    struct = np.ones((3, 3), dtype=bool)
    flood = binary_closing(flood, structure=struct, iterations=2)
    flood = binary_opening(flood, structure=struct, iterations=1)
    diag = {"orbit": base.orbit, "z_threshold": round(z_thr, 2),
            "usable_frac": round(float(usable.mean()), 3)}
    return flood, diag


def permanent_water(bases: list[BaselineStats]) -> np.ndarray:
    """Stable dark pixels across all baseline scenes (the river itself)."""
    stack = np.stack([b.mean_db for b in bases])
    vmask = np.stack([np.isfinite(b.mean_db) for b in bases])
    with np.errstate(invalid="ignore"):
        comp = np.nanmean(np.where(vmask, stack, np.nan), axis=0)
        spread = np.nanmax(np.where(vmask, stack, np.nan), axis=0) - \
            np.nanmin(np.where(vmask, stack, np.nan), axis=0)
    fin = np.isfinite(comp) & np.isfinite(spread)
    # bounded search: water sits near -20 dB in VH means; an unbounded range
    # lets Otsu split the LAND population instead
    thr = otsu_threshold(comp[fin], -26.0, -15.0)
    perm = fin & (comp < thr) & (spread < 6.0)
    struct = np.ones((3, 3), dtype=bool)
    perm = binary_closing(perm, structure=struct, iterations=1)
    return perm


# ------------------------------------------------------- stats / vectorizing
def _cell_area_m2() -> float:
    b = CORRIDOR_BOUNDS
    poly = box(b[0], b[1], b[0] + RES_DEG, b[1] + RES_DEG)
    return abs(_GEOD.geometry_area_perimeter(poly)[0])


def mask_stats(mask: np.ndarray) -> dict:
    n_px = int(mask.sum())
    return {"pixels": n_px, "area_km2": round(n_px * _cell_area_m2() / 1e6, 3)}


def vectorize(mask: np.ndarray, min_area_m2: float = MIN_POLY_M2) -> list[dict]:
    feats = []
    for geom, val in rio_shapes(mask.astype("uint8"), mask=mask,
                                transform=GRID_TRANSFORM):
        if int(val) != 1:
            continue
        g = shape(geom)
        if g.is_empty:
            continue
        area_m2, _ = _GEOD.geometry_area_perimeter(g)
        if area_m2 < min_area_m2:
            continue
        g = g.simplify(0.00005, preserve_topology=True)
        feats.append({
            "type": "Feature",
            "geometry": mapping(g),
            "properties": {
                "area_km2": round(area_m2 / 1e6, 4),
                "area_ha": round(area_m2 / 1e4, 2),
            },
        })
    feats.sort(key=lambda f: -f["properties"]["area_km2"])
    return feats


# ------------------------------------------------------------------ WorldPop
# Meta Data-for-Good HRSL population (CC-BY 4.0): global COG mosaic on S3,
# ranged reads supported -> windowed reads work with no download
POP_URL = ("https://dataforgood-fb-data.s3.amazonaws.com/"
           "hrsl-cogs/hrsl_general/hrsl_general-latest.vrt")


def population_exposure(flood_mask: np.ndarray) -> dict | None:
    try:
        with rasterio.open(f"/vsicurl/{POP_URL}") as src:
            b = CORRIDOR_BOUNDS
            win = rasterio.windows.from_bounds(b[0], b[1], b[2], b[3],
                                               src.transform)
            pop = src.read(1, window=win).astype("float32")
            tf = src.window_transform(win)
            dst = np.zeros(flood_mask.shape, dtype="float32")
            rio_reproject(pop, dst, src_transform=tf, src_crs=src.crs,
                          src_nodata=src.nodata, dst_transform=GRID_TRANSFORM,
                          dst_crs="EPSG:4326", dst_nodata=np.nan,
                          resampling=Resampling.average)
        valid = np.isfinite(dst) & (dst >= 0)
        total = float(dst[valid].sum())
        exposed = float(dst[flood_mask & valid].sum())
        return {
            "exposed_people": int(round(exposed)),
            "aoi_population": int(round(total)),
            "exposed_pct": round(100 * exposed / max(total, 1), 3),
            "source": "Meta Data for Good HRSL population v1.5 (~30 m), CC-BY 4.0",
        }
    except Exception as e:
        return {"error": str(e)[:200]}


# ------------------------------------------------------------------ event API
@dataclass
class EventSpec:
    event_id: str
    label: str
    baseline_ids: list[str]      # flat list; grouped by orbit internally
    event_ids: list[str]


def process_event(spec: EventSpec, out_dir: Path,
                  with_exposure: bool = True) -> dict:
    t0 = time.time()

    def orbit_of(iid: str) -> str:
        return get_item(iid)["properties"].get("sat:orbit_state", "unknown")

    # --- group baselines by orbit
    base_groups: dict[str, list[str]] = {}
    for bid in spec.baseline_ids:
        base_groups.setdefault(orbit_of(bid), []).append(bid)

    bases: list[BaselineStats] = []
    base_meta, failed_items = {}, []
    for orb, ids in sorted(base_groups.items()):
        got = fetch_many(ids)
        ids_ok = [i for i in ids if i in got]
        if len(ids_ok) < MIN_BASELINE_N:
            raise RuntimeError(
                f"orbit {orb}: only {len(ids_ok)} usable baseline scenes")
        dbs = [got[i][0] for i in ids_ok]
        vds = [got[i][1] for i in ids_ok]
        bases.append(baseline_stats(dbs, vds, orb, ids_ok))
        base_meta[orb] = ids_ok
        failed_items += [i for i in ids if i not in got]

    # --- classify each event scene under its orbit's baseline
    evt_groups: dict[str, list[str]] = {}
    for eid in spec.event_ids:
        evt_groups.setdefault(orbit_of(eid), []).append(eid)

    flood_total = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
    diags, evt_meta = [], {}
    for orb, ids in sorted(evt_groups.items()):
        base = next((b for b in bases if b.orbit == orb), None)
        if base is None:
            print(f"WARN no {orb} baseline — skipping {len(ids)} event scene(s)",
                  flush=True)
            failed_items += ids
            continue
        got = fetch_many(ids)
        ids_ok = [i for i in ids if i in got]
        for iid in ids_ok:
            db, vd, dt = got[iid]
            flood, diag = classify_scene(db, vd, base)
            flood_total |= flood
            diag.update({"item": iid, "datetime": dt})
            diags.append(diag)
        evt_meta[orb] = [{"id": i, "datetime": got[i][2]} for i in ids_ok]
        failed_items += [i for i in ids if i not in got]

    perm = permanent_water(bases)

    flood_feats = vectorize(flood_total)
    perm_feats = vectorize(perm, min_area_m2=MIN_POLY_M2 * 2)

    meta = {
        "event_id": spec.event_id,
        "label": spec.label,
        "baseline_items_by_orbit": base_meta,
        "event_items_by_orbit": evt_meta,
        "classification": {
            "diagnostics": diags,
            "vh_abs_cap_db": VH_ABS_CAP_DB,
            "min_baseline_scenes": MIN_BASELINE_N,
            "skipped_items": failed_items,
        },
        "permanent_water": mask_stats(perm),
        "flood_new_inundation": mask_stats(flood_total),
        "flood_polygon_count": len(flood_feats),
        "largest_flood_polys_km2": [
            f["properties"]["area_km2"] for f in flood_feats[:10]],
        "processing": {
            "sensor": "Sentinel-1 C-SAR RTC gamma-0 (VH)",
            "provider": "Microsoft Planetary Computer",
            "method": ("same-orbit per-pixel dB change vs dry-season baseline; "
                       "Otsu on z with absolute darkness cap"),
            "grid_res_m": 27.8,
            "read_level": f"COG overview {int(np.log2(DECIMATE))} (~20 m)",
            "min_poly_ha": MIN_POLY_M2 / 1e4,
        },
        "runtime_s": round(time.time() - t0, 1),
    }
    if with_exposure:
        meta["population_exposure"] = population_exposure(flood_total)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flood.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "name": f"flood_{spec.event_id}",
        "features": flood_feats}))
    (out_dir / "permanent_water.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "name": f"perm_{spec.event_id}",
        "features": perm_feats}))

    # compact QA preview (event dB, baseline mean, flood overlay)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.2), dpi=110)
        some_evt = fetch_many(list(evt_groups.values())[0])[ \
            list(evt_groups.values())[0][0]][0]
        axes[0].imshow(some_evt, vmin=-28, vmax=-8, cmap="gray")
        axes[0].set_title("event VH dB")
        axes[1].imshow(bases[0].mean_db, vmin=-24, vmax=-10, cmap="gray")
        axes[1].set_title(f"dry baseline mean ({bases[0].orbit})")
        rgb = np.zeros((GRID_HEIGHT, GRID_WIDTH, 3), dtype="float32")
        gray = np.clip((some_evt + 26) / 18, 0, 1)
        rgb[..., 0] = gray * 0.55
        rgb[..., 1] = gray * 0.55
        rgb[..., 2] = gray * 0.55
        rgb[flood_total] = (1.0, 0.25, 0.05)
        rgb[perm] = (0.1, 0.45, 0.95)
        axes[2].imshow(rgb)
        axes[2].set_title("red=new flood | blue=permanent water")
        for ax in axes:
            ax.set_xticks([]), ax.set_yticks([])
        plt.tight_layout()
        plt.savefig(out_dir / "qa_triptych.png")
        plt.close(fig)
        meta["artifacts"] = {
            "flood_geojson_kb": round(
                (out_dir / "flood.geojson").stat().st_size / 1024, 1),
            "perm_geojson_kb": round(
                (out_dir / "permanent_water.geojson").stat().st_size / 1024, 1),
            "qa_png": "qa_triptych.png",
        }
    except Exception as e:  # QA render must never sink the pipeline
        meta["artifacts"] = {"qa_error": str(e)[:150]}

    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta
