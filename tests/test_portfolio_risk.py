"""Owner decision 2026-09-20: 2 USDT per trade, 4 USDT across open trades.
Previously the per-trade cap was divided by free slots (UNIUSDT sized at 0.60)."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gptsalov.core import dec, Signal, BAR_MS
from gptsalov import testnet_pilot as p
from gptsalov.adaptive_risk import portfolio_room


def run(sides, reserved=dec(0), equity='50', day_start='50'):
    t=(1789602300000//BAR_MS)*BAR_MS+50000; stamp=t//BAR_MS*BAR_MS-1
    syms=[f'S{i}USDT' for i in range(len(sides))]
    rows=[dict(symbol=s, market='USD-M', quote='USDT', contract='PERPETUAL', decision='CANDIDATE',
               candle_close_ms=stamp, observed_ms=t, score=10-i, volume=100000000,
               direction='LONG' if sides[i]==1 else 'SHORT') for i, s in enumerate(syms)]
    book=SimpleNamespace(s={'equity':equity, 'day_start':day_start, 'high_water':'50', 'seen_ms':None,
                            'policy':{'risk_model':False}}, save=lambda *a: None)
    demo=[dict(symbol=s, status='TRADING', contractType='PERPETUAL', quoteAsset='USDT', marginAsset='USDT') for s in syms]
    api=SimpleNamespace(call=lambda m, path, **kw: {'symbols':demo} if path.endswith('exchangeInfo') else {'price':'100'})
    public=SimpleNamespace(get=lambda path, **kw: {'symbols':demo} if path.endswith('exchangeInfo')
                           else ([] if path.endswith('klines') else [{'symbol':s} for s in syms]))
    side_of=dict(zip(syms, sides))
    def signal(sym, bars):
        s=side_of[sym]; return Signal(sym, s, stamp, dec(100), dec(100-s), dec(100+3*s), dec(2))
    caps=[]
    def sizing(sig, ref, equity, rule, cfg, risk_cap):
        caps.append(risk_cap)
        return SimpleNamespace(entry=dec(100), qty=dec('.1'), target=dec(103), stop=dec(99),
                               notional=dec(10), risk=risk_cap)  # uses the full allowance
    with patch.object(p, 'now_ms', return_value=t), \
         patch('gptsalov.market_scanner.read', return_value={'status':'current', 'started_ms':t-10000, 'rows':rows}), \
         patch('gptsalov.market_scanner.classify', return_value={'status':'pending'}), \
         patch.object(p, 'closed_bars', return_value=[SimpleNamespace(close_ms=stamp)]), \
         patch.object(p, 'strategy', side_effect=signal), \
         patch('gptsalov.multiagent_gate.evaluate', return_value=(True, {})), \
         patch.object(p.Rules, 'from_exchange', return_value=None), \
         patch.object(p, 'adaptive_stop', side_effect=lambda sig, bars: (sig, {})), \
         patch.object(p, 'buffered_size', side_effect=sizing), \
         patch.object(p, 'reward_risk', return_value={'net_rr':'2'}):
        p.multi_candidate(api, public, book, limit=3, risk_reserved=reserved)
    return caps, book.s['last_selection']


class PortfolioRisk(unittest.TestCase):
    def test_each_trade_gets_full_two_usdt(self):
        caps, sel=run([1, -1])
        self.assertEqual(caps, [dec(2), dec(2)])
        self.assertEqual(len(sel['selected']), 2)

    def test_open_risk_reduces_room_but_not_below_trade_cap_until_exhausted(self):
        caps, _=run([-1], reserved=dec(2))
        self.assertEqual(caps, [dec(2)])
        caps, sel=run([-1], reserved=dec('3.5'))
        self.assertEqual(caps, [dec('.5')])
        caps, sel=run([-1], reserved=dec(4))
        self.assertEqual(caps, [])
        self.assertEqual(list(sel['rejected'].values()), ['PORTFOLIO_RISK_EXHAUSTED'])

    def test_daily_room_bounds_the_total(self):
        # day start 50, equity 47: daily lock at 45 -> 2 USDT room for everything.
        self.assertEqual(portfolio_room({'equity':'47', 'day_start':'50', 'high_water':'50'}), dec(2))
        self.assertEqual(portfolio_room({'equity':'50', 'day_start':'50', 'high_water':'50'}), dec(4))


if __name__=='__main__':
    unittest.main()
