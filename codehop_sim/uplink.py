"""Wideband CodeHop uplink: footprint -> hopping Gold-CCSK blocks -> SDR capture
-> conditional receiver (TIN or ordered SIC).

This module backs the interactive demo (``server.py``).  It composes the existing
Level-2 pieces rather than replacing them:

* codes      : ``codes.gold_family`` / ``codes.ccsk_shift`` / ``codes.random_prn``
* waveform   : ``waveform.gfsk_waveform`` (constant-envelope GFSK/MSK, h = 0.5)
* SNR/noise  : the ``paper_tasks.task_k_seed`` convention -- SNR is per user in
               the chip-rate bandwidth; complex noise power per sample = S
               (samples/chip) for a unit-amplitude 0 dB user
* SIC rule   : the ``task_k_seed`` rule -- peel the remaining component with the
               largest |correlation| peak, decide its cyclic shift by argmax,
               subtract the exact template with a complex least-squares gain

What it adds is the satellite context: per-terminal Doppler, Doppler rate and
delay from ``footprint``; frequency hopping over C channels; a pilot/data block
structure; a wideband capture that can be shown as a spectrogram; and receiver
CFO handling.

Baseline: ``modulation="lora"`` swaps the waveform for LoRa-style chirp spread
spectrum on the same channel grid, link budget, geometry and receiver.  A LoRa
symbol is a cyclic shift of one up-chirp (2^SF chips over BW), so dechirp+FFT
demodulation is exactly the all-shifts correlation used for CCSK.  The
differences are the ones that matter for multi-access: every node uses the
*same* chirp (no per-node code) and stays on one channel for the whole packet;
the preamble is plain up-chirps; phase is continuous across the packet.  LoRa's
header, Hamming coding, interleaving and Gray mapping are not modelled; both
waveforms share the illustrative RS(D, k) packet metric.

Receiver side information.  With ``acquisition="known"`` (the paper's
Section 4 regime) the receiver is *given* the active set, each terminal's code,
hop pattern and timing; CFO is either a genie with Gaussian residual (default
sigma = 196.85 Hz) or estimated from the pilot blocks.  With
``acquisition="blind"`` (``acquire.py``) it is given none of these: it searches
every code over timing and carrier offset, identifies hop patterns from the
public pool, and repeats the search on the SIC residual.  The transmitted
waveform is the same in both modes.  There is no packet format or real FEC;
the "packet" metric is a bounded-distance criterion for an illustrative
RS(D, k) code.
"""
from __future__ import annotations

import functools
import math
from dataclasses import asdict, dataclass, fields, replace

import numpy as np
from scipy import fft as sfft

from . import codes, footprint, waveform
from .channel import thermal_noise_dbm
from .params import PHYParams
from .pass_geometry import fspl_db

PAPER_CFO_SIGMA_HZ = 196.85
POOL_SEED = 20261102            # seed of the *public* PRN-code and hop pools
V_SAT_MAX = 7.8e3               # m/s, bound on |range-rate| for any LEO pass

FAMILIES = {
    "gold63": {"label": "Gold-63", "kind": "gold", "degree": 6, "decimation": 5},
    "gold127": {"label": "Gold-127", "kind": "gold", "degree": 7, "decimation": 3},
    "prn255": {"label": "PRN-255", "kind": "prn", "n": 255, "size": 256},
    "gold511": {"label": "Gold-511", "kind": "gold", "degree": 9, "decimation": 3},
}


# ----------------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------------

@dataclass
class UplinkConfig:
    # nodes / access
    n_users: int = 8
    signature: str = "distinct"     # "distinct" codes (paper) | "random" iid draws
    arrival: str = "aligned"        # "aligned" | "beacon" | "random"
    arrival_window_ms: float = 10.0
    # packet
    n_pilot: int = 4
    n_data: int = 8
    guard_us: float = 0.0
    # PHY
    modulation: str = "codehop"     # "codehop" | "lora" (baseline)
    family: str = "gold127"
    chip_rate: float = 100e3
    mod_index: float = 0.5
    bt: float = 0.5
    n_channels: int = 1
    spacing_khz: float = 200.0      # channel grid (shared by both modulations)
    hop_pool: int = 64
    lora_sf: int = 7                # LoRa spreading factor: 2^SF chips per symbol
    lora_bw_khz: float = 125.0      # LoRa bandwidth = chip rate
    # satellite pass
    altitude_km: float = 550.0
    fc_mhz: float = 915.0
    max_elev_deg: float = 60.0
    pass_phase: float = 0.0         # -1 = rise (min_elev), 0 = closest approach, +1 = set
    min_elev_deg: float = 10.0
    cap_radius_km: float = 0.0      # 0 = co-located nodes (paper Fig. 4a regime)
    osc_ppm: float = 0.0
    # power
    power_model: str = "equal"      # "equal" | "spread" | "link"
    snr_db: float = -6.0
    spread_db: float = 10.0
    eirp_dbm: float = 14.0
    rx_gain_dbi: float = 6.0
    nf_db: float = 3.0
    shadow_db: float = 0.0
    # receiver
    cfo_mode: str = "genie"         # "genie" (paper) | "pilot" (exploratory)
    cfo_sigma_hz: float = PAPER_CFO_SIGMA_HZ
    cfo_span_khz: float = 15.0
    bulk_comp: bool = True
    acquisition: str = "known"      # "known" (paper: active set, codes, hops, timing given) | "blind"
    rs_k: int = 8                   # RS(n_data, rs_k); rs_k == n_data -> uncoded
    seed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UplinkConfig":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# ----------------------------------------------------------------------------
