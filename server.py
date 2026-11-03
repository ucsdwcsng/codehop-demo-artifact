"""CodeHop uplink demo server.

    python server.py --port 8000          # then open http://localhost:8000
    uvicorn server:app --host 0.0.0.0     # (run from the repo root)

Serves the static UI in static/ and a small JSON API around
codehop_sim/uplink.py.  All inputs are bounded (pydantic) and every
request is cost-capped, so the server can be exposed publicly.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from codehop_sim import acquire, footprint, uplink  # noqa: E402

# ----------------------------------------------------------------------------
# parameter schema (single source of truth for the UI)
# ----------------------------------------------------------------------------

K_MAX = 128

KNOBS = [
    # group, name, label, kind, extra
    ("Waveform", "modulation", "Modulation", "select", dict(options=[
        ["codehop", "CodeHop"], ["lora", "Hopping LoRa"]])),
    ("Nodes", "n_users", "Simultaneous transmissions", "int", dict(min=1, max=K_MAX, step=1)),
    ("Nodes", "signature", "Signatures", "select", dict(options=[
        ["random", "Random"], ["distinct", "Distinct"]],
     show_if={"modulation": ["codehop"]})),
    ("Nodes", "arrival", "Arrival", "select", dict(options=[
        ["beacon", "Beacon"], ["aligned", "Aligned"], ["random", "Unslotted"]])),
    ("Nodes", "arrival_window_ms", "Window", "float",
     dict(min=0, max=50, step=0.5, unit="ms", show_if={"arrival": ["random"]})),
    ("Power", "power_model", "Model", "select", dict(options=[
        ["link", "Link budget"], ["equal", "Equal"], ["spread", "Near–far"]])),
    ("Power", "snr_db", "SNR", "float", dict(min=-20, max=10, step=0.5, unit="dB",
                                             show_if={"power_model": ["equal", "spread"]})),
    ("Power", "spread_db", "Spread", "float", dict(min=0, max=30, step=0.5, unit="dB",
                                                   show_if={"power_model": ["spread"]})),
    ("Power", "eirp_dbm", "EIRP", "float", dict(min=0, max=30, step=0.5, unit="dBm",
                                                show_if={"power_model": ["link"]})),
    ("Power", "rx_gain_dbi", "Rx gain", "float", dict(min=-3, max=20, step=0.5, unit="dBi",
                                                      show_if={"power_model": ["link"]})),
    ("Power", "nf_db", "Noise figure", "float", dict(min=0, max=10, step=0.5, unit="dB",
                                                     show_if={"power_model": ["link"]})),
    ("Power", "shadow_db", "Shadowing", "float", dict(min=0, max=10, step=0.5, unit="dB",
                                                      show_if={"power_model": ["link"]})),
    ("PHY", "family", "Code", "select", dict(
        options=[[k, v["label"]] for k, v in uplink.FAMILIES.items()], show_if={"modulation": ["codehop"]})),
    ("PHY", "chip_rate", "Chip rate", "float", dict(min=25e3, max=200e3, step=5e3, unit="kcps", scale=1e-3,
                                                    show_if={"modulation": ["codehop"]})),
    ("PHY", "bt", "Pulse Filter BT", "select", dict(
        options=[[0.0, "None"], [0.3, "0.3"], [0.5, "0.5"], [1.0, "1.0"]], show_if={"modulation": ["codehop"]})),
    ("PHY", "lora_sf", "SF", "select", dict(
        options=[[sf, f"SF{sf}"] for sf in range(7, 13)], show_if={"modulation": ["lora"]})),
    ("PHY", "lora_bw_khz", "Bandwidth", "select", dict(
        options=[[125.0, "125 kHz"], [250.0, "250 kHz"], [500.0, "500 kHz"]], show_if={"modulation": ["lora"]})),
    ("PHY", "n_channels", "Channels", "int", dict(min=1, max=16, step=1)),
    ("PHY", "spacing_khz", "Spacing", "float", dict(min=100, max=1000, step=25, unit="kHz")),
    ("PHY", "hop_pool", "Hop patterns", "int", dict(min=1, max=1024, step=1, show_if={"modulation": ["codehop"]})),
    ("Packet", "n_pilot", "Pilots", "int", dict(min=1, max=8, step=1)),
    ("Packet", "n_data", "Data symbols", "int", dict(min=1, max=16, step=1)),
    ("Packet", "rs_k", "RS k", "int", dict(min=1, max=16, step=1)),
    ("Packet", "guard_us", "Guard", "float", dict(min=0, max=2000, step=10, unit="µs", show_if={"modulation": ["codehop"]})),
    ("Receiver", "cfo_span_khz", "Search span", "float", dict(min=1, max=40, step=0.5, unit="kHz")),
    ("Receiver", "bulk_comp", "Doppler comp", "bool", dict()),
    ("Run", "seed", "Seed", "int", dict(min=0, max=2**31 - 1, step=1)),
    ("Satellite Parameters", "pass_phase", "Phase", "float",
     dict(min=-0.98, max=0.98, step=0.02, unit="", fmt="phase")),
    ("Satellite Parameters", "max_elev_deg", "Peak elevation", "float", dict(min=15, max=90, step=1, unit="°")),
    ("Satellite Parameters", "altitude_km", "Altitude", "float", dict(min=300, max=1200, step=10, unit="km")),
    ("Satellite Parameters", "cap_radius_km", "Region radius", "float", dict(min=0, max=1200, step=10, unit="km")),
    ("Satellite Parameters", "min_elev_deg", "Min elevation", "float", dict(min=0, max=30, step=1, unit="°")),
    ("Satellite Parameters", "fc_mhz", "Carrier", "select", dict(options=[[433.0, "433 MHz"], [868.0, "868 MHz"], [915.0, "915 MHz"]])),
    ("Satellite Parameters", "osc_ppm", "Oscillator error", "float", dict(min=0, max=20, step=0.5, unit="ppm")),
]


class Cfg(BaseModel):
    """Defaults describe a plausible deployment rather than the paper's stress
    test: SX126x-class nodes (+22 dBm, TCXO) hopping over one LoRaWAN-US915-like
    sub-band (8 x 200 kHz), answering a satellite beacon from a 600 km region
    (18-84 deg elevation, 4 dB shadowing: ~9 dB SNR spread) during a rising
    550 km pass; the gateway estimates each node's carrier offset from its
    pilots after removing the beam-centre Doppler it predicts from its own
    orbit.  Acquisition is blind: the gateway is not told who transmitted,
    with which code or hop pattern, or when."""
    n_users: int = Field(32, ge=1, le=K_MAX)
    signature: Literal["distinct", "random"] = "random"
    arrival: Literal["aligned", "beacon", "random"] = "beacon"
    arrival_window_ms: float = Field(10.0, ge=0, le=50)
    n_pilot: int = Field(4, ge=1, le=8)
    n_data: int = Field(16, ge=1, le=16)
    guard_us: float = Field(0.0, ge=0, le=2000)
    modulation: Literal["codehop", "lora"] = "codehop"
    family: Literal["gold63", "gold127", "prn255", "gold511"] = "gold127"
    chip_rate: float = Field(100e3, ge=25e3, le=200e3)
    mod_index: float = Field(0.5, ge=0.5, le=0.5)
    bt: float = Field(0.5, ge=0.0, le=1.0)
    n_channels: int = Field(8, ge=1, le=16)
    spacing_khz: float = Field(200.0, ge=100, le=1000)
    hop_pool: int = Field(1024, ge=1, le=1024)
    lora_sf: int = Field(7, ge=7, le=12)
    lora_bw_khz: Literal[125.0, 250.0, 500.0] = 125.0
    altitude_km: float = Field(550.0, ge=300, le=1200)
    fc_mhz: float = Field(915.0, ge=400, le=1000)
    max_elev_deg: float = Field(60.0, ge=15, le=90)
    pass_phase: float = Field(-0.34, ge=-0.98, le=0.98)
    min_elev_deg: float = Field(10.0, ge=0, le=30)
    cap_radius_km: float = Field(600.0, ge=0, le=1200)
    osc_ppm: float = Field(2.0, ge=0, le=20)
    power_model: Literal["equal", "spread", "link"] = "link"
    snr_db: float = Field(-6.0, ge=-20, le=10)
    spread_db: float = Field(10.0, ge=0, le=30)
    eirp_dbm: float = Field(22.0, ge=0, le=30)
    rx_gain_dbi: float = Field(6.0, ge=-3, le=20)
    nf_db: float = Field(3.0, ge=0, le=10)
    shadow_db: float = Field(4.0, ge=0, le=10)
    cfo_span_khz: float = Field(25.0, ge=1, le=40)
    bulk_comp: bool = True
    rs_k: int = Field(12, ge=1, le=16)
    seed: int = Field(0, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _consistent(self):
        n_codes = uplink.code_pool(self.family).shape[0]
        if self.modulation == "codehop" and self.signature == "distinct" and self.n_users > n_codes:
            raise ValueError(f"{self.family} has only {n_codes} codes")
        self.rs_k = min(self.rs_k, self.n_data)
        return self

    def to_uplink(self) -> uplink.UplinkConfig:
        # the receiver is always blind: it is given no side information
        return uplink.UplinkConfig.from_dict({**self.model_dump(), "acquisition": "blind", "cfo_mode": "pilot"})


class StageReq(BaseModel):
    cfg: Cfg
    stage: int = Field(0, ge=0, le=100_000)
    view: Literal["noisy", "clean"] = "noisy"


class CorrReq(BaseModel):
    cfg: Cfg
    j: int = Field(..., ge=0)
    rx: Literal["sic", "tin"] = "sic"


class SweepReq(BaseModel):
    cfg: Cfg
    k_grid: list[int] = Field(..., min_length=1, max_length=16)
    frames_per_k: int = Field(5, ge=1, le=20)


# ----------------------------------------------------------------------------
# cost guard
# ----------------------------------------------------------------------------

FRAME_COST_CAP = 8.0e7          # decoding work (correlations x samples)
SEARCH_COST_CAP = 1.5e9         # blind search work (starts x codes x template x pilots)
SWEEP_LOAD_CAP = 10.0           # a sweep may cost this many frames at the cap
SWEEP_GRID = [8, 16, 32, 48, 64, 96]


def frame_cost(cfg: uplink.UplinkConfig, k: int | None = None) -> float:
    d = uplink.derived(cfg)
    k = cfg.n_users if k is None else k
    collide = 1.0 + k / max(cfg.n_channels, 1) * 0.5
    return k * d["n_blocks"] * d["block_len"] * collide + d["n_blocks"] * d["slot_len"] * 4


def search_cost(cfg: uplink.UplinkConfig) -> float:
    """Work of one blind search round (it does not depend on K)."""
    d = uplink.derived(cfg)
    n_cap = d["block_len"] + int(round(uplink.arrival_window_s(cfg, d) * d["fs"])) + d["n_blocks"] * d["slot_len"]
    g = acquire.make_grid(cfg, d, n_cap)
    units = cfg.n_channels if uplink.is_lora(cfg) else d["n_codes"]
    return float(g.W * units * g.Lt * cfg.n_pilot)


def frame_load(cfg: uplink.UplinkConfig, k: int | None = None) -> float:
    """Frame cost as a fraction of what the public server allows (1 = cap)."""
    return frame_cost(cfg, k) / FRAME_COST_CAP + search_cost(cfg) / SEARCH_COST_CAP


def check_load(load: float, cap: float, what: str):
    if load > cap:
        raise HTTPException(422, f"{what} too heavy ({load / cap:.0%} of the cap)")


# ----------------------------------------------------------------------------
# frame cache
# ----------------------------------------------------------------------------

class FrameCache:
    """Frames are deterministic in (config, seed), so the cache is keyed by the
    config hash and a miss is simply recomputed.  Any worker can therefore
    serve any request; no sticky sessions are needed."""

    def __init__(self, size: int = 24):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(c: uplink.UplinkConfig) -> str:
        return hashlib.sha1(json.dumps(c.to_dict(), sort_keys=True).encode()).hexdigest()[:16]

    def get(self, c: uplink.UplinkConfig) -> tuple[str, dict, bool]:
        k = self.key(c)
        with self._lock:
            e = self._d.get(k)
            if e is not None:
                self._d.move_to_end(k)
                return k, e, True
        fr = uplink.run_frame(c, keep_clean=True)
        spec = spec_payload(fr.rx, fr.scn.d)
        spec_clean = spec_payload(fr.clean, fr.scn.d)
        e = {"frame": fr, "lock": threading.Lock(), "spec": spec, "spec_clean": spec_clean,
             "range": {"noisy": (spec["db_floor"], spec["db_ceil"]),
                       "clean": (spec_clean["db_floor"], spec_clean["db_ceil"])},
             "cursor": {"noisy": (0, fr.rx), "clean": (0, fr.clean)}}
        with self._lock:
            self._d[k] = e
            while len(self._d) > self.size:
                self._d.popitem(last=False)
        return k, e, False


CACHE = FrameCache()


# ----------------------------------------------------------------------------
# payload helpers
# ----------------------------------------------------------------------------

def spec_payload(x, d, floor=None, ceil=None) -> dict:
    p_db, t, f = uplink.spectrogram(x, d["fs"], d["sps"])
    if ceil is None:
        ceil = float(np.percentile(p_db, 99.7)) + 3.0
        floor = ceil - 60.0
    q = np.clip((p_db - floor) / (ceil - floor) * 255.0, 0, 255).astype(np.uint8)
    return {"n_f": int(q.shape[0]), "n_t": int(q.shape[1]),
            "t0_ms": float(t[0] * 1e3), "t1_ms": float(t[-1] * 1e3),
            "f0_khz": float(f[0] / 1e3), "f1_khz": float(f[-1] / 1e3),
            "db_floor": floor, "db_ceil": ceil, "db_med": float(np.median(p_db)),
            "data": base64.b64encode(q.tobytes()).decode()}


def r(x, nd=3):
    return float(round(float(x), nd))


def frame_payload(fr: uplink.Frame, fid: str, spec: dict, elapsed: float) -> dict:
    scn, d, cfg = fr.scn, fr.scn.d, fr.cfg
    B, P = d["n_blocks"], cfg.n_pilot
    chf = np.asarray(d["channel_freqs_hz"])
    fs, L = d["fs"], d["block_len"]
    sm, tm = fr.sic_match, fr.tin_match
    p_mid = (P - 1) / 2.0
    tau_mid = (p_mid * d["slot_len"] + L / 2.0) / fs
    truth_mid = scn.doppler_hz + scn.osc_hz + scn.rate_hz_s * tau_mid

    users = []
    for u in range(scn.k):
        users.append({
            "id": u, "code": int(scn.codes[u]), "hop": int(scn.hops[u]), "ch": int(scn.channels[u, 0]),
            "x_km": r(scn.map_xy[u, 0], 1), "y_km": r(scn.map_xy[u, 1], 1),
            "elev_deg": r(scn.geo["elev_deg"][u], 2), "slant_km": r(scn.geo["slant_m"][u] / 1e3, 1),
            "doppler_hz": r(scn.doppler_hz[u], 1), "rate_hz_s": r(scn.rate_hz_s[u], 2),
            "osc_hz": r(scn.osc_hz[u], 1), "arrival_us": r((scn.arrival[u] - scn.arrival.min()) / fs * 1e6, 1),
            "snr_db": r(scn.snr_db[u], 2),
            "det": bool(sm[u] >= 0), "tin_det": bool(tm[u] >= 0),
            "cfo_err_hz": r(fr.f_est[sm[u]] - truth_mid[u], 1) if sm[u] >= 0 else None,
            "dt_us": r((fr.sic_view.arrival[sm[u]] - scn.arrival[u]) / fs * 1e6, 2) if sm[u] >= 0 else None,
            "sic_ok": int(fr.sic_score["sym_ok"][u].sum()), "tin_ok": int(fr.tin_score["sym_ok"][u].sum()),
            "sic_pkt": bool(fr.sic_score["pkt_ok"][u]), "tin_pkt": bool(fr.tin_score["pkt_ok"][u]),
        })

    blocks = []
    for u in range(scn.k):
        js, jt = int(sm[u]), int(tm[u])
        for b in range(B):
            j = u * B + b
            t0 = scn.block_start(u, b) / fs * 1e3
            data = b >= P
            blocks.append({
                "j": j, "u": u, "b": b, "kind": "data" if data else "pilot",
                "ch": int(scn.channels[u, b]),
                "t0": r(t0, 4), "t1": r(t0 + L / fs * 1e3, 4),
                "f": r((chf[scn.channels[u, b]] + scn.true_offset(u, b)) / 1e3, 3),
                "true": int(scn.symbols[u, b]),
                "miss": js < 0, "tin_miss": jt < 0,
                "sic": int(fr.sic.decided[js, b]) if js >= 0 else None,
                "tin": int(fr.tin.decided[jt, b]) if data and jt >= 0 else None,
                "step": int(fr.sic.step[js, b]) if js >= 0 else -1,
                "sic_m": r(fr.sic.margin_db[js, b], 2) if js >= 0 else None,
                "tin_m": r(fr.tin.margin_db[jt, b], 2) if data and jt >= 0 else None,
            })

    # pass overview
    frame = footprint.PassFrame(cfg.altitude_km * 1e3, cfg.max_elev_deg)
    t_half = d["t_half_s"]
    t_grid = np.linspace(-t_half, t_half, 161) if t_half > 0 else np.array([0.0])
    centre = footprint.cap_from_polar(frame, [0.0], [0.0])[0]
    dc = footprint.doppler_curves(frame, centre, t_grid, cfg.fc_mhz * 1e6)[0]
    du = footprint.doppler_curves(frame, scn.geo["pos"], t_grid, cfg.fc_mhz * 1e6)
    el = [float(footprint.link_state(frame, centre, t, cfg.fc_mhz * 1e6)["elev_deg"][0]) for t in t_grid[::4]]
    track_t = np.linspace(-1.6 * max(t_half, 60.0), 1.6 * max(t_half, 60.0), 81)
    g_cov = footprint.central_angle_for_elevation(cfg.min_elev_deg, cfg.altitude_km * 1e3)
    s_hat, _, _ = frame.sat_state(np.atleast_1d(d["t_now_s"]))
    s_hat = s_hat[0] / np.linalg.norm(s_hat[0])
    e1 = np.cross(s_hat, [0.0, 0.0, 1.0] if abs(s_hat[2]) < 0.9 else [1.0, 0.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(s_hat, e1)
    phi = np.linspace(0.0, 2.0 * np.pi, 121)[:, None]
    rings = []
    for ring_el in sorted({float(cfg.min_elev_deg), 20.0, 30.0, 45.0, 60.0, 75.0}):
        if ring_el < cfg.min_elev_deg:
            continue
        g = footprint.central_angle_for_elevation(ring_el, cfg.altitude_km * 1e3)
        pts = np.cos(g) * s_hat + np.sin(g) * (np.cos(phi) * e1 + np.sin(phi) * e2)
        rings.append({"elev": ring_el, "pts": footprint.to_map(frame, pts).round(1).tolist()})

    def kpi(sc):
        return {"raw": sc["raw_recovery"], "pkt": sc["pkt_success"],
                "sym_correct": sc["sym_correct"], "sym_total": sc["sym_total"],
                "pkt_ok": int(sc["pkt_ok"].sum()), "users": scn.k,
                "det": sc["n_detected"], "false": sc["n_false"]}

    return {
        "frame_id": fid,
        "cfg": cfg.to_dict(),
        "derived": {k: v for k, v in d.items() if isinstance(v, (int, float, str, list))},
        "kpi": {"sic": kpi(fr.sic_score), "tin": kpi(fr.tin_score)},
        "n_steps": len(fr.sic.peel),
        "users": users,
        "blocks": blocks,
        "spec": spec,
        "pass": {"t_s": [r(t, 2) for t in t_grid], "centre_khz": [r(x / 1e3) for x in dc],
                 "lo_khz": [r(x / 1e3) for x in du.min(axis=0)],
                 "hi_khz": [r(x / 1e3) for x in du.max(axis=0)],
                 "elev_t_s": [r(t, 2) for t in t_grid[::4]], "elev_deg": [r(e, 2) for e in el],
                 "t_now_s": d["t_now_s"]},
        "footprint": {"track": footprint.subsatellite_map(frame, track_t).round(1).tolist(),
                      "subsat": footprint.subsatellite_map(frame, d["t_now_s"])[0].round(1).tolist(),
                      "coverage_km": g_cov * footprint.RE / 1e3, "rings": rings,
                      "cap_km": cfg.cap_radius_km},
        "evals": fr.sic.n_evals,
        "elapsed_ms": round(elapsed * 1e3, 1),
    }


# ----------------------------------------------------------------------------
# app
# ----------------------------------------------------------------------------

app = FastAPI(title="CodeHop uplink demo", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.get("/api/schema")
def schema():
    knobs = [{"group": g, "name": n, "label": l, "kind": k, **x} for g, n, l, k, x in KNOBS]
    defaults = Cfg().model_dump()
    return {"knobs": knobs, "defaults": defaults, "k_max": K_MAX,
            "sweep_grid": SWEEP_GRID, "families": uplink.FAMILIES}


@app.post("/api/derived")
def derived_ep(cfg: Cfg):
    c = cfg.to_uplink()
    d = uplink.derived(c)
    out = {k: v for k, v in d.items() if isinstance(v, (int, float, str, list))}
    out["load"] = frame_load(c)
    return out


@app.post("/api/frame")
def frame_ep(cfg: Cfg):
    c = cfg.to_uplink()
    t0 = time.perf_counter()
    fid, e = cached_frame(c)
    out = frame_payload(e["frame"], fid, e["spec"], time.perf_counter() - t0)
    out["spec_clean"] = e["spec_clean"]
    return out


@app.post("/api/kpi")
def kpi_ep(cfg: Cfg):
    """Decode results only (the UI uses it to show the other waveform on the
    same nodes).  The frame is cached, so switching to it afterwards is free."""
    c = cfg.to_uplink()
    _, e = cached_frame(c)
    fr: uplink.Frame = e["frame"]
    out = {}
    for name, sc in (("sic", fr.sic_score), ("tin", fr.tin_score)):
        out[name] = {"raw": sc["raw_recovery"], "pkt": sc["pkt_success"],
                     "pkt_ok": int(sc["pkt_ok"].sum()), "users": fr.scn.k,
                     "det": sc["n_detected"], "false": sc["n_false"]}
    return {"cfg": c.to_dict(), "kpi": out}


def cached_frame(c: uplink.UplinkConfig) -> tuple[str, dict]:
    check_load(frame_load(c), 1.0, "frame")
    try:
        fid, e, _ = CACHE.get(c)
    except ValueError as err:
        raise HTTPException(422, str(err))
    return fid, e


@app.post("/api/stage")
def stage_ep(req: StageReq):
    """Spectrogram after the first ``stage`` SIC subtractions."""
    _, e = cached_frame(req.cfg.to_uplink())
    fr: uplink.Frame = e["frame"]
    view = req.view
    stage = max(0, min(int(req.stage), len(fr.sic.peel)))
    base = fr.clean if view == "clean" else fr.rx
    with e["lock"]:
        s0, res = e["cursor"][view]
        if stage < s0:
            s0, res = 0, base
        res = res.copy()
        fs = fr.scn.d["fs"]
        for entry in fr.sic.peel[s0:stage]:
            start, w = uplink.reconstruct(entry, fr.wf, fs)
            res[start:start + len(w)] -= w
        e["cursor"][view] = (stage, res)
    floor, ceil = e["range"][view]
    return {"stage": stage, "view": view, "spec": spec_payload(res, fr.scn.d, floor, ceil)}


@app.post("/api/corr")
def corr_ep(req: CorrReq):
    """|correlation| profile of one block at the moment it was decided."""
    _, e = cached_frame(req.cfg.to_uplink())
    fr: uplink.Frame = e["frame"]
    j, rx = req.j, req.rx
    B = fr.scn.d["n_blocks"]
    if not 0 <= j < fr.scn.k * B:
        raise HTTPException(404, "no such block")
    u, b = divmod(j, B)
    res = fr.sic if rx == "sic" else fr.tin
    h = int((fr.sic_match if rx == "sic" else fr.tin_match)[u])
    if h < 0:
        raise HTTPException(404, "not detected")
    if res.profiles is None or (rx == "tin" and b < fr.cfg.n_pilot):
        raise HTTPException(404, "no profile")
    prof = res.profiles[h * B + b]
    return {"j": j, "u": u, "b": b, "rx": rx, "profile": [r(x, 4) for x in prof],
            "true": int(fr.scn.symbols[u, b]), "decided": int(res.decided[h, b]),
            "pilot": b < fr.cfg.n_pilot, "step": int(res.step[h, b]),
            "margin_db": r(res.margin_db[h, b], 2),
            "noise_rms": r(math.sqrt(1.0 / fr.scn.d["n"]) , 4)}


@app.post("/api/sweep")
def sweep_ep(req: SweepReq):
    c = req.cfg.to_uplink()
    grid = sorted({int(k) for k in req.k_grid if 1 <= int(k) <= K_MAX})
    if not grid:
        raise HTTPException(422, "empty K grid")
    n_codes = uplink.code_pool(c.family).shape[0]
    if c.modulation == "codehop" and c.signature == "distinct" and max(grid) > n_codes:
        raise HTTPException(422, f"{c.family} has only {n_codes} codes")
    total = sum(frame_load(c, k) for k in grid) * req.frames_per_k
    check_load(total, SWEEP_LOAD_CAP, "sweep")

    def gen():
        t0 = time.perf_counter()
        try:
            for msg in uplink.sweep_k(c, grid, req.frames_per_k):
                msg["elapsed_s"] = round(time.perf_counter() - t0, 2)
                yield json.dumps(msg) + "\n"
        except ValueError as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


if __name__ == "__main__":
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port)
