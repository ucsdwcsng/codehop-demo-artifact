"""Level-2 constant-envelope GFSK/CPFSK baseband generation.

Physically faithful to what an SX127x FSK/GFSK modem emits: chips -> NRZ ->
(optional Gaussian BT filter) -> instantaneous frequency +-deviation -> phase
integral -> exp(j*phase). Envelope is constant by construction (no I/Q
amplitude shaping), matching the hardware constraint.
"""
from __future__ import annotations
import numpy as np
from .params import PHYParams


def _gaussian_pulse(bt: float, sps: int, span: int = 4) -> np.ndarray:
    """Gaussian filter impulse response for a given BT, sampled at sps/chip."""
    t = np.arange(-span * sps, span * sps + 1) / sps  # in chips
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-(t ** 2) / (2 * sigma ** 2))
    return g / np.sum(g)


def gfsk_waveform(chips: np.ndarray, phy: PHYParams, sps: int = 8,
                  bt: float | None = None) -> np.ndarray:
    """Return the complex constant-envelope baseband for a +-1 chip sequence.

    fs = sps * chip_rate. Peak instantaneous deviation = phy.deviation.
    """
    if bt is None:
        bt = phy.bt
    nrz = np.repeat(chips.astype(np.float64), sps)
    if bt is not None and bt > 0:
        g = _gaussian_pulse(bt, sps)
        nrz = np.convolve(nrz, g, mode="same")
    fs = sps * phy.chip_rate
    f_inst = phy.deviation * nrz            # +-deviation Hz
    phase = 2 * np.pi * np.cumsum(f_inst) / fs
    return np.exp(1j * phase)


def upchirp_waveform(n_chips: int, phy: PHYParams, sps: int = 8,
                     bandwidth: float | None = None) -> np.ndarray:
    """Constant-envelope linear up-chirp (CSS/FMCW) over n_chips*sps samples with
    time-bandwidth product ~ n_chips, so a matched filter's processing gain is N
    -- the fair CSS comparison point for Experiment A. bandwidth defaults to
    chip_rate so occupied BW and energy (= L, unit amplitude) match the PRN/MSK
    waveforms."""
    L = n_chips * sps
    fs = sps * phy.chip_rate
    if bandwidth is None:
        bandwidth = phy.chip_rate
    t = np.arange(L) / fs
    T = L / fs
    k = bandwidth / T                        # sweep rate [Hz/s]; B*T = n_chips
    phase = 2 * np.pi * (-bandwidth / 2 * t + 0.5 * k * t ** 2)
    return np.exp(1j * phase)


def sample_rate(phy: PHYParams, sps: int = 8) -> float:
    return sps * phy.chip_rate


def occupied_bw_estimate(phy: PHYParams) -> float:
    """Carson-rule-ish occupied bandwidth for the GFSK signal."""
    return 2 * (phy.deviation + phy.chip_rate / 2)
