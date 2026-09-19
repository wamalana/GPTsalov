"""Run a test module under the previous conservative Testnet limits.

Those tests pin invariants (budget halving, daily room, notional ceilings,
lowest-leverage choice) with concrete numbers from the 0.25 USDT setting.
The invariants are unchanged by the 2 USDT data-collection setting; only the
constants moved, so the tests keep checking them at the old values.
"""
from unittest import mock
from gptsalov import testnet_limits as L
from gptsalov.core import dec

CONSERVATIVE = dict(RISK_CAP=dec('.25'), RISK_FRACTION=dec('.005'), NOTIONAL_CAP=dec('25'),
                    CASH_FRACTION=dec('.5'), MAX_LEVERAGE=2, DAILY_LOSS=dec('.02'), MAX_DRAWDOWN=dec('.08'))
_patch = mock.patch.multiple(L, **CONSERVATIVE)


def setUpModule():
    _patch.start()


def tearDownModule():
    _patch.stop()
