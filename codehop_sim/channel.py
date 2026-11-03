"""LEO channel impairments: Doppler trajectory, CFO, AWGN, near-far power draw."""
from __future__ import annotations
import numpy as np
from .params import C, RE, MU, ChannelParams, NearFarParams


def orbital_velocity(altitude: float) -> float:
    return float(np.sqrt(MU / (RE + altitude)))


def doppler_trajectory(t: np.ndarray, chan: ChannelParams) -> np.ndarray:
    """Radial-velocity Doppler over a pass, for a zenith-passing satellite.

    Uses the standard in-plane approximation f_d(t) = f_max * cos(theta(t))
    where the satellite sweeps from horizon to horizon; the useful, checkable
    property is the near-linear zero-crossing at closest approach with slope
    ~ doppler_rate. We synthesize a smooth arctan-like curve matched to
    (doppler_max, doppler_rate)."""
    tc = chan.pass_dur / 2.0
    # slope at closest approach = doppler_rate; scale time so d/dt at tc matches
    k = chan.doppler_rate / chan.doppler_max
    return chan.doppler_max * np.tanh(-k * (t - tc))


def add_awgn(sig: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    """Add complex AWGN for a target SNR (signal power / noise power)."""
    p_sig = np.mean(np.abs(sig) ** 2)
    if p_sig == 0:
        p_sig = 1.0
    p_noise = p_sig / (10 ** (snr_db / 10))
    n = (rng.standard_normal(sig.shape) + 1j * rng.standard_normal(sig.shape))
    n *= np.sqrt(p_noise / 2)
    return sig + n


def apply_cfo(sig: np.ndarray, cfo_hz: float, fs: float,
              rate_hz_s: float = 0.0) -> np.ndarray:
    """Apply a (possibly drifting) carrier frequency offset."""
    n = len(sig)
    t = np.arange(n) / fs
    phase = 2 * np.pi * (cfo_hz * t + 0.5 * rate_hz_s * t ** 2)
    return sig * np.exp(1j * phase)


def apply_timing_offset(sig: np.ndarray, offset_samples: int) -> np.ndarray:
    """Circularly shift to emulate an unknown arrival time."""
    return np.roll(sig, offset_samples)


def nearfar_powers_db(k: int, nf: NearFarParams,
                      rng: np.random.Generator) -> np.ndarray:
    """Draw k received power levels [dB, relative]. Slant-range spread is a
    uniform band [0, spread_db]; shadowing adds lognormal sigma_db on top.
    equal_power collapses to the worst case (all 0 dB)."""
    if nf.equal_power:
        return np.zeros(k)
    base = rng.uniform(0.0, nf.spread_db, size=k)
    shadow = rng.normal(0.0, nf.sigma_db, size=k)
    return base + shadow


def thermal_noise_dbm(bw: float, noise_fig_db: float) -> float:
    from .params import K_BOLTZ, T0
    return 10 * np.log10(K_BOLTZ * T0 * bw * 1e3) + noise_fig_db
