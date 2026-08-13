"""
回测优化点验证：
1. 严格1h共振：30min底分型 + 1h最近分型也必须是底分型（做多），顶分型同理（做空）
2. 分型去重：用过的分型(timestamp, direction)排除
3. 平仓后冷却90min（3根30min K线）

对比 baseline（宽松1h共振 .any()）vs 优化版
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
LEVERAGE = 100
MARGIN_RATE = 0.05
NOTIONAL_MULT = LEVERAGE * MARGIN_RATE  # 5x

ASSETS = {
    "BTC": {
        "file_30m": "data/bt_BTC_USDT_USDT_30m_2025-08-12.csv",
        "file_1h": "data/bt_BTC_USDT_USDT_1h_2025-08-12.csv",
        "max_stop_pct": 0.017,
        "max_stop_pts": None,
    },
    "ETH": {
        "file_30m": "data/bt_ETH_USDT_USDT_30m_2025-08-12.csv",
        "file_1h": "data/bt_ETH_USDT_USDT_1h_2025-08-12.csv",
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


def run_backtest(m30, h1, asset_cfg, resonance_mode="loose", cooldown_bars=3):
    """
    resonance_mode: "loose" 宽松(1h历史上any出现过分型) | "strict" 严格(1h最近分型方向匹配)
    cooldown_bars: 平仓后冷却K线数（3根=90分钟）
    """
    m30f = add_fractals(m30, LEFT, RIGHT)
    h1f = add_fractals(h1, 2, 2)

    # 预计算1h分型事件（用于严格共振）
    h1_mask = h1f['fractal_low'].values | h1f['fractal_high'].values
    h1_event_ts = h1f['timestamp'].values[h1_mask]
    h1_event_type = np.where(h1f['fractal_low'].values[h1_mask], 'low', 'high')
    # 按时间排序（通常已排序）
    order = np.argsort(h1_event_ts)
    h1_event_ts = h1_event_ts[order]
    h1_event_type = h1_event_type[order]

    max_stop_pct = asset_cfg.get("max_stop_pct")
    max_stop_pts = asset_cfg.get("max_stop_pts")

    trades = []
    in_pos = False
    pos_side = None
    entry_price = sl = tp = 0.0
    entry_idx = 0
    cooldown_until = 0
    traded_pivots = set()  # {(timestamp_ms, 'long'/'short')}

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
                elif bar["high"] >= tp:
                    exit_flag, exit_price = "win", tp
            else:
                if bar["high"] >= sl:
                    exit_flag, exit_price = "loss", sl
                elif bar["low"] <= tp:
                    exit_flag, exit_price = "win", tp

            if exit_flag:
                gross = (exit_price - entry_price) / entry_price if pos_side == "long" else (entry_price - exit_price) / entry_price
                net = gross - FEE * 2
                account_ret = net * NOTIONAL_MULT
                trades.append({
                    "side": pos_side,
                    "entry_idx": int(entry_idx),
                    "exit_idx": int(i),
                    "entry_time": str(m30["datetime"].iloc[entry_idx]),
                    "entry_price": float(entry_price),
                    "exit_time": str(m30["datetime"].iloc[i]),
                    "exit_price": float(exit_price),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                })
                in_pos = False
                cooldown_until = i + cooldown_bars
                i += 1
                continue
            else:
                # 持仓中未触发平仓：继续扫描下一根
                i += 1
                continue

        # 冷却期跳过
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

        # 分型去重：用过的分型排除
        if (pivot_ts, direction) in traded_pivots:
            i += 1
            continue

        # 1h 共振
        if resonance_mode == "loose":
            # 宽松版：1h 历史上任意时间出现过对应分型
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
        else:
            # 严格版：1h 最近一个分型方向必须匹配
            idx = np.searchsorted(h1_event_ts, pivot_ts, side='right') - 1
            if idx < 0:
                i += 1
                continue
            nearest_type = h1_event_type[idx]
            if direction == "long" and nearest_type != "low":
                i += 1
                continue
            if direction == "short" and nearest_type != "high":
                i += 1
                continue

        # 开仓
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

        in_pos = True
        pos_side = direction
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
                "max_consecutive_losses": 0}

    wins = len(td[td["result"] == "win"])
    losses = total - wins
    win_rate = wins / total

    cum_ret = (1 + td["account_return"]).prod() - 1

    gp = td[td["net_return"] > 0]["net_return"].sum()
    gl = abs(td[td["net_return"] < 0]["net_return"].sum())
    pf = gp / gl if gl > 0 else float('inf') if gp > 0 else 0

    equity = (1 + td["account_return"]).cumprod()
    rolling_max = equity.cummax()
    drawdown = (equity / rolling_max) - 1
    max_dd = abs(drawdown.min())

    # 最大连续亏损
    cons = 0
    max_cons = 0
    for _, r in td.iterrows():
        cons = cons + 1 if r["result"] == "loss" else 0
        max_cons = max(max_cons, cons)

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": float(win_rate),
        "total_return": float(cum_ret),
        "profit_factor": float(pf) if pf != float('inf') else 999.0,
        "max_drawdown": float(max_dd),
        "max_consecutive_losses": int(max_cons),
    }


def fmt(n):
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.2f}万"
    return f"{n:.2f}"


def main():
    print("=" * 84)
    print("回测优化点验证：严格1h共振 + 分型去重 + 冷却90min")
    print("策略：分型(5,2) + RR=1:1 + 100x杠杆(5x敞口)")
    print("=" * 84)

    configs = [
        ("当前策略(宽松共振+冷却3根)", "loose", 3),
        ("优化:严格共振+冷却3根", "strict", 3),
        ("优化:严格共振+冷却0根", "strict", 0),
        ("优化:严格共振+冷却6根", "strict", 6),
    ]

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*84}")
        print(f"  【{name}】")
        print(f"{'─'*84}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])

        print(f"\n  {'方案':<26}{'交易':>6}{'胜率':>9}{'累计收益':>18}{'盈亏因子':>10}{'最大回撤':>11}{'连亏':>7}")
        print("  " + "-" * 82)

        for label, mode, cd in configs:
            trades = run_backtest(m30, h1, cfg, resonance_mode=mode, cooldown_bars=cd)
            s = compute_stats(trades)
            print(f"  {label:<26}{s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{fmt(s['total_return']*100):>17}{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%{s['max_consecutive_losses']:>7}")

    print("\n" + "=" * 84)
    print("回测完成")
    print("=" * 84)


if __name__ == "__main__":
    main()
