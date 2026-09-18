"""Deterministic USDT-linear Testnet sizing; no network writes or LLM scores.

Risk is a modeled stop loss, not a guaranteed maximum loss. Margin stress is a
conservative screen, not an exchange liquidation-price calculator.
"""
from .core import dec, floor_step

VERSION = 'risk-aware-v1'
MAX_LEVERAGE = 2
FEE = dec('.0008')  # conservative taker allowance on EACH leg
SLIP = dec('.001')  # adverse execution allowance on EACH leg
FUNDING = dec('.001')
MIN_RR = dec('1.2')


def strategy_review(bars, signal):
    """Candidate hypotheses: avoid choppy paths, chasing and shock candles."""
    if signal is None or len(bars) < 22 or signal.side not in (-1, 1):
        raise ValueError('QUALITY_INPUT_MISSING')
    prior = bars[-21:-1]
    path = sum((abs(b.close-a.close) for a, b in zip(prior, prior[1:])), dec(0))
    efficiency = abs(prior[-1].close-prior[0].close)/path if path else dec(0)
    atr = sum((max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
               for a, b in zip(bars[-16:-2], bars[-15:-1])), dec(0))/14
    if atr <= 0:
        raise ValueError('QUALITY_ATR_INVALID')
    boundary = max(b.high for b in prior) if signal.side == 1 else min(b.low for b in prior)
    chase = signal.side*(bars[-1].close-boundary)/atr
    shock = max(bars[-1].high-bars[-1].low,
                abs(bars[-1].high-bars[-2].close), abs(bars[-1].low-bars[-2].close))/atr
    reasons = []
    if efficiency < dec('.25'): reasons.append('CHOPPY_TREND')
    if not dec(0) < chase <= dec('.75'): reasons.append('BREAKOUT_CHASE')
    if shock > 3: reasons.append('SHOCK_CANDLE')
    return dict(allow=not reasons, reasons=reasons, efficiency=efficiency,
                chase_atr=chase, shock_atr=shock, atr=atr,
                calibrated_probability=False, version=VERSION)


def risk_budget(state):
    equity, high, start = (dec(state[k]) for k in ('equity', 'high_water', 'day_start'))
    if min(equity, high, start) <= 0 or equity > high:
        raise ValueError('RISK_STATE_INVALID')
    streak = state.get('loss_streak', 0)
    if type(streak) is not int or streak < 0:
        raise ValueError('LOSS_STREAK_INVALID')
    if any(state.get(k) for k in ('lock', 'active', 'error')) or streak >= 3:
        raise ValueError('PORTFOLIO_BLOCKED')
    dd = max(dec(0), 1-equity/high)
    daily_room = equity-start*dec('.98')
    drawdown_room = equity-high*dec('.92')
    # Never increase risk after losses. Respect remaining daily/DD loss room.
    scale = dec('.5') if dd >= dec('.04') or streak >= 2 else dec(1)
    budget = min(dec('.25'), min(equity, dec(50))*dec('.005')*scale,
                 daily_room, drawdown_room)
    if budget <= 0:
        raise ValueError('LOSS_BUDGET_EXHAUSTED')
    return budget


def bracket_rows(payload, symbol):
    rows = payload if isinstance(payload, list) else [payload]
    matches = [r for r in rows if r.get('symbol') == symbol]
    if len(matches) != 1 or dec(matches[0].get('notionalCoef', 1)) != 1:
        raise ValueError('BRACKET_MISSING_OR_CUSTOM_COEFFICIENT')
    parsed = []
    for r in matches[0]['brackets']:
        low, high, mmr = (dec(r[k]) for k in ('notionalFloor', 'notionalCap', 'maintMarginRatio'))
        lev = r['initialLeverage']
        if type(lev) is not int or not 1 <= lev <= 125 or not 0 <= low < high or not 0 < mmr < 1:
            raise ValueError('BRACKET_INVALID')
        parsed.append((low, high, mmr, lev))
    parsed.sort()
    if not parsed or parsed[0][0] != 0 or any(a[1] != b[0] for a,b in zip(parsed, parsed[1:])):
        raise ValueError('BRACKET_GAP_OR_OVERLAP')
    return parsed


def _bracket(rows, notional):
    matches = [r for r in rows if r[0] <= notional < r[1]]
    if len(matches) != 1:
        raise ValueError('NOTIONAL_OUTSIDE_BRACKETS')
    return matches[0]


def costs(reference, stop, target, side):
    reference, stop, target = map(dec, (reference, stop, target))
    if side not in (-1, 1) or min(reference, stop, target) <= 0:
        raise ValueError('INVALID_PRICES_OR_SIDE')
    # Size for the WORST accepted entry drift, not just expected slippage.
    entry = reference*(1+side*dec('.005'))
    if side*(entry-stop) <= 0 or side*(target-entry) <= 0:
        raise ValueError('INVALID_STOP_TARGET_AFTER_DRIFT')
    stop_fill = stop*(1-side*SLIP)
    target_fill = target*(1-side*SLIP)
    loss = abs(entry-stop_fill)+(entry+stop_fill)*FEE+entry*FUNDING
    reward = side*(target_fill-entry)-(entry+target_fill)*FEE-entry*FUNDING
    # Upper notional bound regardless of direction.
    ceiling = reference*dec('1.005')
    reserve = ceiling*(2*FEE+FUNDING+2*SLIP)
    return entry, ceiling, loss, reward, reserve


