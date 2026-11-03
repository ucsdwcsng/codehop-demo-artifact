"""Spreading/chip sequence families and correlation metrics.

The verdict memo and the hardware notes both converge on PRN as the right
family for asynchronous, aperiodic, poorly-timed operation (structured
CAZAC/m-sequence advantages are periodic-correlation only). We therefore make
PRN primary and provide m-sequences for the periodic-ACF comparison that
demonstrates exactly that point.
"""
from __future__ import annotations
import numpy as np

# Primitive polynomials (feedback tap positions, 1-indexed high->low) for
# maximal-length LFSRs, standard references. Length = 2**degree - 1.
_PRIMITIVE_TAPS = {
    5: [5, 2], 6: [6, 1], 7: [7, 3], 8: [8, 4, 3, 2], 9: [9, 4], 10: [10, 3],
}


def random_prn(n: int, rng: np.random.Generator) -> np.ndarray:
    """Bernoulli +-1 sequence of length n. First chip fixed to +1 (matches
    the hardware notes' bit-1 constraint)."""
    b = rng.integers(0, 2, size=n) * 2 - 1
    b[0] = 1
    return b.astype(np.float64)


def msequence(degree: int) -> np.ndarray:
    """Maximal-length LFSR sequence, length 2**degree-1, mapped to +-1."""
    if degree not in _PRIMITIVE_TAPS:
        raise ValueError(f"no primitive polynomial tabulated for degree {degree}")
    taps = _PRIMITIVE_TAPS[degree]
    state = [1] * degree
    n = (1 << degree) - 1
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        out[i] = 1.0 if state[-1] else -1.0
        fb = 0
        for t in taps:
            fb ^= state[t - 1]
        state = [fb] + state[:-1]
    return out


def walsh_code(n: int, index: int) -> np.ndarray:
    """Row `index` of the order-n Walsh-Hadamard matrix as +-1 (n a power of 2).
    Walsh rows are PERFECTLY orthogonal when time-aligned but lose orthogonality
    under any timing offset -- the reason CodeHop uses PRN/Gold for asynchronous
    (satellite-delay) operation. H[i,j] = (-1)^popcount(i & j)."""
    if n & (n - 1) != 0:
        raise ValueError("walsh length must be a power of 2")
    j = np.arange(n)
    b = np.bitwise_and(index, j)
    pc = np.zeros(n, dtype=int)
    while np.any(b):
        pc += b & 1
        b >>= 1
    return np.where(pc & 1, -1.0, 1.0).astype(np.float64)


def gold_family(degree: int = 7, decimation: int = 3) -> list[np.ndarray]:
    """Return a preferred-pair Gold family of length ``2**degree - 1``.

    For odd ``degree``, decimating an m-sequence by ``2**k + 1`` with
    ``gcd(k, degree) == 1`` gives a preferred pair.  The default ``k=1``
    (decimation 3) produces the length-127 family used by the paper harness.
    In the +-1 representation, XOR is pointwise multiplication.
    """
    n = (1 << degree) - 1
    if np.gcd(decimation, n) != 1:
        raise ValueError("decimation must be coprime with sequence period")
    a = msequence(degree)
    b = a[(decimation * np.arange(n)) % n]
    return [a, b] + [a * np.roll(b, shift) for shift in range(n)]


def ccsk_shift(code: np.ndarray, symbol: int) -> np.ndarray:
    """Map one CCSK symbol to a cyclic shift of ``code``."""
    code = np.asarray(code, dtype=float)
    if code.ndim != 1 or len(code) == 0:
        raise ValueError("code must be a non-empty one-dimensional sequence")
    return np.roll(code, int(symbol) % len(code))


def code_of_length(n: int, rng: np.random.Generator, family: str = "prn") -> np.ndarray:
    """Return a code of (approximately) length n. m-sequences snap to 2**k-1."""
    if family == "prn":
        return random_prn(n, rng)
    if family == "msequence":
        degree = int(round(np.log2(n + 1)))
        return msequence(degree)
    raise ValueError(f"unknown family {family}")


def periodic_acf(c: np.ndarray) -> np.ndarray:
    """Circular autocorrelation, all N shifts."""
    n = len(c)
    C = np.fft.fft(c)
    r = np.fft.ifft(C * np.conj(C)).real
    return r / n  # normalized so zero-lag = mean power (=1 for +-1)


def aperiodic_acf(c: np.ndarray) -> np.ndarray:
    """Linear (aperiodic) autocorrelation, lags -(N-1)..(N-1)."""
    return np.correlate(c, c, mode="full")


def crosscorr(a: np.ndarray, b: np.ndarray, circular: bool = False) -> np.ndarray:
    """Cross-correlation. circular=True => periodic (needs equal length)."""
    if circular:
        A, B = np.fft.fft(a), np.fft.fft(b)
        return np.fft.ifft(A * np.conj(B)).real
    return np.correlate(a, b, mode="full")


def peak_to_sidelobe_db(c: np.ndarray, periodic: bool = True) -> float:
    """PSL in dB from the autocorrelation (peak / max off-peak sidelobe)."""
    if periodic:
        r = np.abs(periodic_acf(c)) * len(c)  # unnormalize: peak = N
        peak = r[0]
        side = np.max(r[1:])
    else:
        r = np.abs(aperiodic_acf(c))
        mid = len(c) - 1
        peak = r[mid]
        side = np.max(np.delete(r, mid))
    if side <= 0:
        return np.inf
    return 20.0 * np.log10(peak / side)


def max_crosscorr(a: np.ndarray, b: np.ndarray, periodic: bool = False) -> float:
    """Peak absolute cross-correlation, normalized by sqrt(Na*Nb)."""
    x = np.abs(crosscorr(a, b, circular=periodic))
    return float(np.max(x) / np.sqrt(len(a) * len(b)))
