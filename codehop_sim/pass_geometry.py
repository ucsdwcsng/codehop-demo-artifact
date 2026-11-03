"""Moving-gateway (LEO overhead pass) geometry and time-varying link budget.

The FSMA link budget is a *static snapshot* (nodes drawn on the ground, one
received-power draw each): free-space path loss + received power, no pass. This
module adds the physics that snapshot lacks -- elevation(t), slant-range(t), and
therefore SNR(t) as the satellite sweeps horizon-to-horizon -- while reusing the
same FSPL / received-power model so all schemes see one consistent channel.

Ported faithfully from fsma-satellite-iot's `sensorDistributionAndLinkBudget.m`:
    FSPL   = 20*log10(d) + 20*log10(fc) - 20*log10(c/(4*pi))
    P_rx   = EIRP + G_rx - FSPL - shadow      (shadow ~ N(0, sigma))
Validated in tests against the measured TinyGS trace (Tianqi-26/33): the FSPL
model reproduces measured RSSI from measured slant range.

Geometry (spherical Earth, circular orbit, Earth rotation ignored -- adequate
for a single ~10-min pass): the central angle gamma between the ground node and
the sub-satellite point sweeps at the orbital mean motion n = sqrt(MU/(RE+h)^3).
A pass runs from horizon (elevation 0) to horizon; `max_elev_deg` sets how close
to overhead it is (90 deg = zenith pass; typical passes are lower).
"""
from __future__ import annotations
import numpy as np
from .params import C, RE, MU, ChannelParams
from .channel import thermal_noise_dbm

# 20*log10(4*pi/c); with d[m], fc[Hz] this makes fspl_db the standard FSPL (=-147.55).
FSPL_CONST_DB = 20.0 * np.log10(4.0 * np.pi / C)


def mean_motion(altitude: float) -> float:
    """Orbital angular rate n [rad/s] for a circular LEO at `altitude`."""
    return float(np.sqrt(MU / (RE + altitude) ** 3))


def _central_angle_for_elevation(elev_rad: float, altitude: float) -> float:
    """Central angle gamma (Earth-center angle node<->sub-sat point) that yields
    a given elevation. Closed form from the O-G-S triangle:
        nadir angle alpha = arcsin(RE*cos(eps)/r);  gamma = 90deg - eps - alpha.
    """
    r = RE + altitude
    alpha = np.arcsin(np.clip(RE * np.cos(elev_rad) / r, -1.0, 1.0))
    return np.pi / 2.0 - elev_rad - alpha


def horizon_central_angle(altitude: float) -> float:
    """Central angle at which the satellite is on the horizon (elevation 0)."""
    return float(np.arccos(RE / (RE + altitude)))


def pass_duration(altitude: float, max_elev_deg: float = 40.0) -> float:
    """Horizon-to-horizon visibility time [s] for a pass peaking at max_elev_deg."""
    n = mean_motion(altitude)
    gamma_min = _central_angle_for_elevation(np.radians(max_elev_deg), altitude)
    gamma_h = horizon_central_angle(altitude)
    # along-track angle at the horizons: cos(gamma_h) = cos(gamma_min)*cos(phi_h)
    ratio = np.cos(gamma_h) / np.cos(gamma_min)
    if ratio > 1.0:
        return 0.0  # peak elevation never clears the horizon -> no pass
    phi_h = np.arccos(np.clip(ratio, -1.0, 1.0))
    return float(2.0 * phi_h / n)


def elevation_range_trajectory(t: np.ndarray, altitude: float,
                               max_elev_deg: float = 40.0):
    """Elevation [rad] and slant range [m] over pass times `t` (t=0 = closest
    approach). Off-zenith passes use max_elev_deg < 90. Uses the spherical
    right-triangle approximation cos(gamma) = cos(gamma_min)*cos(n*t)."""
    r = RE + altitude
    n = mean_motion(altitude)
    gamma_min = _central_angle_for_elevation(np.radians(max_elev_deg), altitude)
    cos_gamma = np.clip(np.cos(gamma_min) * np.cos(n * np.asarray(t, float)), -1.0, 1.0)
    gamma = np.arccos(cos_gamma)
    elev = np.arctan2(np.cos(gamma) - RE / r, np.sin(gamma))
    slant = np.sqrt(RE ** 2 + r ** 2 - 2.0 * RE * r * np.cos(gamma))
    return elev, slant


def slant_range_from_elevation(elev_rad, altitude: float):
    """Slant range [m] to a satellite seen at `elev_rad` above the horizon
    (spherical Earth). Inverse: altitude = sqrt(RE^2 + d^2 + 2*d*RE*sin(eps)) - RE.
    """
    s = RE * np.sin(elev_rad)
    return np.sqrt(s ** 2 + altitude ** 2 + 2.0 * RE * altitude) - s


def fspl_db(distance_m, fc_hz: float):
    """Free-space path loss [dB]. Matches the FSMA link-budget formula."""
    return 20.0 * np.log10(distance_m) + 20.0 * np.log10(fc_hz) + FSPL_CONST_DB


def received_power_dbm(distance_m, fc_hz: float, eirp_dbm: float = 22.0,
                       rx_gain_db: float = 0.0):
    """Received power at the satellite [dBm]: EIRP + G_rx - FSPL."""
    return eirp_dbm + rx_gain_db - fspl_db(distance_m, fc_hz)


def snr_over_pass(t: np.ndarray, chan: ChannelParams, max_elev_deg: float = 40.0,
                  bw_hz: float = 125e3, eirp_dbm: float = 22.0,
                  rx_gain_db: float = 0.0, shadow_sigma_db: float = 0.0,
                  rng: np.random.Generator | None = None):
    """SNR(t) [dB] in `bw_hz` over a pass, plus the geometry that produced it.

    Returns (elevation_rad, slant_range_m, snr_db). Deterministic when
    shadow_sigma_db=0; set it (with rng) for lognormal-shadowed Monte Carlo.
    """
    elev, slant = elevation_range_trajectory(t, chan.altitude, max_elev_deg)
    prx = received_power_dbm(slant, chan.fc, eirp_dbm, rx_gain_db)
    if shadow_sigma_db and rng is not None:
        prx = prx - rng.normal(0.0, shadow_sigma_db, size=np.shape(prx))
    snr_db = prx - thermal_noise_dbm(bw_hz, chan.noise_fig_db)
    return elev, slant, snr_db


def doppler_over_pass(t: np.ndarray, chan: ChannelParams,
                      max_elev_deg: float = 40.0):
    """Geometry-consistent Doppler [Hz] over a pass: f_d = -(d/dt slant)*fc/c.
    Positive while approaching, zero at closest approach, negative receding --
    the physical S-curve (more faithful than channel.doppler_trajectory's tanh)."""
    t = np.asarray(t, float)
    _, slant = elevation_range_trajectory(t, chan.altitude, max_elev_deg)
    v_radial = np.gradient(slant, t)
    return -v_radial * chan.fc / C
