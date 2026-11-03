"""Parameter dataclasses and physical constants.

All defaults trace to docs/research/* and the hardware feasibility envelope
(SX127x-class constant-envelope FSK/GFSK, 920 MHz, 125 kHz channel).
"""
from __future__ import annotations
from dataclasses import dataclass, field

C = 299_792_458.0        # m/s
RE = 6_371_000.0         # m, Earth radius
MU = 3.986e14            # m^3/s^2, Earth gravitational parameter
K_BOLTZ = 1.380649e-23   # J/K
T0 = 290.0               # K, reference noise temperature


@dataclass
class PHYParams:
    fc: float = 920e6            # center frequency [Hz]
    chip_rate: float = 50e3      # chips/s (LMIC FSK path default); 100e3 sweep option
    deviation: float = 25e3      # FSK peak deviation [Hz]  -> h = 2*dev/chip_rate
    bt: float = 0.5              # Gaussian BT (GFSK); None => plain CPFSK
    channel_bw: float = 125e3    # per-channel RF bandwidth [Hz]
    payload_bits: int = 64       # useful satellite-IoT message [32..128]
    code_lengths: tuple = (32, 64, 128, 256, 512, 1024)

    @property
    def mod_index(self) -> float:
        return 2.0 * self.deviation / self.chip_rate  # h; MSK when 0.5

    def chip_time(self) -> float:
        return 1.0 / self.chip_rate


@dataclass
class ChannelParams:
    altitude: float = 550e3      # LEO [m]
    pass_dur: float = 450.0      # s (300-600)
    fc: float = 920e6
    doppler_max: float = 23e3    # |f_d| max [Hz] at 920 MHz
    doppler_rate: float = 300.0  # Hz/s (200-350)
    cfo_ppm: float = 10.0        # cheap XO 10/20; TCXO 2
    snr_db_125k: float = -10.0   # in 125 kHz
    noise_fig_db: float = 3.0

    def cfo_hz(self) -> float:
        return self.cfo_ppm * 1e-6 * self.fc  # +/- one-sigma bound


@dataclass
class NearFarParams:
    spread_db: float = 15.0      # max deterministic (slant-range) spread
    sigma_db: float = 4.0        # lognormal shadowing sigma (3-6)
    equal_power: bool = False    # worst case: all users equal power


@dataclass
class TrafficParams:
    # offered load G in "packets per packet-duration per channel" (normalized)
    load_grid: tuple = tuple(round(x, 4) for x in
                             [0.01 * (1.6 ** i) for i in range(0, 13)])  # ~0.01..~4
    n_channels: int = 8          # 8 x 125 kHz = 1 MHz
    seed: int = 0


@dataclass
class MACParams:
    """Energy/coupling model knobs. All costs from docs/research memos."""
    e_tx_j: float = 0.064        # J per 160 ms TX @ ~0.4 W (SX126x-class)
    e_rx_listen_j: float = 0.036 # J per ~1 s beacon/ACK listen window
    e_cad_j: float = 0.001       # J per gating CAD/probe-listen (~8 ms @ ~86 mW,
                                 # FSMA SF10 CAD); the honest cost of link-aware
                                 # gating -- without it gating looks free
    max_tx: int = 8              # blind-repeat / feedback retry cap
    fixed_repeats: int = 4       # Kineis-style 1+3
    recovery_depth: int = 4      # gateway resolves up to m overlaps (SIC/corr)
    use_rx_energy: bool = True   # honest: feedback costs listen energy
    duty_cycle: float = 0.01     # EU 1%/hr cap on airtime (None => US no-cap)
    sic_error_prop: float = 0.0  # fraction of SIC layers that inject error (0..1)


@dataclass
class HardwareEnvelope:
    """Feasible parameter ranges for RadioLib on SX1276/RFM95 + Adafruit
    Feather M0 (SAMD21 48 MHz) / RP2040 (133 MHz, PIO). Supersedes the
    Arduino-LMIC constraints (which whitened payloads and capped ~63 B/FIFO).
    RadioLib exposes setBitRate/setFrequencyDeviation/setDataShaping/
    setPreambleLength(0)/setSyncWord(off)/setCrcFiltering(false)/
    setEncoding(NRZ)/fixedPacketLengthMode + transmitDirect (continuous)."""
    bitrate_min: float = 0.5e3          # RadioLib SX127x FSK setBitRate range
    bitrate_max: float = 300e3          # kcps upper bound (SX1276 FSK)
    deviation_max: float = 200e3        # <200 kHz (RadioLib constraint)
    bt_options: tuple = (0.3, 0.5, 1.0)
    packet_mode_max_bytes: int = 63     # single-FIFO packet mode
    direct_mode_unlimited: bool = True  # continuous mode -> arbitrary length
    # max reliable chip rate for arbitrary-length (direct) emission per board:
    m0_direct_chiprate_max: float = 40e3    # SAMD21 bit-bang / IRQ limited [must be measured]
    rp2040_direct_chiprate_max: float = 250e3  # PIO-clocked DIO2/DCLK [must be measured]

    def feasible_code_length(self, n_chips: int, chip_rate: float,
                             board: str = "rp2040") -> bool:
        """A length-N code at chip_rate: packet mode if <=63 B, else needs
        direct mode (board chip-rate limited)."""
        if n_chips <= self.packet_mode_max_bytes * 8:
            return chip_rate <= self.bitrate_max
        cap = self.rp2040_direct_chiprate_max if board == "rp2040" else self.m0_direct_chiprate_max
        return self.direct_mode_unlimited and chip_rate <= cap


@dataclass
class SimConfig:
    phy: PHYParams = field(default_factory=PHYParams)
    chan: ChannelParams = field(default_factory=ChannelParams)
    nearfar: NearFarParams = field(default_factory=NearFarParams)
    traffic: TrafficParams = field(default_factory=TrafficParams)
    mac: MACParams = field(default_factory=MACParams)
    hw: HardwareEnvelope = field(default_factory=HardwareEnvelope)
    seed: int = 0
