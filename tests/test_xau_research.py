import unittest
from decimal import Decimal as D
from gptsalov.xau_research import size,timestamp

class XauTests(unittest.TestCase):
    def test_lot_rounds_down_including_costs(self):
        c={k:D(v) for k,v in dict(tick_size='.01',tick_value_loss_per_lot='1',commission_roundtrip_usd_per_lot='7',slippage_price_per_side='.1',volume_min='.01',volume_max='100',volume_step='.01',risk_usd='2').items()}
        lots=size(c,D('1'))
        self.assertEqual(lots,D('.01'))
        self.assertLessEqual(lots*D('127'),c['risk_usd'])
        c['risk_usd']=D('.25');self.assertEqual(size(c,D('1')),0)

    def test_timestamp_requires_timezone(self):
        with self.assertRaises(ValueError):timestamp('2026-10-06T00:00:00')
        self.assertEqual(timestamp('2026-10-06T07:00:00+07:00').hour,0)

class ReplayTests(unittest.TestCase):
    def test_next_bar_entry_stop_wins_ambiguous_bar(self):
        from datetime import datetime,timedelta,timezone
        from unittest.mock import patch
        from gptsalov.xau_research import replay
        c={k:D(v) for k,v in dict(tick_size='.01',tick_value_loss_per_lot='1',commission_roundtrip_usd_per_lot='0',slippage_price_per_side='0',volume_min='.01',volume_max='100',volume_step='.01',risk_usd='2',initial_equity_usd='50',max_spread_price='.5').items()}
        c.update(session_timezone='UTC',session_start_hour=10)
        bars=[]
        for i in range(72):
            b=dict(time=datetime(2026,10,6,5,tzinfo=timezone.utc)+timedelta(minutes=5*i))
            for side,delta in [('bid',D(0)),('ask',D('.1'))]:
                for key,v in [('open','100'),('high','104'),('low','98'),('close','100')]:b[side+'_'+key]=D(v)+delta
            bars.append(b)
        with patch('gptsalov.xau_research.signal',return_value=(1,D(1))):
            r=replay(bars,c,'trend_pullback')
        self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['trades'][0]['time'],'2026-10-06T10:00:00+00:00')
        self.assertEqual(r['trades'][0]['reason'],'STOP')
        self.assertEqual(D(r['net_usd']),D('-2'))
