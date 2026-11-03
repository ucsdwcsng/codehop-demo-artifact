"""Blind acquisition: find who transmitted, with which code and hop pattern,
when, and at what carrier offset, without any of it being supplied.

Pipeline (one call to ``blind_receive`` per frame):

1. **Channelize.**  Mix the capture down to every channel (after removing the
   beam-centre Doppler when ``bulk_comp`` is on) and decimate to ``SD``
   samples per chip.
2. **Search.**  For every code, correlate each pilot block's public waveform
   on the code's public pilot channel over every candidate start time in the
   protocol's arrival window and every carrier offset within +/- the search
   span (partial correlations over short segments, then an FFT across
   segments).  Each correlation is normalised by the energy of the window it
   was taken over and the P pilots are summed non-coherently.  Cells where
   one pilot supplies most of the sum are dropped here already (see step 3).  LoRa has one
   shared chirp, so it searches each channel instead of each code.
3. **Verify.**  For each candidate above the noise threshold, refine the
   offset on the pilots, correlate every data block on every channel against
   all shifts of the candidate's code, and pick the public hop pattern with
   the most data-block evidence.  The evidence threshold is set per round
   from decoys: the same verification run for a wrong code at every
   candidate's time and offset, so it tracks how much interference the round
   sees.  LoRa has no decoy: every LoRa data symbol is the shared chirp at
   some offset, so data evidence cannot tell one LoRa node from another and
   only the fixed floor applies (its preamble test does the work).  Candidates are dropped when their data
   evidence does not beat the decoys, or when one pilot supplies most of the pilot
   evidence (a single data block that happens to resemble a pilot; for LoRa
   every data symbol is a frequency-shifted preamble chirp, which is why
   real LoRa receivers look for several identical up-chirps in a row).
   Duplicates of one node are merged.
4. **Refine.**  Fine timing (full rate, +/- one chip) and carrier offset
   (pilot FFT around the coarse estimate) for each accepted node.  A LoRa
   delay of x chips is indistinguishable from an offset of x bins on the
   up-chirp preamble (real LoRa adds down-chirps to break this; the modelled
   packet has none), and a fractional x makes every data decision ambiguous
   between two neighbouring symbols.  The LoRa receiver therefore slides along
   that ridge and keeps the point where the data blocks' correlation peaks are
   sharpest (decision-free timing recovery).
5. **Cancel and search again.**  Run ordered SIC on the accepted nodes, drop
   hypotheses whose pilots carry no energy once SIC has run (aliases of a
   stronger node that it has already explained), subtract the rest, and
   repeat steps 1-4 on the residual.  Weak nodes hidden next to strong ones
   are found in this round.

TIN uses the first-round detections only: it never cancels anything.
"""
from __future__ import annotations

import functools
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv
from scipy import fft as sfft
from scipy import signal, stats
from scipy.ndimage import maximum_filter1d

from . import codes, waveform
from . import uplink as U
from .params import PHYParams

SD = 2                  # search rate [samples per chip]
P_FA_SEARCH = 0.1       # expected noise-only candidates per search (first round)
P_FA_RESIDUAL = 10.0    # later rounds: few candidates, verification guards them
VERIFY_MIN = 8.0        # floor on the data-evidence threshold (Exp(1) units)
DECOY_QUANTILE = 0.99   # threshold = this quantile of the decoys' evidence
BALANCE_MAX = 0.55      # largest share one pilot may supply (3+ pilots)
DEFER_RATIO = 0.5       # see ``defer``
P_FA_POST = 1e-3        # post-SIC pilot-energy test (per hypothesis)
ROUNDS = 3              # search rounds (later rounds run on the SIC residual)
MAX_CANDIDATES = 400    # per round, strongest first
LN2_INV = 1.0 / math.log(2.0)   # median of Exp(1) is ln 2
SEARCH_THREADS = int(os.environ.get("CODEHOP_SEARCH_THREADS", min(8, os.cpu_count() or 1)))


# ----------------------------------------------------------------------------
# hypotheses
# ----------------------------------------------------------------------------

