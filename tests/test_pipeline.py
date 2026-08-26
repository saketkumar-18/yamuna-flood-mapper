"""Tests for the flood-mapping pipeline — fully offline, synthetic rasters only."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pipeline import (  # noqa: E402
    BaselineStats, GRID_HEIGHT, GRID_WIDTH, MIN_STD_DB,
    baseline_stats, classify_scene,
    mask_stats, otsu_threshold, permanent_water, vectorize,
)


def _flat(value, nan_frac=0.0, seed=1):
    rng = np.random.default_rng(seed)
    arr = np.full((GRID_HEIGHT, GRID_WIDTH), value, dtype="float32")
    if nan_frac:
        hole = rng.random(arr.shape) < nan_frac
        arr[hole] = np.nan
    return arr


# ------------------------------------------------------------------ Otsu
def test_otsu_separates_bimodal():
    v = np.concatenate([np.random.default_rng(0).normal(-20, 1, 5000),
                        np.random.default_rng(1).normal(-6, 1.5, 5000)])
    thr = otsu_threshold(v.astype("float32"), -25, 0)
    assert -15 < thr < -10


def test_otsu_range_clamps_search():
    v = np.full(1000, -3.0, dtype="float32")  # degenerate: outside search range
    thr = otsu_threshold(v, -12, -2)
    assert -12 <= thr <= -2


# ------------------------------------------------------------ change logic
def test_classify_scene_ignores_brightening():
    """Pixels brighter than baseline must never be flagged (z <= 0)."""
    mean = _flat(-13.0)
    std = np.full_like(mean, 1.0)
    base = BaselineStats(mean, std,
                         np.full(mean.shape, 4, dtype="uint8"), "descending")
    evt = _flat(-8.0)
    flood, _ = classify_scene(evt, np.ones_like(evt, dtype=bool), base)
    assert float(flood.mean()) == 0.0


# --------------------------------------------------------- baseline stats
def test_baseline_stats_n_and_nan_handling():
    dbs = [_flat(-13.0, seed=i) for i in range(4)]
    valids = [np.isfinite(d) for d in dbs]
    valids[0][:, :10] = False     # cols 0-9: n=3 -> still finite (== MIN)
    for k in (1, 2, 3):
        valids[k][:, 20:30] = False  # cols 20-29: n=1 -> NaN
    bs = baseline_stats(dbs, valids, "descending", list("abcd"))
    assert bs.mean_db.shape == (GRID_HEIGHT, GRID_WIDTH)
    assert np.isfinite(float(bs.mean_db[:, 5].mean()))
    assert np.isnan(float(bs.mean_db[:, 25].mean()))
    assert int(bs.n[:, 25].max()) == 1


def test_baseline_stats_values():
    dbs = [_flat(-13.0, seed=i) for i in range(3)]
    valids = [np.isfinite(d) for d in dbs]
    bs = baseline_stats(dbs, valids, "ascending", list("abc"))
    assert abs(float(np.nanmean(bs.mean_db)) - (-13.0)) < 1e-4
    assert float(np.nanmin(bs.std_db)) >= 0      # floor applied in classify_scene
    assert bs.orbit == "ascending"


# ---------------------------------------------------------- classification
def test_classify_scene_flags_dark_blob():
    mean = _flat(-13.0)
    std = np.full_like(mean, 1.0)
    base = BaselineStats(mean, std,
                         np.full(mean.shape, 4, dtype="uint8"), "descending")
    evt = _flat(-13.0)
    evt[500:560, 600:700] = -26.0     # flooded blob: far darker than baseline
    valid = np.ones_like(evt, dtype=bool)
    flood, diag = classify_scene(evt, valid, base)
    inner = flood[520:545, 630:680]
    assert inner.mean() > 0.9         # core of blob must be flagged
    assert float(flood[50:90, 50:90].mean()) < 0.02   # unchanged land stays dry
    assert diag["orbit"] == "descending"


def test_classify_scene_respects_abs_cap():
    mean = _flat(-13.0)
    std = np.full_like(mean, 1.0)
    base = BaselineStats(mean, std,
                         np.full(mean.shape, 4, dtype="uint8"), "descending")
    evt = _flat(-16.0)                # z-drop large but NOT under darkness cap
    valid = np.ones_like(evt, dtype=bool)
    flood, _ = classify_scene(evt, valid, base)
    assert float(flood.mean()) == 0.0


def test_permanent_water_finds_stable_dark_river():
    comp = _flat(-11.0)
    comp[:, 700:730] = -21.0          # stable river stripe
    spread = np.full_like(comp, 0.5)
    bases = [BaselineStats(comp.copy(), np.full_like(comp, 1.0),
                           np.full(comp.shape, 3, dtype="uint8"), "descending")]
    perm = permanent_water(bases)
    stripe = perm[:, 702:728].mean()
    land = perm[:, 100:400, :].mean() if perm.ndim == 3 else perm[:, 100:400].mean()
    assert stripe > 0.7
    assert land < 0.05


# ------------------------------------------------------- stats/vectorizing
def test_mask_stats_area_matches_pixels():
    m = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
    st0 = mask_stats(m)
    assert st0["area_km2"] == 0 and st0["pixels"] == 0
    m[:10, :] = True
    st1 = mask_stats(m)
    assert st1["pixels"] == 10 * GRID_WIDTH
    assert 0 < st1["area_km2"] < 100   # corridor is ~38 km wide -> 10 rows ~ 0.3 km2


def test_vectorize_min_area_filter_and_geojson():
    m = np.zeros((60, 60), dtype=bool)
    m[10:14, 10:14] = True            # 16 px ≈ 0.012 km² -> filtered at 5 ha
    m[30:44, 30:44] = True            # 196 px ≈ 0.15 km² -> kept
    feats = vectorize(m, min_area_m2=5e4)
    assert len(feats) == 1
    f = feats[0]
    assert f["type"] == "Feature"
    assert f["geometry"]["type"] == "Polygon"
    assert f["properties"]["area_km2"] > 0
