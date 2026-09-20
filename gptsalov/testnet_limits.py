"""Single source of truth for Testnet pilot risk limits (Testnet only; no production path).

2026-09-19 data-collection setting chosen by the owner: 2 USDT modeled risk per
trade on 50 USDT virtual capital (4%). Loss locks are widened proportionally so
one or two stops do not end the batch. These are NOT recommended live settings:
core.Config still enforces 0.5% risk / 2% daily / 8% drawdown for paper and any
future live path. Revert by restoring the commented previous values.
"""
from .core import dec

VIRTUAL_EQUITY = dec('50')
RISK_CAP = dec('2')            # per trade; previous: 0.25
PORTFOLIO_RISK_CAP = dec('4')  # all open trades together (8%); max 2 open (one per side)
RISK_FRACTION = dec('0.04')    # previous: 0.005 of min(equity, 50)
NOTIONAL_CAP = dec('100')      # previous: 25
CASH_FRACTION = dec('0.9')     # share of virtual equity usable as margin; previous: 0.5
MAX_LEVERAGE = 3               # previous: 2 (needed so 100 notional fits in 45 margin)
DAILY_LOSS = dec('0.10')       # previous: 0.02 -> daily lock at 90% of day start
MAX_DRAWDOWN = dec('0.25')     # previous: 0.08 -> review lock at 75% of high water
