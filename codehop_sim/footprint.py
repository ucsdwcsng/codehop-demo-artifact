"""Per-user geometry for a ground cap of terminals under one LEO pass.

Extends ``pass_geometry`` (which tracks a single ground point) to many
terminals.  Same assumptions: spherical Earth, circular orbit, Earth rotation
ignored over the few-millisecond packet and the ~10-minute pass.

Frame (Earth-centred, metres):
  * the orbit lies in the x-z plane; the satellite is at
    ``r_s * (sin(n t), 0, cos(n t))`` so t = 0 is closest approach to the cap
    centre;
  * the cap centre ``G0 = RE * (0, sin(gamma_min), cos(gamma_min))`` sees the
    satellite at ``max_elev_deg`` at t = 0, exactly as in
    ``pass_geometry.elevation_range_trajectory``.

Map coordinates are azimuthal-equidistant around G0: x along-track (direction
of satellite motion), y cross-track (positive toward the ground track), km.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .params import C, RE
from .pass_geometry import _central_angle_for_elevation, mean_motion


@dataclass(frozen=True)
class PassFrame:
    altitude: float          # m
    max_elev_deg: float      # elevation of the cap centre at closest approach

    @property
    def r_s(self) -> float:
        return RE + self.altitude

    @property
    def n(self) -> float:
        return mean_motion(self.altitude)

    @property
    def gamma_min(self) -> float:
        return float(_central_angle_for_elevation(np.radians(self.max_elev_deg),
                                                  self.altitude))

    def basis(self):
        """Unit up-vector at G0 and the two local tangent axes (along, cross)."""
        g = self.gamma_min
        up = np.array([0.0, np.sin(g), np.cos(g)])
        along = np.array([1.0, 0.0, 0.0])
        cross = np.array([0.0, np.cos(g), -np.sin(g)])   # points toward the track
        return up, along, cross

    def sat_state(self, t):
        """Satellite position, velocity, acceleration at time(s) t [s]."""
        t = np.asarray(t, float)
        nt = self.n * t
        pos = self.r_s * np.stack([np.sin(nt), np.zeros_like(nt), np.cos(nt)], -1)
        vel = self.r_s * self.n * np.stack([np.cos(nt), np.zeros_like(nt), -np.sin(nt)], -1)
        acc = -(self.n ** 2) * pos
        return pos, vel, acc


def central_angle_for_elevation(elev_deg: float, altitude: float) -> float:
    return float(_central_angle_for_elevation(np.radians(elev_deg), altitude))


def visibility_half_window(frame: PassFrame, min_elev_deg: float) -> float:
    """Half-length [s] of the interval in which the cap centre sees the
    satellite at or above ``min_elev_deg`` (0 if it never does)."""
    g_e = central_angle_for_elevation(min_elev_deg, frame.altitude)
    ratio = np.cos(g_e) / np.cos(frame.gamma_min)
    if ratio > 1.0:
        return 0.0
    return float(np.arccos(np.clip(ratio, -1.0, 1.0)) / frame.n)


def cap_points(frame: PassFrame, n_points: int, radius_m: float,
               rng: np.random.Generator):
    """Uniform-area samples in a spherical cap of ground radius ``radius_m``
    around G0.  Returns (positions (n,3) [m], map_xy (n,2) [km])."""
    beta = max(radius_m, 0.0) / RE
    cos_t = rng.uniform(np.cos(beta), 1.0, n_points)
    theta = np.arccos(np.clip(cos_t, -1.0, 1.0))
    phi = rng.uniform(0.0, 2.0 * np.pi, n_points)
    return cap_from_polar(frame, theta, phi)


def cap_from_polar(frame: PassFrame, theta, phi):
    up, along, cross = frame.basis()
    theta = np.asarray(theta, float)[:, None]
    phi = np.asarray(phi, float)[:, None]
    dirs = (np.cos(theta) * up
            + np.sin(theta) * (np.cos(phi) * along + np.sin(phi) * cross))
    pos = RE * dirs
    xy = RE / 1e3 * np.hstack([theta * np.cos(phi), theta * np.sin(phi)])
    return pos, xy


def to_map(frame: PassFrame, unit_vecs) -> np.ndarray:
    """Azimuthal-equidistant map coordinates [km] of Earth-centred unit vectors."""
    up, along, cross = frame.basis()
    v = np.atleast_2d(unit_vecs)
    theta = np.arccos(np.clip(v @ up, -1.0, 1.0))
    phi = np.arctan2(v @ cross, v @ along)
    return RE / 1e3 * np.stack([theta * np.cos(phi), theta * np.sin(phi)], -1)


def subsatellite_map(frame: PassFrame, t) -> np.ndarray:
    pos, _, _ = frame.sat_state(np.atleast_1d(t))
    return to_map(frame, pos / frame.r_s)


def link_state(frame: PassFrame, ground_pos, t: float, fc: float) -> dict:
    """Slant range, elevation, Doppler and Doppler rate for each ground point.

    f_d = -(dr/dt) fc / c  (positive while approaching), and with Earth
    rotation ignored r'' = (|v|^2 + d.a - r'^2) / r.
    """
    pos, vel, acc = frame.sat_state(t)
    d = pos[None, :] - np.atleast_2d(ground_pos)
    r = np.linalg.norm(d, axis=1)
    r_dot = d @ vel / r
    r_ddot = (vel @ vel + d @ acc - r_dot ** 2) / r
    up = np.atleast_2d(ground_pos) / np.linalg.norm(ground_pos, axis=1)[:, None]
    sin_el = np.einsum("ij,ij->i", d, up) / r
    return {
        "slant_m": r,
        "elev_deg": np.degrees(np.arcsin(np.clip(sin_el, -1.0, 1.0))),
        "doppler_hz": -r_dot * fc / C,
        "doppler_rate_hz_s": -r_ddot * fc / C,
        "delay_s": r / C,
    }


def doppler_curves(frame: PassFrame, ground_pos, t_grid, fc: float) -> np.ndarray:
    """Doppler [Hz] for each ground point (rows) over ``t_grid`` (columns)."""
    pos, vel, _ = frame.sat_state(np.asarray(t_grid, float))
    g = np.atleast_2d(ground_pos)
    d = pos[None, :, :] - g[:, None, :]
    r = np.linalg.norm(d, axis=2)
    r_dot = np.einsum("utk,tk->ut", d, vel) / r
    return -r_dot * fc / C
