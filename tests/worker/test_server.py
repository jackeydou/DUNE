"""`swarmeval-worker`'s command line."""

import argparse

import pytest

from swarmeval.worker.server import lease_seconds


def test_a_lease_is_a_finite_number_of_seconds_above_zero() -> None:
    assert lease_seconds("30") == 30.0
    assert lease_seconds("0.5") == 0.5
    for bad in ("0", "-1", "nan", "inf", "soon"):
        with pytest.raises(argparse.ArgumentTypeError, match=f"`{bad}`"):
            lease_seconds(bad)
