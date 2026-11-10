"""Exact Kubernetes-compatible CPU quantity conversions."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Final

INT64_MAX: Final = (1 << 63) - 1
NANOCPUS_PER_MILLI: Final = 1_000_000
MAX_CPU_MILLICORES: Final = INT64_MAX // NANOCPUS_PER_MILLI
CPU_SHARES_PER_CORE: Final = 1024
CPU_MIN_SHARES: Final = 2
CPU_MAX_SHARES: Final = 262_144
CPU_PERIOD_MICROSECONDS: Final = 100_000
CPU_MIN_QUOTA_MICROSECONDS: Final = 1_000

_CPU_QUANTITY_RE = re.compile(
    r"(?P<sign>[+-]?)"
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:(?P<suffix>n|u|m|k|M|G|T|P|E)|[eE](?P<exponent>[+-]?\d+))?"
    r"\Z"
)
_DECIMAL_SI_EXPONENTS: Final = {
    "n": -9,
    "u": -6,
    "m": -3,
    "k": 3,
    "M": 6,
    "G": 9,
    "T": 12,
    "P": 15,
    "E": 18,
}
_DEFAULT_DIVISOR = object()


class CPUQuantityError(ValueError):
    """Raised when a CPU quantity cannot be represented by the supported contract."""


def _quantity_text(value: object) -> str:
    if isinstance(value, bool):
        raise CPUQuantityError("CPU quantity must not be a boolean")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CPUQuantityError("CPU quantity must be finite")
        text = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise CPUQuantityError("CPU quantity must be finite")
        text = str(value)
    elif isinstance(value, int | str):
        text = str(value)
    else:
        raise CPUQuantityError("CPU quantity must be a string, integer, finite float, or Decimal")
    if not text or text != text.strip():
        raise CPUQuantityError(
            "CPU quantity must be nonempty and contain no surrounding whitespace"
        )
    return text


def cpu_to_millicores(value: object) -> int:
    """Parse a positive DecimalSI CPU quantity into exact integer millicores.

    Kubernetes container CPU resources support no precision finer than one
    millicore. Spellings using nano- or micro-CPU are accepted only when their
    resolved value is an exact whole millicore.
    """

    text = _quantity_text(value)
    match = _CPU_QUANTITY_RE.fullmatch(text)
    if match is None:
        raise CPUQuantityError(f"invalid DecimalSI CPU quantity: {text!r}")
    if match.group("sign") == "-":
        raise CPUQuantityError("CPU quantity must be greater than zero")

    number = str(match.group("number"))
    whole, dot, fractional = number.partition(".")
    digits = f"{whole or '0'}{fractional if dot else ''}"
    try:
        coefficient = int(digits)
    except ValueError as exc:
        raise CPUQuantityError(f"invalid CPU quantity magnitude: {text!r}") from exc
    if coefficient == 0:
        raise CPUQuantityError("CPU quantity must be greater than zero")

    suffix_value = match.group("suffix")
    suffix = str(suffix_value) if suffix_value is not None else None
    exponent_value = match.group("exponent")
    exponent_text = str(exponent_value) if exponent_value is not None else None
    quantity_exponent: int = (
        int(exponent_text)
        if exponent_text is not None
        else _DECIMAL_SI_EXPONENTS.get(suffix or "", 0)
    )
    millicore_exponent = quantity_exponent - len(fractional) + 3
    millicores: int

    if millicore_exponent >= 0:
        coefficient_digits = len(str(coefficient))
        maximum_digits = len(str(MAX_CPU_MILLICORES))
        if coefficient_digits + millicore_exponent > maximum_digits:
            raise CPUQuantityError("CPU quantity exceeds signed-int64 nanoCPU range")
        millicores = coefficient * (10**millicore_exponent)
    else:
        divisor_exponent = -millicore_exponent
        if divisor_exponent > len(str(coefficient)):
            raise CPUQuantityError("CPU quantity precision is finer than one millicore")
        divisor = 10**divisor_exponent
        if coefficient % divisor:
            raise CPUQuantityError("CPU quantity precision is finer than one millicore")
        millicores = coefficient // divisor

    if millicores <= 0:
        raise CPUQuantityError("CPU quantity must be greater than zero")
    if millicores > MAX_CPU_MILLICORES:
        raise CPUQuantityError("CPU quantity exceeds signed-int64 nanoCPU range")
    return millicores


def millicores_to_decimal_cores(millicores: int) -> Decimal:
    """Return canonical plain-decimal cores for a validated millicore value."""

    if isinstance(millicores, bool) or not isinstance(millicores, int) or millicores <= 0:
        raise CPUQuantityError("millicores must be a positive integer")
    if millicores > MAX_CPU_MILLICORES:
        raise CPUQuantityError("CPU quantity exceeds signed-int64 nanoCPU range")
    whole, fractional = divmod(millicores, 1000)
    if not fractional:
        return Decimal(str(whole))
    return Decimal(f"{whole}.{fractional:03d}".rstrip("0"))


def cpu_to_decimal_cores(value: object) -> Decimal:
    """Parse a CPU quantity and return canonical exact Decimal cores."""

    return millicores_to_decimal_cores(cpu_to_millicores(value))


def cpu_to_nano_cpus(value: object) -> int:
    """Return Docker's exact signed-int64 nanoCPU value."""

    return cpu_to_millicores(value) * NANOCPUS_PER_MILLI


def cpu_to_shares(value: object) -> int:
    """Return Kubernetes-compatible CFS shares for a CPU request."""

    shares = (cpu_to_millicores(value) * CPU_SHARES_PER_CORE) // 1000
    return min(CPU_MAX_SHARES, max(CPU_MIN_SHARES, shares))


def cpu_to_quota(
    value: object,
    *,
    period: int = CPU_PERIOD_MICROSECONDS,
) -> int:
    """Return Kubernetes-compatible CFS quota microseconds for a CPU limit."""

    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise CPUQuantityError("CPU period must be a positive integer")
    if period > INT64_MAX:
        raise CPUQuantityError("CPU period exceeds signed-int64 range")
    quota = (cpu_to_millicores(value) * period) // 1000
    quota = max(CPU_MIN_QUOTA_MICROSECONDS, quota)
    if quota > INT64_MAX:
        raise CPUQuantityError("CPU quota exceeds signed-int64 range")
    return quota


def divide_cpu_quantity_ceil(
    value: object,
    divisor: object = _DEFAULT_DIVISOR,
) -> int:
    """Divide CPU quantities with the ceiling semantics of resourceFieldRef.

    An omitted divisor means one CPU. Any explicitly supplied divisor,
    including zero or ``None``, must be a valid positive CPU quantity.
    """

    value_millicores = cpu_to_millicores(value)
    divisor_millicores = 1000 if divisor is _DEFAULT_DIVISOR else cpu_to_millicores(divisor)
    return (value_millicores + divisor_millicores - 1) // divisor_millicores