def plan_order(*, symbol, side, reference, stop, target, atr, quantity_cap,
               rules, state, available_balance, brackets):
    """Size before leverage; choose the lowest fitting leverage without upsizing.

    One position at a time makes gross correlated exposure equal to this order.
    Missing balances/brackets fail closed. All quantity rounding is downward.
    """
    if rules.symbol != symbol:
        raise ValueError('SYMBOL_MISMATCH')
    reference, stop, target, atr, quantity_cap, available_balance = map(
        dec, (reference, stop, target, atr, quantity_cap, available_balance))
    if min(atr, quantity_cap, available_balance) <= 0:
        raise ValueError('SIZING_INPUT_INVALID')
    if any(p % rules.tick or not rules.min_price <= p <= rules.max_price for p in (stop, target)):
        raise ValueError('PRICE_FILTER')
    budget = risk_budget(state)
    equity = min(dec(state['equity']), dec(50))
    entry, ceiling, unit_loss, unit_reward, unit_reserve = costs(reference, stop, target, side)
    rr = unit_reward/unit_loss
    if rr < MIN_RR:
        raise ValueError('NET_RR_BELOW_1_2')
    rows = bracket_rows(brackets, symbol)
    cash = min(available_balance, equity*dec('.5'))
    quantity = floor_step(min(quantity_cap, budget/unit_loss, dec(25)/ceiling,
                              equity*dec('.5')/ceiling, rules.max_qty,
                              rules.max_notional/ceiling), rules.step)
    # Stop + adverse mark-price stress; no claim of exact liquidation price.
    stress_distance = max(2*abs(entry-stop), 3*atr)
    stressed_mark = entry-side*stress_distance
    if stressed_mark <= 0:
        raise ValueError('STRESS_PRICE_INVALID')
    choices = []
    for leverage in range(1, MAX_LEVERAGE+1):
        q = floor_step(min(quantity, cash/(ceiling/leverage+unit_reserve)), rules.step)
        if q <= 0 or q < rules.min_qty or q*reference*dec('.995') < rules.min_notional:
            continue
        initial = _bracket(rows, q*ceiling)
        stress = _bracket(rows, q*stressed_mark)
        if leverage > min(initial[3], stress[3]):
            continue
        # Ignore cum deduction, giving a conservative maintenance upper bound.
        maintenance = q*stressed_mark*max(initial[2], stress[2])
        initial_margin = q*entry/leverage
        stress_surplus = initial_margin-q*stress_distance-maintenance-q*unit_reserve
        if stress_surplus <= 0:
            continue
        choices.append(dict(quantity=q, leverage=leverage, notional=q*ceiling,
                            initial_margin=q*ceiling/leverage, cost_reserve=q*unit_reserve,
                            modeled_risk=q*unit_loss, risk_budget=budget, net_rr=rr,
                            effective_leverage=q*ceiling/equity,
                            stress_surplus=stress_surplus, atr=atr, version=VERSION))
    if not choices:
        raise ValueError('NO_SAFE_LEVERAGE_OR_EXCHANGE_MINIMUM')
    # Preserve largest risk-sized quantity, then minimize margin leverage.
    return max(choices, key=lambda c: (c['quantity'], -c['leverage']))


def available_usdt(api):
    rows = api.call('GET', '/fapi/v3/balance')
    matches = [r for r in rows if r['asset'] == 'USDT']
    if len(matches) != 1:
        raise ValueError('USDT_BALANCE_MISSING')
    return dec(matches[0]['availableBalance'])


def validate_execution(api, plan, current, rules):
    """Recheck the immutable quantity at current prices before any entry POST."""
    if plan.get('risk_model') != VERSION or type(plan.get('leverage')) is not int:
        raise ValueError('RISK_MODEL_INVALID')
    quantity = dec(plan['quantity'])
    reference = dec(plan['reference'])
    if abs(dec(current)/reference-1) > dec('.005'):
        raise ValueError('ENTRY_DRIFT')
    # Original worst-drift envelope already covers the accepted current price.
    result = plan_order(symbol=plan['symbol'], side=1 if plan['side']=='BUY' else -1,
        reference=reference, stop=plan['stop'], target=plan['target'],
        atr=plan['risk_atr'], quantity_cap=quantity, rules=rules,
        state=plan['risk_state'], available_balance=available_usdt(api),
        brackets=api.call('GET', '/fapi/v1/leverageBracket', symbol=plan['symbol']))
    if result['quantity'] != quantity or result['leverage'] != plan['leverage']:
        raise ValueError('RISK_PLAN_CHANGED_REVIEW_REQUIRED')
    return result