# public pools
# ----------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def code_pool(family: str) -> np.ndarray:
    spec = FAMILIES[family]
    if spec["kind"] == "gold":
        fam = codes.gold_family(spec["degree"], spec["decimation"])
        return np.asarray(fam, dtype=float)
    rng = np.random.default_rng([POOL_SEED, spec["n"]])
    return np.asarray([codes.random_prn(spec["n"], rng) for _ in range(spec["size"])])


@functools.lru_cache(maxsize=32)
def hop_patterns(pool_size: int, n_blocks: int, n_channels: int) -> np.ndarray:
    """Public hop-pattern pool: ``pool_size`` iid channel sequences."""
    rng = np.random.default_rng([POOL_SEED, pool_size, n_blocks, n_channels])
    return rng.integers(0, n_channels, size=(pool_size, n_blocks))


@functools.lru_cache(maxsize=32)
def pilot_channels(n_codes: int, n_pilot: int, n_channels: int) -> np.ndarray:
    """Public pilot hop sequence per code: pilot b of a CodeHop node with code
    c is sent on channel ``pilot_channels(...)[c, b]``; its data blocks follow
    the node's own hop pattern.  Keying the pilot channels to the code lets a
    receiver that does not know the active set search each code on one
    channel per pilot, and makes cross-code aliases collect energy only where
    two codes' pilot channels coincide."""
    rng = np.random.default_rng([POOL_SEED, 1, n_codes, n_pilot, n_channels])
    return rng.integers(0, n_channels, size=(n_codes, n_pilot))


def slant_bounds(cfg: UplinkConfig, d: dict) -> tuple[float, float]:
    """Smallest and largest slant range [m] over the visible part of the node
    region.  Depends only on geometry, so a receiver can compute it."""
    frame = footprint.PassFrame(cfg.altitude_km * 1e3, cfg.max_elev_deg)
    beta = max(cfg.cap_radius_km, 0.0) * 1e3 / footprint.RE
    th, ph = np.meshgrid(np.linspace(0.0, beta, 9), np.linspace(0.0, 2 * np.pi, 36, endpoint=False))
    pos, _ = footprint.cap_from_polar(frame, th.ravel(), ph.ravel())
    ls = footprint.link_state(frame, pos, d["t_now_s"], cfg.fc_mhz * 1e6)
    keep = ls["elev_deg"] >= cfg.min_elev_deg
    r = ls["slant_m"][keep] if keep.any() else ls["slant_m"]
    return float(r.min()), float(r.max())


def arrival_window_s(cfg: UplinkConfig, d: dict) -> float:
    """Largest possible arrival offset [s] under the access protocol: the
    receiver searches this window (plus margins) for packet starts."""
    if cfg.arrival == "aligned":
        return 0.0
    r_lo, r_hi = slant_bounds(cfg, d)
    if cfg.arrival == "beacon":
        return 2.0 * (r_hi - r_lo) / 2.998e8
    return cfg.arrival_window_ms * 1e-3 + (r_hi - r_lo) / 2.998e8