@dataclass
class Hyps:
    """The receiver's view of the active set (one row per detected node)."""
    code: np.ndarray        # (M,) code index (LoRa: 0)
    hop: np.ndarray         # (M,) hop-pattern index (LoRa: the channel)
    channels: np.ndarray    # (M, B) channel of every block
    start: np.ndarray       # (M,) first-block start sample (full rate)
    f_hz: np.ndarray        # (M,) carrier offset at the pilot centre
    score: np.ndarray       # (M,) search statistic
    rnd: np.ndarray         # (M,) search round that found it

    @classmethod
    def empty(cls, n_blocks: int) -> "Hyps":
        z = np.zeros(0)
        return cls(z.astype(int), z.astype(int), np.zeros((0, n_blocks), int), z.astype(int), z, z, z.astype(int))

    def __len__(self) -> int:
        return len(self.code)

    def take(self, idx) -> "Hyps":
        idx = np.asarray(idx, dtype=int)
        return Hyps(*(getattr(self, f)[idx] for f in ("code", "hop", "channels", "start", "f_hz", "score", "rnd")))

    def concat(self, other: "Hyps") -> "Hyps":
        return Hyps(*(np.concatenate([getattr(self, f), getattr(other, f)])
                      for f in ("code", "hop", "channels", "start", "f_hz", "score", "rnd")))


def make_view(scn: U.Scenario, h: Hyps) -> U.Scenario:
    """A Scenario holding only what the receiver believes: detected codes,
    channels and start times, with the public pilot symbols.  ``receive`` and
    ``estimate_offsets`` (pilot mode) read nothing else."""
    cfg = replace(scn.cfg, cfo_mode="pilot")
    d = scn.d
    k, B, P = len(h), d["n_blocks"], cfg.n_pilot
    sym = np.zeros((k, B), dtype=int)
    if not U.is_lora(cfg):
        sym[:, :P] = U.pilot_shifts(d["n"], P)[None, :]
    z = np.zeros(k)
    return replace(scn, cfg=cfg, codes=h.code.astype(int), hops=h.hop.astype(int),
                   channels=h.channels.astype(int), symbols=sym, phases=np.zeros((k, B)),
                   snr_db=z, doppler_hz=z, rate_hz_s=z, osc_hz=z, arrival=h.start.astype(int),
                   geo={}, map_xy=np.zeros((k, 2)), cfo_err=z)


# ----------------------------------------------------------------------------
# search set-up
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    sd: int            # samples per chip
    q: int             # decimation factor from the capture rate
    gch: int           # chips per coherent segment
    Q: int             # segments per block
    G: int             # samples per segment
    Lt: int            # template length (padded to Q * G)
    La: int            # active template length (n * sd)
    NF: int            # offset bins
    bin_hz: float      # offset-bin spacing
    off: tuple         # start of every block relative to the packet [search samples]
    W: int             # candidate start positions


