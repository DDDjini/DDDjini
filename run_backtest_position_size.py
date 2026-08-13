"""
回测：头仓/加仓仓位比例对比
=================================================
基于最新策略：分型(5,2) + 1h宽松共振 + 4h严格共振 + RR=1 + 100x杠杆 + 冷却3根 + 加仓点浮亏40%

对比不同仓位配置：
  旧方案：头仓5% + 加仓5%（杠杆敞口 5x+5x=10x）
  新方案：头仓3% + 加仓4%（杠杆敞口 3x+4x=7x）
  以及若干中间组合作参考

注意：胜率与仓位无关（只取决于开平仓判定），仓位只影响收益率和回撤。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from pathlib import Path


LEFT, RIGHT = 5, 2
RR = 1.0
SL_BUFFER = 0.0005
FEE = 0.0005
COOLDOWN_BARS = 3
LEVERAGE = 100
ADD_FRAC = 0.40  # 加仓点：浮亏40%

ASSETS = {
    "BTC": {
        "file_30m": "data/bt_BTC_USDT_USDT_30m_2025-08-12.csv",
        "file_1h": "data/bt_BTC_USDT_USDT_1h_2025-08-12.csv",
        "file_4h": "data/bt_BTC_USDT_USDT_4h_2025-08-12.csv",
        "max_stop_pct": 0.017,
        "max_stop_pts": None,
    },
    "ETH": {
        "file_30m": "data/bt_ETH_USDT_USDT_30m_2025-08-12.csv",
        "file_1h": "data/bt_ETH_USDT_USDT_1h_2025-08-12.csv",
        "file_4h": "data/bt_ETH_USDT_USDT_4h_2025-08-12.csv",
        "max_stop_pct": None,
        "max_stop_pts": 50.0,
    },
}


def add_fractals(df, left, right):
    df = df.copy()
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]
    lm = pd.concat(low_shifts, axis=1)
    hm = pd.concat(high_shifts, axis=1)
    min_low = lm.min(axis=1)
    max_high = hm.max(axis=1)
    count_low = (lm.values == df["low"].values[:, None]).sum(axis=1)
    count_high = (hm.values == df["high"].values[:, None]).sum(axis=1)
    df["fractal_low"] = (df["low"] == min_low) & (count_low == 1)
    df["fractal_high"] = (df["high"] == max_high) & (count_high == 1)
    return df


def run_backtest(m30, h1, h4, asset_cfg, head_margin=0.05, add_margin=0.05):
    """
    head_margin: 头仓保证金比例
    add_margin: 加仓保证金比例
    """
    head_notional = LEVERAGE * head_margin
    add_notional = LEVERAGE * add_margin

    m30f = add_fractals(m30, LEFT, RIGHT)
    h1f = add_fractals(h1, 2, 2)
    h4f = add_fractals(h4, 2, 2)

    h4_mask = h4f['fractal_low'].values | h4f['fractal_high'].values
    h4_ts = h4f['timestamp'].values[h4_mask]
    h4_typ = np.where(h4f['fractal_low'].values[h4_mask], 'low', 'high')
    order = np.argsort(h4_ts)
    h4_ts = h4_ts[order]
    h4_typ = h4_typ[order]

    max_stop_pct = asset_cfg.get("max_stop_pct")
    max_stop_pts = asset_cfg.get("max_stop_pts")

    trades = []
    in_pos = False
    pos_side = None
    entry_price = sl = tp = 0.0
    entry1 = 0.0
    entry_idx = 0
    cooldown_until = 0
    traded_pivots = set()
    added = False
    add_price = 0.0

    max_i = len(m30f) - 1
    i = LEFT + RIGHT + 3

    while i < max_i:
        if in_pos:
            bar = m30.iloc[i]
            exit_flag = None
            exit_price = 0.0

            if pos_side == "long":
                if bar["low"] <= sl:
                    exit_flag, exit_price = "loss", sl
                elif not added and bar["low"] <= add_price:
                    added = True
                    avg_entry = (entry1 + add_price) / 2
                    new_risk = avg_entry - sl
                    tp = avg_entry + RR * new_risk
                elif bar["high"] >= tp:
                    exit_flag, exit_price = "win", tp
            else:
                if bar["high"] >= sl:
                    exit_flag, exit_price = "loss", sl
                elif not added and bar["high"] >= add_price:
                    added = True
                    avg_entry = (entry1 + add_price) / 2
                    new_risk = sl - avg_entry
                    tp = avg_entry - RR * new_risk
                elif bar["low"] <= tp:
                    exit_flag, exit_price = "win", tp

            if exit_flag:
                if added:
                    if pos_side == "long":
                        net1 = (exit_price - entry1) / entry1 - FEE * 2
                        net2 = (exit_price - add_price) / add_price - FEE * 2
                    else:
                        net1 = (entry1 - exit_price) / entry1 - FEE * 2
                        net2 = (add_price - exit_price) / add_price - FEE * 2
                    # 加权账户收益：头仓 net1 × 头仓杠杆 + 加仓 net2 × 加仓杠杆
                    account_ret = net1 * head_notional + net2 * add_notional
                    net = (net1 * head_margin + net2 * add_margin) / (head_margin + add_margin)
                else:
                    gross = (exit_price - entry_price) / entry_price if pos_side == "long" else (entry_price - exit_price) / entry_price
                    net = gross - FEE * 2
                    account_ret = net * head_notional

                trades.append({
                    "side": pos_side,
                    "entry_idx": int(entry_idx),
                    "exit_idx": int(i),
                    "entry_price": float(entry_price),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                    "added": added,
                })
                in_pos = False
                added = False
                cooldown_until = i + COOLDOWN_BARS
                i += 1
                continue
            else:
                i += 1
                continue

        if i < cooldown_until:
            i += 1
            continue

        pivot = i - RIGHT
        if pivot < 0:
            i += 1
            continue

        direction = None
        if m30f.loc[pivot, "fractal_low"]:
            direction = "long"
        elif m30f.loc[pivot, "fractal_high"]:
            direction = "short"
        if direction is None:
            i += 1
            continue

        pivot_ts = int(m30f.loc[pivot, "timestamp"])
        if (pivot_ts, direction) in traded_pivots:
            i += 1
            continue

        sub = h1f[h1f["timestamp"] <= pivot_ts]
        if len(sub) < 5:
            i += 1
            continue
        if direction == "long" and not sub["fractal_low"].any():
            i += 1
            continue
        if direction == "short" and not sub["fractal_high"].any():
            i += 1
            continue

        idx4 = np.searchsorted(h4_ts, pivot_ts, side='right') - 1
        if idx4 < 0:
            i += 1
            continue
        if direction == "long" and h4_typ[idx4] != "low":
            i += 1
            continue
        if direction == "short" and h4_typ[idx4] != "high":
            i += 1
            continue

        if i + 1 >= len(m30):
            break
        entry_idx = i + 1
        entry_price = float(m30.iloc[entry_idx]["open"])

        if direction == "long":
            sl = float(m30f.loc[pivot, "low"]) * (1 - SL_BUFFER)
            risk = entry_price - sl
            if risk <= 0:
                i += 1
                continue
            if max_stop_pct and risk > entry_price * max_stop_pct:
                risk = entry_price * max_stop_pct
                sl = entry_price - risk
            tp = entry_price + RR * risk
            add_price = entry_price - ADD_FRAC * (entry_price - sl)
        else:
            sl = float(m30f.loc[pivot, "high"]) * (1 + SL_BUFFER)
            risk = sl - entry_price
            if risk <= 0:
                i += 1
                continue
            if max_stop_pts and risk > max_stop_pts:
                risk = max_stop_pts
                sl = entry_price + risk
            tp = entry_price - RR * risk
            add_price = entry_price + ADD_FRAC * (sl - entry_price)

        in_pos = True
        pos_side = direction
        entry1 = entry_price
        added = False
        traded_pivots.add((pivot_ts, direction))
        cooldown_until = 0
        i = entry_idx + 1

    return trades


def compute_stats(trades):
    td = pd.DataFrame(trades) if trades else pd.DataFrame()
    total = len(td)
    if total == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_return": 0, "profit_factor": 0, "max_drawdown": 0,
                "added_count": 0, "added_win_rate": 0}

    wins = len(td[td["result"] == "win"])
    losses = total - wins
    win_rate = wins / total
    cum_ret = (1 + td["account_return"]).prod() - 1

    # PF 用账户收益（含杠杆仓位）更准确
    gp = td[td["account_return"] > 0]["account_return"].sum()
    gl = abs(td[td["account_return"] < 0]["account_return"].sum())
    pf = gp / gl if gl > 0 else float('inf') if gp > 0 else 0

    equity = (1 + td["account_return"]).cumprod()
    rolling_max = equity.cummax()
    drawdown = (equity / rolling_max) - 1
    max_dd = abs(drawdown.min())

    added_trades = td[td["added"] == True] if "added" in td.columns else pd.DataFrame()
    added_count = len(added_trades)
    added_wins = len(added_trades[added_trades["result"] == "win"])
    added_wr = added_wins / added_count if added_count > 0 else 0

    return {
        "trades": total, "wins": wins, "losses": losses,
        "win_rate": float(win_rate), "total_return": float(cum_ret),
        "profit_factor": float(pf) if pf != float('inf') else 999.0,
        "max_drawdown": float(max_dd),
        "added_count": added_count, "added_win_rate": float(added_wr),
    }


def fmt(n):
    if abs(n) >= 1e8:
        return f"{n/1e8:.2f}亿"
    if abs(n) >= 1e4:
        return f"{n/1e4:.2f}万"
    return f"{n:.2f}"


def main():
    print("=" * 100)
    print("回测：头仓/加仓仓位比例对比（基于4h严格共振+浮亏40%加仓）")
    print("=" * 100)

    # 仓位配置：(头仓%, 加仓%, 标签)
    configs = [
        (0.05, 0.05, "头5%+加5%(旧)"),
        (0.03, 0.04, "头3%+加4%(新)"),
        (0.03, 0.03, "头3%+加3%"),
        (0.04, 0.04, "头4%+加4%"),
        (0.04, 0.03, "头4%+加3%"),
    ]

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*100}")
        print(f"  【{name}】")
        print(f"{'─'*100}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])
        h4 = pd.read_csv(cfg["file_4h"])
        h4["datetime"] = pd.to_datetime(h4["datetime"])

        print(f"\n  {'仓位配置':<18}{'交易':>6}{'胜率':>9}{'累计收益':>18}{'盈亏因子':>10}{'最大回撤':>11}{'加仓次数':>9}")
        print("  " + "-" * 92)

        for head_m, add_m, label in configs:
            trades = run_backtest(m30, h1, h4, cfg, head_margin=head_m, add_margin=add_m)
            s = compute_stats(trades)
            print(f"  {label:<18}{s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{fmt(s['total_return']*100):>17}{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%{s['added_count']:>9}")

    print("\n" + "=" * 100)
    print("回测完成")
    print("=" * 100)


if __name__ == "__main__":
    main()