def pilot_shifts(n: int, n_pilot: int) -> np.ndarray:
    """Public cover shifts for the pilot blocks (break the N-chip ambiguity)."""
    return np.array([(j * (n // 4) + j * j) % n for j in range(n_pilot)], dtype=int)


def largest_prime_le(n: int) -> int:
    for p in range(n, 1, -1):
        if all(p % q for q in range(2, int(p ** 0.5) + 1)):
            return p
    return 2


def p_signature_collision(k: int, m: int) -> float:
    """Paper Eq. (1): P(any two of K uniform draws from M signatures collide)."""
    if m <= 0:
        return 1.0
    p_ok = 1.0
    for i in range(int(k)):
        p_ok *= max(0.0, 1.0 - i / m)
    return 1.0 - p_ok


# ----------------------------------------------------------------------------
# derived quantities
# ----------------------------------------------------------------------------

def is_lora(cfg: UplinkConfig) -> bool:
    return cfg.modulation == "lora"


def derived(cfg: UplinkConfig) -> dict:
    if is_lora(cfg):
        n_codes, n = 1, 1 << int(cfg.lora_sf)
        rc = float(cfg.lora_bw_khz) * 1e3
        label = f"LoRa SF{int(cfg.lora_sf)}/{cfg.lora_bw_khz:g} kHz"
    else:
        n_codes, n = code_pool(cfg.family).shape
        rc = float(cfg.chip_rate)
        label = FAMILIES[cfg.family]["label"]
    spacing = cfg.spacing_khz * 1e3
    fc = cfg.fc_mhz * 1e6
    offset_bound = V_SAT_MAX * fc / 2.998e8 + cfg.osc_ppm * 1e-6 * fc
    span = (cfg.n_channels - 1) * spacing + 2.0 * rc + 2.0 * offset_bound
    s = max(4, 4 * math.ceil(span / rc / 4.0))
    fs = s * rc
    L = n * s
    # LoRa never retunes inside a packet, so its symbols are back to back
    guard = 0 if is_lora(cfg) else int(round(cfg.guard_us * 1e-6 * fs))
    n_blocks = cfg.n_pilot + cfg.n_data
    t_block = n / rc
    frame = footprint.PassFrame(cfg.altitude_km * 1e3, cfg.max_elev_deg)
    t_half = footprint.visibility_half_window(frame, cfg.min_elev_deg)
    t_now = cfg.pass_phase * t_half
    centre = footprint.link_state(frame, footprint.cap_from_polar(frame, [0.0], [0.0])[0],
                                  t_now, fc)
    rs_k = int(min(max(cfg.rs_k, 1), cfg.n_data))
    p_field = largest_prime_le(n)
    centre_snr = float(cfg.eirp_dbm + cfg.rx_gain_dbi - fspl_db(centre["slant_m"][0], fc)
                       - thermal_noise_dbm(rc, cfg.nf_db))
    return {
        "modulation": "lora" if is_lora(cfg) else "codehop",
        "n": n, "n_codes": n_codes, "family_label": label,
        "rc": rc, "deviation_hz": 0.0 if is_lora(cfg) else cfg.mod_index * rc / 2.0,
        "sps": s, "fs": fs,
        "block_len": L, "guard_samples": guard, "slot_len": L + guard,
        "n_blocks": n_blocks, "t_block_s": t_block,
        "t_packet_s": n_blocks * (L + guard) / fs,
        "proc_gain_db": 10.0 * math.log10(n),
        "cfo_scale_hz": 1.0 / (4.0 * t_block),
        "bits_per_symbol": math.log2(n),
        "channel_freqs_hz": [(c - (cfg.n_channels - 1) / 2.0) * spacing
                             for c in range(cfg.n_channels)],
        "offset_bound_hz": offset_bound,
        "t_half_s": t_half, "t_now_s": t_now,
        "centre_elev_deg": float(centre["elev_deg"][0]),
        "centre_slant_km": float(centre["slant_m"][0] / 1e3),
        "centre_doppler_hz": float(centre["doppler_hz"][0]),
        "centre_rate_hz_s": float(centre["doppler_rate_hz_s"][0]),
        "centre_snr_db": centre_snr,
        "rs_k": rs_k, "rs_t": (cfg.n_data - rs_k) // 2, "rs_field": p_field,
        "info_bits": rs_k * math.log2(p_field),
        # LoRa: everyone shares the chirp, so a "signature" is just the channel
        "p_coll_joint": p_signature_collision(
            cfg.n_users, cfg.n_channels if is_lora(cfg) else n_codes * cfg.hop_pool),
        "p_coll_code": p_signature_collision(cfg.n_users, n_codes),
    }


# ----------------------------------------------------------------------------
# waveform cache
# ----------------------------------------------------------------------------

def lora_upchirp(n: int, sps: int) -> np.ndarray:
    """Base LoRa up-chirp: N = 2^SF chips at ``sps`` samples/chip, sweeping
    -BW/2 -> +BW/2 over one symbol.  Its phase returns to 0 at the symbol end,
    so every cyclic shift is phase-continuous (the frequency wrap of a LoRa
    symbol)."""
    L = n * sps
    x = np.arange(L) / L                              # time in symbols
    return np.exp(2j * np.pi * (n / 2.0) * (x * x - x))


class Waveforms:
    """Per-configuration templates (GFSK-CCSK or LoRa chirps) and correlation
    FFTs.  Symbol a is the base waveform cyclically shifted right by a chips."""

    def __init__(self, cfg: UplinkConfig, d: dict):
        self.lora = is_lora(cfg)
        self.n = d["n"]
        self.s = d["sps"]
        self.L = d["block_len"]
        if self.lora:
            self.chirp = lora_upchirp(self.n, self.s)
        else:
            self.pool = code_pool(cfg.family)
            self.phy = PHYParams(chip_rate=d["rc"], deviation=d["deviation_hz"],
                                 bt=cfg.bt if cfg.bt > 0 else None)
        self.m_fft = sfft.next_fast_len(2 * self.n)
        self._blocks: dict = {}
        self._dfft: dict = {}
        self.shift_index = (self.n - np.arange(self.n)) % self.n

    def block(self, code_idx: int, symbol: int) -> np.ndarray:
        key = (int(code_idx), int(symbol) % self.n)
        w = self._blocks.get(key)
        if w is None:
            if self.lora:
                w = np.roll(self.chirp, key[1] * self.s)
            else:
                chips = codes.ccsk_shift(self.pool[key[0]], key[1])
                w = waveform.gfsk_waveform(chips, self.phy, sps=self.s)
            if len(self._blocks) < 4096:
                self._blocks[key] = w
        return w

    def doubled_fft(self, code_idx: int) -> np.ndarray:
        """FFT of the polyphase components of the doubled-code waveform.

        Window starting at chip s of gfsk([c, c]) equals the waveform of
        roll(c, (N - s) mod N) up to a constant phase (and Gaussian edge
        effects over ~1 chip), so one correlation scores all N shifts."""
        x = self._dfft.get(code_idx)
        if x is None:
            if self.lora:
                x2 = np.concatenate([self.chirp, self.chirp])
            else:
                c = self.pool[int(code_idx)]
                x2 = waveform.gfsk_waveform(np.concatenate([c, c]), self.phy, sps=self.s)
            poly = x2.reshape(2 * self.n, self.s).T              # (S, 2N)
            x = sfft.fft(poly, n=self.m_fft, axis=-1)
            self._dfft[code_idx] = x
        return x

    def correlate(self, windows: np.ndarray, code_idx: np.ndarray) -> np.ndarray:
        """|correlation| against every cyclic shift.  windows: (J, L) already
        mixed to baseband.  Returns (J, N) magnitudes indexed by symbol."""
        j = windows.shape[0]
        poly = windows.reshape(j, self.n, self.s).transpose(0, 2, 1)
        wf = sfft.fft(poly, n=self.m_fft, axis=-1)                # (J, S, M)
        x2 = np.stack([self.doubled_fft(int(c)) for c in code_idx])
        h = sfft.ifft(np.einsum("jsm,jsm->jm", x2, wf.conj()), axis=-1)[:, :self.n]
        return np.abs(h)[:, self.shift_index]


# ----------------------------------------------------------------------------
# scenario
# ----------------------------------------------------------------------------

@dataclass
class Scenario:
    cfg: UplinkConfig
    d: dict
    codes: np.ndarray          # (K,) code index
    hops: np.ndarray           # (K,) hop-pattern index
    channels: np.ndarray       # (K, B) channel index per block
    symbols: np.ndarray        # (K, B) cyclic shift per block (pilots public)
    phases: np.ndarray         # (K, B) carrier phase after each retune
    snr_db: np.ndarray         # (K,)
    doppler_hz: np.ndarray     # (K,) at packet start
    rate_hz_s: np.ndarray      # (K,)
    osc_hz: np.ndarray         # (K,)
    arrival: np.ndarray        # (K,) start sample in the capture
    geo: dict                  # per-user geometry arrays
    map_xy: np.ndarray         # (K, 2) km
    n_samples: int
    noise: np.ndarray | None = None
    cfo_err: np.ndarray | None = None  # (K,) unit-normal draws for the genie residual

    @property
    def k(self) -> int:
        return len(self.codes)

    def block_start(self, u: int, b: int) -> int:
        return int(self.arrival[u] + b * self.d["slot_len"])

    def true_offset(self, u: int, b: int) -> float:
        """Carrier offset [Hz] at the centre of block b (Doppler + LO)."""
        tau = (b * self.d["slot_len"] + self.d["block_len"] / 2.0) / self.d["fs"]
        return float(self.doppler_hz[u] + self.rate_hz_s[u] * tau + self.osc_hz[u])


def build_scenario(cfg: UplinkConfig, rng: np.random.Generator | None = None) -> Scenario:
    d = derived(cfg)
    if rng is None:
        rng = np.random.default_rng(cfg.seed)
    # one stream per quantity: switching modulation (or code family) redraws
    # only the symbols, so both waveforms see the same nodes, powers and timing
    r_pos, r_sig, r_hop, r_sym, r_phase, r_pow, r_osc, r_arr, r_cfo = rng.spawn(9)
    lora = is_lora(cfg)
    k, n, B = cfg.n_users, d["n"], d["n_blocks"]
    fc = cfg.fc_mhz * 1e6
    frame = footprint.PassFrame(cfg.altitude_km * 1e3, cfg.max_elev_deg)
    t_now = d["t_now_s"]

    # terminals: uniform in the cap, restricted to those above min elevation
    pos = np.zeros((0, 3))
    xy = np.zeros((0, 2))
    for _ in range(200):
        p, q = footprint.cap_points(frame, 4 * k + 8, cfg.cap_radius_km * 1e3, r_pos)
        el = footprint.link_state(frame, p, t_now, fc)["elev_deg"]
        keep = el >= cfg.min_elev_deg
        pos = np.vstack([pos, p[keep]])
        xy = np.vstack([xy, q[keep]])
        if len(pos) >= k:
            break
    if len(pos) < k:
        raise ValueError("cap is not visible above the minimum elevation")
    pos, xy = pos[:k], xy[:k]
    geo = footprint.link_state(frame, pos, t_now, fc)
    geo["pos"] = pos

    # signatures
    n_codes = d["n_codes"]
    if lora:
        code_idx = np.zeros(k, dtype=int)                # one shared chirp
    elif cfg.signature == "distinct":
        if k > n_codes:
            raise ValueError("more users than distinct codes")
        code_idx = r_sig.choice(n_codes, size=k, replace=False)
    else:
        code_idx = r_sig.integers(0, n_codes, size=k)
    hop_idx = r_hop.integers(0, cfg.hop_pool, size=k)
    channels = hop_patterns(cfg.hop_pool, B, cfg.n_channels)[hop_idx]
    if lora:                                             # one channel per packet
        channels = np.repeat(channels[:, :1], B, axis=1)
    else:                                                # pilots hop on the code's public sequence
        channels[:, :cfg.n_pilot] = pilot_channels(n_codes, cfg.n_pilot, cfg.n_channels)[code_idx]

    symbols = np.empty((k, B), dtype=int)
    symbols[:, :cfg.n_pilot] = 0 if lora else pilot_shifts(n, cfg.n_pilot)[None, :]
    symbols[:, cfg.n_pilot:] = r_sym.integers(0, n, size=(k, cfg.n_data))
    phases = r_phase.uniform(0.0, 2.0 * np.pi, size=(k, B))
    if lora:                                             # no retune: continuous phase
        phases[:] = phases[:, :1]

    # received power
    if cfg.power_model == "equal":
        snr = np.full(k, cfg.snr_db)
    elif cfg.power_model == "spread":
        snr = cfg.snr_db + r_pow.uniform(0.0, cfg.spread_db, size=k)
    else:
        noise_dbm = thermal_noise_dbm(d["rc"], cfg.nf_db)
        prx = cfg.eirp_dbm + cfg.rx_gain_dbi - fspl_db(geo["slant_m"], fc)
        if cfg.shadow_db > 0:
            prx = prx - np.abs(r_pow.normal(0.0, cfg.shadow_db, size=k))
        snr = prx - noise_dbm

    osc = r_osc.uniform(-1.0, 1.0, size=k) * cfg.osc_ppm * 1e-6 * fc

    # arrival offsets, relative to the earliest possible arrival from the
    # region (geometry only), so the capture window reveals nothing about
    # where the actual nodes are
    fs = d["fs"]
    r = geo["slant_m"]
    r_lo = slant_bounds(cfg, d)[0] if cfg.arrival != "aligned" else 0.0
    if cfg.arrival == "aligned":
        off_s = np.zeros(k)
    elif cfg.arrival == "beacon":
        off_s = 2.0 * (r - r_lo) / 2.998e8
    else:
        off_s = (r_arr.uniform(0.0, cfg.arrival_window_ms * 1e-3, size=k)
                 + (r - r_lo) / 2.998e8)
    pad = d["block_len"] // 2
    arrival = pad + np.round(off_s * fs).astype(int)
    window = int(round(arrival_window_s(cfg, d) * fs))
    n_samples = int(max(pad + window, arrival.max()) + B * d["slot_len"] + pad)

    return Scenario(cfg=cfg, d=d, codes=code_idx, hops=hop_idx, channels=channels,
                    symbols=symbols, phases=phases, snr_db=snr,
                    doppler_hz=geo["doppler_hz"], rate_hz_s=geo["doppler_rate_hz_s"],
                    osc_hz=osc, arrival=arrival, geo=geo, map_xy=xy,
                    n_samples=n_samples, cfo_err=r_cfo.standard_normal(k))


def synthesize(scn: Scenario, wf: Waveforms, rng: np.random.Generator,
               return_clean: bool = False):
    """Wideband capture: sum of all hopping blocks + AWGN (unit noise / chip BW).
    With return_clean, also return the noise-free signal sum (for display)."""
    d = scn.d
    fs, L = d["fs"], d["block_len"]
    chf = np.asarray(d["channel_freqs_hz"])
    rx = np.zeros(scn.n_samples, dtype=complex)
    nloc = np.arange(L)
    for u in range(scn.k):
        amp = 10.0 ** (scn.snr_db[u] / 20.0)
        f0 = scn.doppler_hz[u] + scn.osc_hz[u]
        for b in range(d["n_blocks"]):
            start = scn.block_start(u, b)
            tau = (b * d["slot_len"] + nloc) / fs            # time since packet start
            f_ch = chf[scn.channels[u, b]]
            phase = 2 * np.pi * ((f_ch + f0) * tau + 0.5 * scn.rate_hz_s[u] * tau ** 2)
            seg = wf.block(scn.codes[u], scn.symbols[u, b])
            rx[start:start + L] += amp * seg * np.exp(1j * (phase + scn.phases[u, b]))
    clean = rx.copy() if return_clean else None
    noise = (rng.standard_normal(scn.n_samples) + 1j * rng.standard_normal(scn.n_samples))
    rx += noise * np.sqrt(d["sps"] / 2.0)
    return (rx, clean) if return_clean else rx


# ----------------------------------------------------------------------------
# receiver
# ----------------------------------------------------------------------------

def _mixer(f_hz: float, L: int, fs: float) -> np.ndarray:
    return np.exp(-2j * np.pi * f_hz * np.arange(L) / fs)


def bulk_reference(scn: Scenario) -> tuple[float, float]:
    """Beam-centre Doppler and Doppler rate (known from ephemeris)."""
    if not scn.cfg.bulk_comp:
        return 0.0, 0.0
    return scn.d["centre_doppler_hz"], scn.d["centre_rate_hz_s"]


def estimate_offsets(rx: np.ndarray, scn: Scenario, wf: Waveforms,
                     rng: np.random.Generator, prior_hz: np.ndarray | None = None,
                     span_hz: float | None = None) -> np.ndarray:
    """Per-user carrier-offset estimate [Hz], referenced to the pilot centre.

    genie : true offset + N(0, sigma)            (paper's evaluated regime)
    pilot : despread each known pilot block, integrate-and-dump to 4/chip,
            zero-padded FFT, noncoherent sum over pilots, peak search within
            +/- cfo_span around the bulk-Doppler prior, parabolic refinement.
            ``prior_hz`` / ``span_hz`` replace that prior and span (the blind
            receiver refines its coarse per-node estimate this way).
    """
    cfg, d = scn.cfg, scn.d
    k = scn.k
    p_mid = max(cfg.n_pilot - 1, 0) / 2.0
    tau_mid = (p_mid * d["slot_len"] + d["block_len"] / 2.0) / d["fs"]
    truth = scn.doppler_hz + scn.osc_hz + scn.rate_hz_s * tau_mid
    if cfg.cfo_mode == "genie" or cfg.n_pilot == 0:
        err = scn.cfo_err if scn.cfo_err is not None else rng.standard_normal(k)
        return truth + cfg.cfo_sigma_hz * err

    fs, L, s, n = d["fs"], d["block_len"], d["sps"], d["n"]
    chf = np.asarray(d["channel_freqs_hz"])
    f_bulk, _ = bulk_reference(scn)
    q = s // 4                                    # dump factor -> rate 4*Rc
    fs_q = fs / q
    nfft = 1 << int(math.ceil(math.log2(max(16 * 4 * n, fs_q / 8.0))))
    freqs = sfft.fftfreq(nfft, 1.0 / fs_q)
    span = cfg.cfo_span_khz * 1e3 if span_hz is None else float(span_hz)
    sel = np.abs(freqs) <= span
    est = np.empty(k)
    for u in range(k):
        acc = np.zeros(nfft)
        centre = f_bulk if prior_hz is None else float(prior_hz[u])
        for b in range(cfg.n_pilot):
            start = scn.block_start(u, b)
            f_ref = chf[scn.channels[u, b]] + centre
            w = rx[start:start + L] * _mixer(f_ref, L, fs)
            v = w * wf.block(scn.codes[u], scn.symbols[u, b]).conj()
            v = v.reshape(-1, q).sum(axis=1)
            acc += np.abs(sfft.fft(v, n=nfft)) ** 2
        acc_sel = np.where(sel, acc, -np.inf)
        i = int(np.argmax(acc_sel))
        df = freqs[1] - freqs[0]
        delta = 0.0
        ym, y0, yp = acc[(i - 1) % nfft], acc[i], acc[(i + 1) % nfft]
        den = ym - 2 * y0 + yp
        if den < 0:
            delta = 0.5 * (ym - yp) / den
        est[u] = centre + freqs[i] + delta * df
    return est


@dataclass
class RxResult:
    mode: str
    decided: np.ndarray        # (K, B) decided shift (-1 = not decided)
    peak: np.ndarray           # (K, B) |corr| peak at decision / L
    margin_db: np.ndarray      # (K, B) peak over runner-up at decision
    profiles: np.ndarray       # (K*B, N) |corr| / L at decision (float32)
    step: np.ndarray           # (K, B) peel step (-1 if not peeled)
    peel: list                 # [(comp, start, f_rx, code, symbol, gain)]
    n_evals: int


def _component_table(scn: Scenario, f_est: np.ndarray):
    """Flattened (user, block) components with receiver mixing frequencies."""
    d, cfg = scn.d, scn.cfg
    k, B = scn.k, d["n_blocks"]
    chf = np.asarray(d["channel_freqs_hz"])
    _, rate_bulk = bulk_reference(scn)
    p_mid = max(cfg.n_pilot - 1, 0) / 2.0
    starts = np.empty(k * B, dtype=int)
    f_rx = np.empty(k * B)
    code = np.empty(k * B, dtype=int)
    for u in range(k):
        for b in range(B):
            j = u * B + b
            starts[j] = scn.block_start(u, b)
            drift = rate_bulk * (b - p_mid) * d["slot_len"] / d["fs"]
            f_rx[j] = chf[scn.channels[u, b]] + f_est[u] + drift
            code[j] = scn.codes[u]
    return starts, f_rx, code


def receive(rx: np.ndarray, scn: Scenario, wf: Waveforms, f_est: np.ndarray,
            sic: bool = True, keep_profiles: bool = True) -> RxResult:
    d, cfg = scn.d, scn.cfg
    k, B, L, n, fs = scn.k, d["n_blocks"], d["block_len"], d["n"], d["fs"]
    if k == 0:
        e = np.zeros((0, B))
        return RxResult("sic" if sic else "tin", e.astype(int), e, e,
                        np.zeros((0, n), np.float32) if keep_profiles else None, e.astype(int), [], 0)
    starts, f_rx, code = _component_table(scn, f_est)
    n_comp = k * B
    is_pilot = np.tile(np.arange(B) < cfg.n_pilot, k)
    known = scn.symbols.reshape(-1)
    mix_cache: dict = {}

    def mixers(idx):
        out = np.empty((len(idx), L), dtype=complex)
        for r, j in enumerate(idx):
            m = mix_cache.get(j)
            if m is None:
                m = _mixer(f_rx[j], L, fs)
                if n_comp * L <= 3_000_000:
                    mix_cache[j] = m
            out[r] = m
        return out

    def evaluate(residual, idx):
        idx = np.asarray(idx, dtype=int)
        win = residual[starts[idx][:, None] + np.arange(L)[None, :]] * mixers(idx)
        return wf.correlate(win, code[idx]) / L

    decided = np.full((k, B), -1, dtype=int)
    peak = np.zeros((k, B))
    margin = np.zeros((k, B))
    step = np.full((k, B), -1, dtype=int)
    profiles = np.zeros((n_comp, n), dtype=np.float32) if keep_profiles else None
    n_evals = 0

    def record(j, prof):
        u, b = divmod(int(j), B)
        sym = int(known[j]) if is_pilot[j] else int(np.argmax(prof))
        top = prof[sym]
        rest = np.delete(prof, sym)
        decided[u, b] = sym
        peak[u, b] = top
        margin[u, b] = 20 * np.log10(top / (rest.max() + 1e-12) + 1e-12)
        if profiles is not None:
            profiles[j] = prof
        return sym

    if not sic:                          # treat interference as noise
        data_idx = np.flatnonzero(~is_pilot)
        prof = evaluate(rx, data_idx)
        n_evals += len(data_idx)
        for r, j in enumerate(data_idx):
            record(j, prof[r])
        return RxResult("tin", decided, peak, margin, profiles, step, [], n_evals)

    residual = rx.copy()
    current = evaluate(residual, np.arange(n_comp))
    n_evals += n_comp
    score = np.where(is_pilot, current[np.arange(n_comp), known], current.max(axis=1))
    remaining = np.ones(n_comp, dtype=bool)
    near_hz = 1.6 * d["rc"]
    peel = []
    for it in range(n_comp):
        cand = np.flatnonzero(remaining)
        j = int(cand[np.argmax(score[cand])])
        sym = record(j, current[j])
        u, b = divmod(j, B)
        step[u, b] = it
        s0 = starts[j]
        tmpl = wf.block(code[j], sym) * np.conj(mixers([j])[0])
        seg = residual[s0:s0 + L]
        gain = np.vdot(tmpl, seg) / (np.vdot(tmpl, tmpl).real + 1e-12)
        residual[s0:s0 + L] = seg - gain * tmpl
        peel.append((j, int(s0), float(f_rx[j]), int(code[j]), sym, complex(gain)))
        remaining[j] = False
        dirty = np.flatnonzero(remaining & (np.abs(starts - s0) < L)
                               & (np.abs(f_rx - f_rx[j]) < near_hz))
        if len(dirty):
            current[dirty] = evaluate(residual, dirty)
            n_evals += len(dirty)
            score[dirty] = np.where(is_pilot[dirty], current[dirty, known[dirty]],
                                    current[dirty].max(axis=1))
    return RxResult("sic", decided, peak, margin, profiles, step, peel, n_evals)


def reconstruct(peel_entry, wf: Waveforms, fs: float) -> tuple[int, np.ndarray]:
    """Waveform that SIC step ``peel_entry`` subtracted from the capture."""
    _, start, f_rx, code, sym, gain = peel_entry
    tmpl = wf.block(code, sym) * np.conj(_mixer(f_rx, wf.L, fs))
    return start, gain * tmpl


# ----------------------------------------------------------------------------
# metrics
# ----------------------------------------------------------------------------

def score(scn: Scenario, res: RxResult, match: np.ndarray | None = None) -> dict:
    """Score decisions against the truth.  ``match[u]`` is the receiver's
    hypothesis for true node u (-1 if it never detected u); with known side
    information it is the identity."""
    cfg, d = scn.cfg, scn.d
    P = cfg.n_pilot
    if match is None:
        match = np.arange(scn.k)
    det = match >= 0
    ok = np.zeros((scn.k, cfg.n_data), dtype=bool)
    if det.any():
        ok[det] = res.decided[match[det], P:] == scn.symbols[det, P:]
    errors = (~ok).sum(axis=1)
    pkt_ok = errors <= d["rs_t"]
    n_hyp = res.decided.shape[0]
    return {
        "detected": det,
        "n_detected": int(det.sum()),
        "n_false": int(n_hyp - len(set(match[det].tolist()))),
        "sym_ok": ok,
        "sym_correct": int(ok.sum()),
        "sym_total": int(ok.size),
        "raw_recovery": float(ok.mean()) if ok.size else 0.0,
        "pkt_ok": pkt_ok,
        "pkt_success": float(pkt_ok.mean()) if len(pkt_ok) else 0.0,
    }


# ----------------------------------------------------------------------------
# one frame
# ----------------------------------------------------------------------------

@dataclass
class Frame:
    """One simulated frame.  ``sic`` / ``tin`` are indexed by the receiver's
    hypotheses (``sic_view`` / ``tin_view``); ``sic_match[u]`` maps true node
    u to its hypothesis (-1 = missed).  With known side information the views
    are the true scenario and the matches are the identity."""
    cfg: UplinkConfig
    scn: Scenario
    wf: Waveforms
    rx: np.ndarray
    f_est: np.ndarray            # SIC hypotheses' offset estimates
    sic: RxResult
    tin: RxResult
    sic_score: dict
    tin_score: dict
    clean: np.ndarray | None = None
    sic_view: Scenario | None = None
    tin_view: Scenario | None = None
    sic_match: np.ndarray | None = None
    tin_match: np.ndarray | None = None
    tin_f_est: np.ndarray | None = None
    rejected: int = 0


def run_frame(cfg: UplinkConfig, keep_profiles: bool = True,
              keep_clean: bool = False) -> Frame:
    rng = np.random.default_rng(cfg.seed)
    scn = build_scenario(cfg, rng)
    wf = Waveforms(cfg, scn.d)
    rx, clean = synthesize(scn, wf, rng, return_clean=True)
    if not keep_clean:
        clean = None
    if cfg.acquisition == "blind":
        from . import acquire
        br = acquire.blind_receive(rx, scn, wf, rng, keep_profiles=keep_profiles)
        sm = acquire.match(scn, br.sic_view, br.sic)
        tm = acquire.match(scn, br.tin_view, br.tin)
        return Frame(cfg, scn, wf, rx, br.sic_f, br.sic, br.tin, score(scn, br.sic, sm),
                     score(scn, br.tin, tm), clean, br.sic_view, br.tin_view, sm, tm,
                     br.tin_f, br.rejected)
    if is_lora(cfg) and cfg.cfo_mode == "pilot":
        from . import acquire
        f_est = acquire.lora_known_offsets(rx, scn, wf)
    else:
        f_est = estimate_offsets(rx, scn, wf, rng)
    sic = receive(rx, scn, wf, f_est, sic=True, keep_profiles=keep_profiles)
    tin = receive(rx, scn, wf, f_est, sic=False, keep_profiles=keep_profiles)
    ident = np.arange(scn.k)
    return Frame(cfg, scn, wf, rx, f_est, sic, tin, score(scn, sic), score(scn, tin), clean,
                 scn, scn, ident, ident, f_est, 0)


def residual_after(frame: Frame, stage: int, clean: bool = False) -> np.ndarray:
    """Capture after the first ``stage`` SIC subtractions.  With clean=True the
    same subtractions are applied to the noise-free signal sum, which shows
    what cancellation leaves behind (residue from wrong or imperfect peels)."""
    res = (frame.clean if clean else frame.rx).copy()
    fs = frame.scn.d["fs"]
    for entry in frame.sic.peel[:max(0, int(stage))]:
        start, w = reconstruct(entry, frame.wf, fs)
        res[start:start + len(w)] -= w
    return res


# ----------------------------------------------------------------------------
# spectrogram
# ----------------------------------------------------------------------------

def spectrogram(x: np.ndarray, fs: float, sps: int, max_frames: int = 900):
    """Hann STFT power [dB], frequency fftshifted.  Returns (P_db (F, T),
    t_centres_s, f_hz)."""
    nfft = int(2 ** round(math.log2(max(64, min(1024, 8 * sps)))))
    hop = max(nfft // 4, int(math.ceil((len(x) - nfft) / max_frames)))
    n_frames = max(1, 1 + (len(x) - nfft) // hop)
    idx = np.arange(nfft)[None, :] + hop * np.arange(n_frames)[:, None]
    win = np.hanning(nfft)
    spec = sfft.fftshift(sfft.fft(x[idx] * win, axis=1), axes=1)
    p = np.abs(spec) ** 2 / np.sum(win ** 2)
    p_db = 10 * np.log10(p.T + 1e-12)
    t = (hop * np.arange(n_frames) + nfft / 2) / fs
    f = sfft.fftshift(sfft.fftfreq(nfft, 1.0 / fs))
    return p_db, t, f


# ----------------------------------------------------------------------------
# sweeps
# ----------------------------------------------------------------------------

def sweep_k(cfg: UplinkConfig, k_grid, frames_per_k: int = 5):
    """Recovery vs K for TIN and ordered SIC.  Yields progress dicts and finally
    {"done": True, "rows": [...]}.  Each K uses independent seeds
    (cfg.seed, K, trial), so results do not depend on the grid."""
    rows = []
    total = len(k_grid) * frames_per_k
    done = 0
    for k in k_grid:
        acc = {"sic": [0, 0, 0], "tin": [0, 0, 0]}   # sym correct, sym total, pkt ok
        for trial in range(frames_per_k):
            seed = int(np.random.SeedSequence([cfg.seed, int(k), trial]).generate_state(1)[0])
            fr = run_frame(replace(cfg, n_users=int(k), seed=seed), keep_profiles=False)
            for name, sc in (("sic", fr.sic_score), ("tin", fr.tin_score)):
                acc[name][0] += sc["sym_correct"]
                acc[name][1] += sc["sym_total"]
                acc[name][2] += int(sc["pkt_ok"].sum())
            done += 1
            yield {"progress": done / total, "k": int(k), "trial": trial}
        row = {"k": int(k), "users": int(k) * frames_per_k}
        for name in ("sic", "tin"):
            c, t, p = acc[name]
            row[f"{name}_raw"] = c / t if t else 0.0
            row[f"{name}_sym_total"] = t
            row[f"{name}_pkt"] = p / (int(k) * frames_per_k)
        rows.append(row)
        yield {"row": row}
    yield {"done": True, "rows": rows}
