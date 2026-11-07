"""Exact CPU quantity parsing and conversion contracts."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ae.controller.quantities import (
    CPU_MAX_SHARES,
    CPU_MIN_SHARES,
    CPU_PERIOD_MICROSECONDS,
    MAX_CPU_MILLICORES,
    CPUQuantityError,
    cpu_to_decimal_cores,
    cpu_to_millicores,
    cpu_to_nano_cpus,
    cpu_to_quota,
    cpu_to_shares,
    divide_cpu_quantity_ceil,
)
from ae.controller.spec import ResourceQuantities


@pytest.mark.parametrize(
    ("raw", "expected_millicores"),
    [
        ("1m", 1),
        ("1000000n", 1),
        ("1000u", 1),
        ("250m", 250),
        ("500m", 500),
        ("0.5", 500),
        (0.5, 500),
        (Decimal("0.5"), 500),
        ("1", 1000),
        (1, 1000),
        ("1000m", 1000),
        ("1.001", 1001),
        ("1e-3", 1),
        ("1E-3", 1),
        ("0.001k", 1000),
        ("0.000001M", 1000),
        ("+500m", 500),
    ],
)
def test_cpu_quantity_parses_decimal_si_exactly(raw: object, expected_millicores: int) -> None:
    assert cpu_to_millicores(raw) == expected_millicores


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        "",
        " 1m",
        "1m ",
        "NaN",
        "Infinity",
        float("nan"),
        float("inf"),
        "-1m",
        "0",
        "0m",
        "0.1m",
        "0.0005",
        "999999n",
        "999u",
        "1Ki",
        "1K",
        "1e",
        "cpu",
        object(),
    ],
)
def test_cpu_quantity_rejects_invalid_or_unsupported_inputs(raw: object) -> None:
    with pytest.raises(CPUQuantityError):
        cpu_to_millicores(raw)


def test_cpu_quantity_enforces_signed_int64_nanocpu_range() -> None:
    maximum = "9223372036.854"
    assert cpu_to_millicores(maximum) == MAX_CPU_MILLICORES
    assert cpu_to_nano_cpus(maximum) == MAX_CPU_MILLICORES * 1_000_000

    with pytest.raises(CPUQuantityError, match="signed-int64 nanoCPU range"):
        cpu_to_millicores("9223372036.855")


def test_cpu_quantity_converts_to_canonical_decimal_cores() -> None:
    assert cpu_to_decimal_cores("500m") == Decimal("0.5")
    assert cpu_to_decimal_cores("1000m") == Decimal("1")
    assert cpu_to_decimal_cores("1001m") == Decimal("1.001")

    quantities = ResourceQuantities(cpu="500m")
    assert quantities.cpu == Decimal("0.5")
    assert quantities.quantity_map()["cpu"] == "0.5"

    with pytest.raises(ValidationError, match="finer than one millicore"):
        ResourceQuantities(cpu="0.0005")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1m", CPU_MIN_SHARES),
        ("2m", CPU_MIN_SHARES),
        ("250m", 256),
        ("1", 1024),
        ("256", CPU_MAX_SHARES),
        ("257", CPU_MAX_SHARES),
    ],
)
def test_cpu_shares_match_kubernetes_integer_bounds(raw: object, expected: int) -> None:
    assert cpu_to_shares(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1m", 1000),
        ("9m", 1000),
        ("0.009", 1000),
        ("10m", 1000),
        ("18m", 1800),
        ("0.018", 1800),
        ("500m", 50_000),
        ("1", CPU_PERIOD_MICROSECONDS),
        ("1001m", 100_100),
    ],
)
def test_cpu_quota_is_exact_with_kubernetes_minimum(raw: object, expected: int) -> None:
    assert cpu_to_quota(raw) == expected


def test_resource_field_ref_division_uses_ceiling_and_strict_divisors() -> None:
    assert divide_cpu_quantity_ceil("500m") == 1
    assert divide_cpu_quantity_ceil("1500m") == 2
    assert divide_cpu_quantity_ceil("500m", "1m") == 500
    assert divide_cpu_quantity_ceil("1001m", "250m") == 5

    with pytest.raises(CPUQuantityError, match="greater than zero"):
        divide_cpu_quantity_ceil("500m", "0")
    with pytest.raises(CPUQuantityError):
        divide_cpu_quantity_ceil("500m", None)