def make_grid(cfg: U.UplinkConfig, d: dict, n_capture: int, nf_mult: int = 1) -> Grid:
    s, rc, n = d["sps"], d["rc"], d["n"]
    sd = SD if s % SD == 0 else 1
    q = s // sd
    span = cfg.cfo_span_khz * 1e3
    gch = max(1, int(rc // (2.0 * span)))
    Q = math.ceil(n / gch)
    G = gch * sd
    off = tuple(int(round(b * d["slot_len"] / q)) for b in range(d["n_blocks"]))
    W = max(1, (n_capture - d["n_blocks"] * d["slot_len"]) // q + 1)
    return Grid(sd, q, gch, Q, G, Q * G, n * sd, nf_mult * Q, rc / (gch * Q * nf_mult), off, W)


def _waveform_key(cfg: U.UplinkConfig, d: dict) -> tuple:
    return (U.is_lora(cfg), cfg.family, float(d["rc"]), float(d["deviation_hz"]),
            float(cfg.bt), int(d["n"]))


@functools.lru_cache(maxsize=8)
def _templates(wkey: tuple, n_pilot: int, sd: int, Q: int, G: int) -> np.ndarray:
    """Conjugated pilot templates at the search rate, (P, Q, G, n_templates)."""
    lora, family, rc, dev, bt, n = wkey
    Lt = Q * G
    if lora:
        base = U.lora_upchirp(n, sd)
        T = np.zeros((n_pilot, 1, Lt), np.complex64)
        T[:, 0, :n * sd] = base[None, :]
    else:
        pool = U.code_pool(family)
        phy = PHYParams(chip_rate=rc, deviation=dev, bt=bt if bt > 0 else None)
        shifts = U.pilot_shifts(n, n_pilot)
        T = np.zeros((n_pilot, len(pool), Lt), np.complex64)
        for b in range(n_pilot):
            for c in range(len(pool)):
                T[b, c, :n * sd] = waveform.gfsk_waveform(codes.ccsk_shift(pool[c], shifts[b]), phy, sps=sd)
    return np.conj(T).reshape(n_pilot, T.shape[1], Q, G).transpose(0, 2, 3, 1).copy()


WAVEFORM_FIELDS = ("modulation", "family", "chip_rate", "mod_index", "bt", "lora_sf", "lora_bw_khz",
                   "n_channels", "spacing_khz", "fc_mhz", "osc_ppm")


@functools.lru_cache(maxsize=8)
def _search_waveforms(wkey: tuple, cfg_items: tuple, sd: int) -> U.Waveforms:
    """All-shift correlator at the search rate."""
    cfg = U.UplinkConfig(**dict(cfg_items))
    d = U.derived(cfg)
    d = dict(d, sps=sd, block_len=d["n"] * sd)
    return U.Waveforms(cfg, d)


def search_units(cfg: U.UplinkConfig, d: dict) -> tuple[np.ndarray, np.ndarray]:
    """What the search iterates over.  CodeHop: every code, with its public
    pilot channels.  LoRa: every channel (one shared chirp, all pilots on the
    node's channel).  Returns (pilot channel per unit and pilot, template
    index per unit)."""
    C, P = cfg.n_channels, cfg.n_pilot
    if U.is_lora(cfg):
        return np.repeat(np.arange(C)[:, None], P, axis=1), np.zeros(C, dtype=int)
    m = d["n_codes"]
    return U.pilot_channels(m, P, C), np.arange(m)


def channelize(y: np.ndarray, cfg: U.UplinkConfig, d: dict, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel baseband at the search rate (beam-centre Doppler removed
    when bulk_comp is on), zero-padded so every window exists, and the energy
    of every La-sample window."""
    f_bulk = d["centre_doppler_hz"] if cfg.bulk_comp else 0.0
    chf = np.asarray(d["channel_freqs_hz"])
    t = np.arange(len(y)) / d["fs"]
    yb = y * np.exp(-2j * np.pi * f_bulk * t)
    need = grid.W + max(grid.off) + grid.Lt + 1
    X = np.zeros((len(chf), need), np.complex64)
    for c, f in enumerate(chf):
        x = signal.resample_poly(yb * np.exp(-2j * np.pi * f * t), 1, grid.q)
        m = min(len(x), need)
        X[c, :m] = x[:m]
    cs = np.concatenate([np.zeros((len(chf), 1)), np.cumsum(np.abs(X) ** 2, axis=1)], axis=1)
    E = np.maximum(cs[:, grid.La:] - cs[:, :-grid.La], 1e-12).astype(np.float32)
    return X, E


# ----------------------------------------------------------------------------
# search, verification, refinement
# ----------------------------------------------------------------------------

def search(X: np.ndarray, E: np.ndarray, grid: Grid, unit_ch: np.ndarray, unit_tmpl: np.ndarray,
           Tm: np.ndarray, n_pilot: int, p_fa: float = P_FA_SEARCH) -> list[tuple[int, int, float, float]]:
    """Candidates (unit, start [search samples], offset relative to the
    channel [Hz], statistic), strongest first."""
    M, C = len(unit_ch), X.shape[0]
    W, NF, Q, G, Lt = grid.W, grid.NF, grid.Q, grid.G, grid.Lt
    groups = [[np.flatnonzero(unit_ch[:, b] == c) for c in range(C)] for b in range(n_pilot)]
    best = np.zeros((W, M), np.float32)
    bestf = np.zeros((W, M), np.int32)
    chunk = max(16, int(4e6 // (NF * M)))
    views = [swv(X[c], Lt) for c in range(C)]

    def run(w0: int) -> None:
        w1 = min(W, w0 + chunk)
        w = w1 - w0
        tot = np.zeros((NF, w, M), np.float32)
        big = np.zeros((NF, w, M), np.float32) if n_pilot >= 3 else None
        for b in range(n_pilot):
            o = grid.off[b]
            for c in range(C):
                idx = groups[b][c]
                if not len(idx):
                    continue
                A = views[c][w0 + o:w1 + o].reshape(w, Q, G).transpose(1, 0, 2)
                Z = np.matmul(A, Tm[b][:, :, unit_tmpl[idx]])
                F = sfft.fft(Z, n=NF, axis=0, workers=1)
                pw = F.real ** 2
                pw += F.imag ** 2
                pw /= E[c, w0 + o:w1 + o][None, :, None]
                tot[:, :, idx] += pw
                if big is not None:
                    big[:, :, idx] = np.maximum(big[:, :, idx], pw)
        fi = np.argmax(tot, axis=0)
        bt = np.take_along_axis(tot, fi[None], 0)[0]
        if big is not None:
            share = np.take_along_axis(big, fi[None], 0)[0] / np.maximum(bt, 1e-30)
            bt[share > BALANCE_MAX] = 0.0
        best[w0:w1] = bt
        bestf[w0:w1] = fi

    starts = list(range(0, W, chunk))
    if len(starts) > 1 and SEARCH_THREADS > 1:
        with ThreadPoolExecutor(min(SEARCH_THREADS, len(starts))) as ex:
            list(ex.map(run, starts))
    else:
        for w0 in starts:
            run(w0)
    lam = float(stats.gamma.isf(p_fa / (W * M * NF), n_pilot))
    peak = maximum_filter1d(best, size=2 * grid.sd + 1, axis=0)
    tt, mm = np.nonzero((best >= lam) & (best == peak))
    order = np.argsort(-best[tt, mm])[:MAX_CANDIDATES]
    fgrid = sfft.fftfreq(NF, grid.G / (grid.sd * (grid.bin_hz * grid.gch * grid.NF)))
    return [(int(mm[i]), int(tt[i]), float(fgrid[bestf[tt[i], mm[i]]]), float(best[tt[i], mm[i]]))
            for i in order]


def verify(cands, X: np.ndarray, E: np.ndarray, grid: Grid, cfg: U.UplinkConfig, d: dict,
           unit_ch: np.ndarray, unit_tmpl: np.ndarray, Tm: np.ndarray, wf2: U.Waveforms):
    """Refine each candidate's offset on its pilots, then score every data
    block on every channel against all shifts of the candidate's code and pick
    the hop pattern with the most evidence.  Returns per candidate
    (offset [Hz, relative], hop index, channels (B,), evidence, largest
    single-pilot share of the pilot evidence)."""
    n, sd, La, P, B, C = d["n"], grid.sd, grid.La, cfg.n_pilot, d["n_blocks"], cfg.n_channels
    D = B - P
    lora = U.is_lora(cfg)
    rc = d["rc"]
    out = []
    pats = None if lora else U.hop_patterns(cfg.hop_pool, B, C)
    tl = np.arange(La) / (sd * rc)
    nfft = 8 * (1 << math.ceil(math.log2(n)))
    ff = sfft.fftfreq(nfft, 1.0 / rc)
    for i0 in range(0, len(cands), 40):
        cb = cands[i0:i0 + 40]
        m = np.array([c[0] for c in cb])
        tau = np.array([c[1] for c in cb])
        f0 = np.array([c[2] for c in cb])
        nc = len(cb)
        # pilots: despread, integrate to one sample per chip, zero-padded FFT
        pil = np.empty((nc, P, La), np.complex64)
        for b in range(P):
            tmpl = Tm[b].transpose(2, 0, 1).reshape(Tm.shape[3], -1)[unit_tmpl[m], :La]
            for i in range(nc):
                o = tau[i] + grid.off[b]
                pil[i, b] = X[unit_ch[m[i], b], o:o + La] * tmpl[i]
        specb = np.abs(sfft.fft(pil.reshape(nc, P, n, sd).sum(-1), n=nfft, axis=-1)) ** 2
        spec = specb.sum(1)
        ok = np.abs(ff[None, :] - f0[:, None]) <= grid.bin_hz
        k = np.argmax(np.where(ok, spec, -1.0), axis=1)
        y0, ym, yp = spec[np.arange(nc), k], spec[np.arange(nc), (k - 1) % nfft], spec[np.arange(nc), (k + 1) % nfft]
        den = ym - 2 * y0 + yp
        delta = np.where(den < 0, 0.5 * (ym - yp) / np.where(den < 0, den, -1.0), 0.0)
        f = ff[k] + delta * (ff[1] - ff[0])
        pe = specb[np.arange(nc), :, k] / (np.median(specb, axis=-1) * LN2_INV + 1e-30)
        balance = pe.max(axis=1) / np.maximum(pe.sum(axis=1), 1e-12)
        # data blocks, all shifts: every channel for CodeHop (the hop pattern
        # is unknown), the candidate's own channel for LoRa (it never hops)
        Cw = 1 if lora else C
        win = np.empty((nc, D, Cw, La), np.complex64)
        for bi, b in enumerate(range(P, B)):
            for i in range(nc):
                o = tau[i] + grid.off[b]
                win[i, bi] = X[m[i]:m[i] + 1, o:o + La] if lora else X[:, o:o + La]
        win *= np.exp(-2j * np.pi * f[:, None, None, None] * tl[None, None, None, :]).astype(np.complex64)
        code = np.zeros(nc, dtype=int) if lora else m
        prof = wf2.correlate(win.reshape(-1, La), np.repeat(code, D * Cw))
        p2 = prof ** 2
        tab = (p2.max(axis=1) / (np.median(p2, axis=1) * LN2_INV + 1e-30)).reshape(nc, D, Cw)
        for i in range(nc):
            if lora:
                ch = int(m[i])
                out.append((float(f[i]), ch, np.full(B, ch), float(tab[i, :, 0].mean()), float(balance[i])))
            else:
                s = tab[i][np.arange(D)[None, :], pats[:, P:]].sum(axis=1)
                h = int(np.argmax(s))
                chans = pats[h].copy()
                chans[:P] = unit_ch[m[i]]
                out.append((float(f[i]), h, chans, float(s[h] / D), float(balance[i])))
    return out


def same_node(cfg: U.UplinkConfig, d: dict, a: tuple, b: tuple) -> bool:
    """Whether two detections (unit, start [full-rate samples], offset [Hz])
    are the same transmission.  CodeHop: same code within two chips.  LoRa: a
    delay of k chips looks like an offset of k bins, so detections on one
    channel along that ridge (or a whole symbol along it) are one node."""
    s, n, rc = d["sps"], d["n"], d["rc"]
    if a[0] != b[0]:
        return False
    dt = (a[1] - b[1]) / s
    if not U.is_lora(cfg):
        return abs(dt) <= 2.0
    if abs(dt) > (cfg.n_pilot + 0.5) * n:
        return False
    ridge = (a[2] - b[2]) / (rc / n) - dt
    ridge = (ridge + n / 2.0) % n - n / 2.0
    return abs(ridge) <= 1.5


def refine_timing(y: np.ndarray, view: U.Scenario, wf: U.Waveforms, f_est: np.ndarray,
                  radius: int) -> tuple[np.ndarray, np.ndarray]:
    """Start sample maximising the pilots' summed correlation within
    +/-radius.  For LoRa a delay trades against an offset (one chip = one bin),
    so the offset moves with the delay and the search lands on the whole-chip
    point of the ridge, which is where the decisions are unambiguous."""
    d, P = view.d, view.cfg.n_pilot
    L, fs, s = d["block_len"], d["fs"], d["sps"]
    chf = np.asarray(d["channel_freqs_hz"])
    lora = U.is_lora(view.cfg)
    deltas = np.arange(-radius, radius + 1)
    df = deltas / s * (d["rc"] / d["n"]) if lora else np.zeros(len(deltas))
    tl = np.arange(L) / fs
    yp = np.concatenate([np.zeros(radius, y.dtype), y, np.zeros(radius + L, y.dtype)])
    start, fout = view.arrival.copy(), np.asarray(f_est, float).copy()
    for u in range(view.k):
        acc = np.zeros(len(deltas))
        for b in range(P):
            s0 = view.block_start(u, b)                    # yp[s0] is sample s0 - radius
            seg = swv(yp[s0:s0 + L + 2 * radius], L)
            t = np.conj(wf.block(view.codes[u], view.symbols[u, b])) * U._mixer(chf[view.channels[u, b]] + f_est[u], L, fs)
            if lora:
                acc += np.abs(np.einsum("dl,dl->d", seg, t[None, :] * np.exp(-2j * np.pi * df[:, None] * tl[None, :]))) ** 2
            else:
                acc += np.abs(seg @ t) ** 2
        i = int(np.argmax(acc))
        start[u] = view.arrival[u] + deltas[i]
        fout[u] = f_est[u] + df[i]
    return start, fout


def lora_ridge_timing(y: np.ndarray, view: U.Scenario, wf: U.Waveforms, f_est: np.ndarray,
                      n_blocks_used: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """LoRa only: move each node along the delay/offset ridge by up to half a
    chip to where the data blocks' largest correlation is largest."""
    d, P = view.d, view.cfg.n_pilot
    L, fs, s = d["block_len"], d["fs"], d["sps"]
    chf = np.asarray(d["channel_freqs_hz"])
    deltas = np.arange(-(s // 2), s - s // 2)
    dfs = deltas / s * (d["rc"] / d["n"])
    blocks = list(range(P, min(d["n_blocks"], P + n_blocks_used)))
    yp = np.concatenate([np.zeros(s, y.dtype), y, np.zeros(s + L, y.dtype)])
    start, fout = view.arrival.copy(), np.asarray(f_est, float).copy()
    if not blocks:
        return start, fout
    for u in range(view.k):
        win = np.empty((len(deltas), len(blocks), L), complex)
        for bi, b in enumerate(blocks):
            s0 = view.block_start(u, b) + s
            for di, (dl, dfh) in enumerate(zip(deltas, dfs)):
                win[di, bi] = yp[s0 + dl:s0 + dl + L] * U._mixer(chf[view.channels[u, b]] + f_est[u] + dfh, L, fs)
        prof = wf.correlate(win.reshape(-1, L), np.zeros(win.shape[0] * win.shape[1], dtype=int))
        metric = (prof.max(axis=1) ** 2).reshape(len(deltas), len(blocks)).sum(axis=1)
        i = int(np.argmax(metric))
        start[u] = view.arrival[u] + deltas[i]
        fout[u] = f_est[u] + dfs[i]
    return start, fout


def detect(y: np.ndarray, scn: U.Scenario, wf: U.Waveforms, existing: Hyps, rnd: int,
           rng: np.random.Generator, with_tin: bool = False) -> tuple[Hyps, Hyps | None]:
    """One search round on ``y`` (the capture or a SIC residual).  Returns the
    detections for SIC (decoy-calibrated evidence threshold) and, with
    ``with_tin``, those for TIN (fixed floor: a false TIN packet costs nothing,
    because it is never subtracted and fails the packet check)."""
    cfg, d = replace(scn.cfg, cfo_mode="pilot"), scn.d
    grid = make_grid(cfg, d, len(y), nf_mult=1 if rnd == 0 else 2)
    unit_ch, unit_tmpl = search_units(cfg, d)
    wkey = _waveform_key(cfg, d)
    Tm = _templates(wkey, cfg.n_pilot, grid.sd, grid.Q, grid.G)
    wf2 = _search_waveforms(wkey, tuple((f, getattr(cfg, f)) for f in WAVEFORM_FIELDS), grid.sd)
    X, E = channelize(y, cfg, d, grid)
    cands = search(X, E, grid, unit_ch, unit_tmpl, Tm, cfg.n_pilot,
                   p_fa=P_FA_SEARCH if rnd == 0 else P_FA_RESIDUAL)
    B = d["n_blocks"]
    if not cands:
        return Hyps.empty(B), (Hyps.empty(B) if with_tin else None)
    f_bulk = d["centre_doppler_hz"] if cfg.bulk_comp else 0.0
    q = grid.q
    prev = [(int(existing.code[i]) if not U.is_lora(cfg) else int(existing.channels[i, 0]),
             int(existing.start[i]), float(existing.f_hz[i]) - f_bulk) for i in range(len(existing))]
    cands = [c for c in cands if not any(same_node(cfg, d, (c[0], c[1] * q, c[2]), p) for p in prev)]
    ver = verify(cands, X, E, grid, cfg, d, unit_ch, unit_tmpl, Tm, wf2)
    M = len(unit_ch)
    v_min = VERIFY_MIN
    if not U.is_lora(cfg) and M > 1:
        decoys = [((c[0] + 1 + c[1] % (M - 1)) % M, c[1], c[2], c[3]) for c in cands]
        vd = np.array([v[3] for v in verify(decoys, X, E, grid, cfg, d, unit_ch, unit_tmpl, Tm, wf2)])
        v_min = max(VERIFY_MIN, float(np.quantile(vd, DECOY_QUANTILE)))
    bal_max = BALANCE_MAX if cfg.n_pilot >= 3 else 1.0

    def accept(threshold: float) -> list[int]:
        kept: list[int] = []
        for i, (c, v) in enumerate(zip(cands, ver)):
            if v[3] < threshold or v[4] > bal_max:
                continue
            key = (c[0], c[1] * q, v[0])
            if any(same_node(cfg, d, key, (cands[j][0], cands[j][1] * q, ver[j][0])) for j in kept):
                continue
            kept.append(i)
        return kept

    k_sic = accept(v_min)
    k_tin = accept(VERIFY_MIN) if with_tin else []
    kept = sorted(set(k_sic) | set(k_tin))
    if not kept:
        return Hyps.empty(B), (Hyps.empty(B) if with_tin else None)
    lora = U.is_lora(cfg)
    h = Hyps(code=np.array([0 if lora else cands[i][0] for i in kept]),
             hop=np.array([ver[i][1] for i in kept]),
             channels=np.stack([ver[i][2] for i in kept]),
             start=np.array([cands[i][1] * q for i in kept]),
             f_hz=np.array([f_bulk + ver[i][0] for i in kept]),
             score=np.array([cands[i][3] for i in kept]),
             rnd=np.full(len(kept), rnd))
    # full-rate refinement: timing to the sample, then the offset around the coarse value
    last = len(y) - B * d["slot_len"]                 # latest start with the whole packet captured
    view = make_view(scn, h)
    h.start, h.f_hz = refine_timing(y, view, wf, h.f_hz, radius=d["sps"])
    h.start = np.clip(h.start, 0, last)
    view = make_view(scn, h)
    h.f_hz = U.estimate_offsets(y, view, wf, rng, prior_hz=h.f_hz, span_hz=grid.bin_hz)
    if lora:
        h.start, h.f_hz = lora_ridge_timing(y, make_view(scn, h), wf, h.f_hz)
        h.start = np.clip(h.start, 0, last)
    pos = {i: k for k, i in enumerate(kept)}
    return h.take([pos[i] for i in k_sic]), (h.take([pos[i] for i in k_tin]) if with_tin else None)


def defer(cfg: U.UplinkConfig, d: dict, h: Hyps) -> np.ndarray:
    """Which detections the SIC receiver takes this round.  A detection that
    shares its hop pattern (LoRa: its channel) with a stronger one, overlaps
    it in time and has less than DEFER_RATIO of its statistic is most likely
    an alias of it: a code at a Doppler offset partly matches another code,
    and the alias decodes the stronger node's blocks, so it passes
    verification.  It is deferred; once the stronger node is cancelled, a real
    node is found again in the next round and an alias is not."""
    packet = d["n_blocks"] * d["slot_len"]
    order = np.argsort(-h.score)
    take = np.ones(len(h), dtype=bool)
    for a, i in enumerate(order):
        for j in order[:a]:
            if (take[j] and h.hop[i] == h.hop[j] and abs(h.start[i] - h.start[j]) < packet
                    and h.score[i] < DEFER_RATIO * h.score[j]):
                take[i] = False
                break
    return take


def residual(rx: np.ndarray, res: U.RxResult, wf: U.Waveforms, fs: float, keep_hyp=None,
             n_blocks: int = 1) -> np.ndarray:
    y = rx.copy()
    for e in res.peel:
        if keep_hyp is not None and not keep_hyp[e[0] // n_blocks]:
            continue
        start, w = U.reconstruct(e, wf, fs)
        y[start:start + len(w)] -= w
    return y


def pilot_energy(rx: np.ndarray, view: U.Scenario, res: U.RxResult, wf: U.Waveforms) -> np.ndarray:
    """Per hypothesis: summed |SIC gain|^2 of its pilots, in units of the
    noise a gain estimate would show (Exp(1) per pilot if there is no node)."""
    d, P = view.d, view.cfg.n_pilot
    B, L = d["n_blocks"], d["block_len"]
    y = residual(rx, res, wf, d["fs"])
    sigma2 = float(np.median(np.abs(y) ** 2) / np.log(2.0))
    T = np.zeros(view.k)
    for j, _, _, _, _, gain in res.peel:
        u, b = divmod(j, B)
        if b < P:
            T[u] += abs(gain) ** 2 * L / sigma2
    return T


@dataclass
class BlindResult:
    sic_view: U.Scenario
    sic_f: np.ndarray
    sic: U.RxResult
    tin_view: U.Scenario
    tin_f: np.ndarray
    tin: U.RxResult
    rejected: int           # hypotheses dropped by the post-SIC pilot test


def blind_receive(rx: np.ndarray, scn: U.Scenario, wf: U.Waveforms, rng: np.random.Generator,
                  keep_profiles: bool = True) -> BlindResult:
    d = scn.d
    B, P = d["n_blocks"], scn.cfg.n_pilot
    t_post = float(stats.gamma.isf(P_FA_POST, P))
    first, first_tin = detect(rx, scn, wf, Hyps.empty(B), 0, rng, with_tin=True)
    tin_view = make_view(scn, first_tin)
    tin = U.receive(rx, tin_view, wf, first_tin.f_hz, sic=False, keep_profiles=keep_profiles)
    hyps, rejected = first.take(np.flatnonzero(defer(scn.cfg, d, first))), 0
    for rnd in range(1, ROUNDS):
        if not len(hyps):
            break
        view = make_view(scn, hyps)
        sic = U.receive(rx, view, wf, hyps.f_hz, sic=True, keep_profiles=False)
        keep = pilot_energy(rx, view, sic, wf) >= t_post
        rejected += int((~keep).sum())
        y = residual(rx, sic, wf, d["fs"], keep_hyp=keep, n_blocks=B)
        hyps = hyps.take(np.flatnonzero(keep))
        new, _ = detect(y, scn, wf, hyps, rnd, rng)
        if not len(new):
            break
        hyps = hyps.concat(new.take(np.flatnonzero(defer(scn.cfg, d, new))))
    view = make_view(scn, hyps)
    sic = U.receive(rx, view, wf, hyps.f_hz, sic=True, keep_profiles=keep_profiles)
    keep = pilot_energy(rx, view, sic, wf) >= t_post
    if not keep.all():
        rejected += int((~keep).sum())
        hyps = hyps.take(np.flatnonzero(keep))
        view = make_view(scn, hyps)
        sic = U.receive(rx, view, wf, hyps.f_hz, sic=True, keep_profiles=keep_profiles)
    return BlindResult(view, hyps.f_hz, sic, tin_view, first_tin.f_hz, tin, rejected)


# ----------------------------------------------------------------------------
# LoRa offset estimate when timing is known
# ----------------------------------------------------------------------------

def lora_known_offsets(rx: np.ndarray, scn: U.Scenario, wf: U.Waveforms, n_cand: int = 4) -> np.ndarray:
    """Carrier offset of each LoRa node when every node's timing is given.
    All nodes on a channel send the same up-chirp preamble, so node u's
    dechirped pilots also show a tone for every co-channel node whose preamble
    overlaps them; on its own the strongest tone is often another node's.  A
    tone at offset f seen at u's delay lies on the delay/offset ridge
    rho = f / bin - delay [chips] of the node that sent it, and that node sees
    it at full strength (all of its pilots aligned).  So all nodes' candidate
    tones are pooled per channel and assigned strongest first, each ridge to
    one node."""
    cfg, d = scn.cfg, scn.d
    P, L, fs, s, n, rc = cfg.n_pilot, d["block_len"], d["fs"], d["sps"], d["n"], d["rc"]
    chf = np.asarray(d["channel_freqs_hz"])
    f_bulk = d["centre_doppler_hz"] if cfg.bulk_comp else 0.0
    span = cfg.cfo_span_khz * 1e3
    nfft = 8 * (1 << math.ceil(math.log2(n)))
    ff = sfft.fftfreq(nfft, 1.0 / rc)
    sel = np.abs(ff) <= span
    binw = rc / n
    pool = []                                   # (power, node, offset, ridge)
    out = np.full(scn.k, f_bulk)
    for u in range(scn.k):
        spec = np.zeros(nfft)
        for b in range(P):
            st = scn.block_start(u, b)
            v = rx[st:st + L] * U._mixer(chf[scn.channels[u, b]] + f_bulk, L, fs) * np.conj(wf.block(0, scn.symbols[u, b]))
            spec += np.abs(sfft.fft(v.reshape(n, s).sum(axis=1), n=nfft)) ** 2
        sp = np.where(sel, spec, -1.0)
        peaks = np.flatnonzero((sp > np.roll(sp, 1)) & (sp >= np.roll(sp, -1)) & (sp > 0))
        peaks = peaks[np.argsort(-sp[peaks])][:n_cand]
        for k in peaks:
            ym, y0, yp = spec[(k - 1) % nfft], spec[k], spec[(k + 1) % nfft]
            den = ym - 2 * y0 + yp
            f = f_bulk + ff[k] + (0.5 * (ym - yp) / den if den < 0 else 0.0) * (ff[1] - ff[0])
            pool.append((float(spec[k]), u, f, f / binw - scn.arrival[u] / s))
        if len(peaks):
            out[u] = pool[-len(peaks)][2]           # fallback: strongest tone
    done = np.zeros(scn.k, dtype=bool)
    claimed: dict[int, list[float]] = {}
    for _, u, f, rho in sorted(pool, key=lambda x: -x[0]):
        if done[u]:
            continue
        ch = int(scn.channels[u, 0])
        if any(abs((rho - r + n / 2) % n - n / 2) <= 1.5 for r in claimed.get(ch, [])):
            continue
        out[u], done[u] = f, True
        claimed.setdefault(ch, []).append(rho)
    return out


# ----------------------------------------------------------------------------
# scoring against the truth (evaluation only)
# ----------------------------------------------------------------------------

def match(scn: U.Scenario, view: U.Scenario, res: U.RxResult) -> np.ndarray:
    """For every true node, the hypothesis that is its packet (-1 if missed).
    CodeHop: same code, start within one chip.  LoRa: same channel, start
    within a quarter symbol (a LoRa delay error trades against an offset
    error along the chirp ridge without changing the decoded symbols).  If
    several hypotheses qualify, the one that decoded most symbols wins."""
    s, n, P = scn.d["sps"], scn.d["n"], scn.cfg.n_pilot
    lora = U.is_lora(scn.cfg)
    tol = n * s / 4 if lora else s
    pairs = []
    for u in range(scn.k):
        if lora:
            cand = np.flatnonzero((view.channels[:, 0] == scn.channels[u, 0])
                                  & (np.abs(view.arrival - scn.arrival[u]) <= tol))
        else:
            cand = np.flatnonzero((view.codes == scn.codes[u])
                                  & (np.abs(view.arrival - scn.arrival[u]) <= tol))
        for j in cand:
            ok = int((res.decided[j, P:] == scn.symbols[u, P:]).sum())
            pairs.append((-ok, abs(int(view.arrival[j]) - int(scn.arrival[u])), u, int(j)))
    out = np.full(scn.k, -1)
    used = set()
    for _, _, u, j in sorted(pairs):           # one hypothesis per node and vice versa
        if out[u] < 0 and j not in used:
            out[u] = j
            used.add(j)
    return out
