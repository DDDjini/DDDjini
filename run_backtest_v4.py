"""
回测：额外周期共振（4h/2h）对胜率和收益的影响
=================================================
在 30m分型 + 1h共振 基础上，额外加入 4h 或 2h 级别的分型共振，
测试能否提升胜率和收益。

4h/2h 数据从 30m 数据聚合生成（4h = 8根30m，2h = 4根30m）。

共振方式：
- loose（宽松）：额外周期历史上任意出现过分型
- strict（严格）：额外周期最近一个分型方向必须匹配
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


def resample_ohlcv(df_30m, rule):
    """从30m数据聚合出更粗周期（4h/2h）"""
    df = df_30m.copy()
    df = df.set_index("datetime")
    agg = df.resample(rule).agg({
        "timestamp": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    agg = agg.reset_index(drop=True)
    return agg


def build_fractal_events(tf_df, left=2, right=2):
    """构建某周期的分型事件（时间戳 + 类型），用于共振判断"""
    f = add_fractals(tf_df, left, right)
    mask = f['fractal_low'].values | f['fractal_high'].values
    ts = f['timestamp'].values[mask]
    typ = np.where(f['fractal_low'].values[mask], 'low', 'high')
    order = np.argsort(ts)
    return ts[order], typ[order]


def run_backtest(m30, h1, asset_cfg, h1_mode="loose", extra_df=None, extra_mode="strict"):
    """
    h1_mode: "loose" 宽松1h共振 | "strict" 严格1h共振
    extra_df: None 或 额外周期DataFrame(4h/2h)
    extra_mode: "strict" 严格(最近分型方向匹配) | "loose" 宽松(历史any)
    """
    m30f = add_fractals(m30, LEFT, RIGHT)
    h1f = add_fractals(h1, 2, 2)

    # 1h 分型事件（严格共振用）
    h1_mask = h1f['fractal_low'].values | h1f['fractal_high'].values
    h1_ts = h1f['timestamp'].values[h1_mask]
    h1_typ = np.where(h1f['fractal_low'].values[h1_mask], 'low', 'high')
    order = np.argsort(h1_ts)
    h1_ts = h1_ts[order]
    h1_typ = h1_typ[order]

    # 额外周期分型事件
    extra_ts, extra_typ, extra_df_f = None, None, None
    if extra_df is not None:
        extra_df_f = add_fractals(extra_df, 2, 2)
        extra_ts, extra_typ = build_fractal_events(extra_df, 2, 2)

    max_stop_pct = asset_cfg.get("max_stop_pct")
    max_stop_pts = asset_cfg.get("max_stop_pts")

    trades = []
    in_pos = False
    pos_side = None
    entry_price = sl = tp = 0.0
    entry_idx = 0
    cooldown_until = 0
    traded_pivots = set()

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
                    "entry_price": float(entry_price),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                })
                in_pos = False
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

        # ── 1h 共振 ──
        if h1_mode == "loose":
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
            idx = np.searchsorted(h1_ts, pivot_ts, side='right') - 1
            if idx < 0:
                i += 1
                continue
            if direction == "long" and h1_typ[idx] != "low":
                i += 1
                continue
            if direction == "short" and h1_typ[idx] != "high":
                i += 1
                continue

        # ── 额外周期共振（4h/2h）──
        if extra_df is not None:
            if extra_mode == "strict":
                idx = np.searchsorted(extra_ts, pivot_ts, side='right') - 1
                if idx < 0:
                    i += 1
                    continue
                if direction == "long" and extra_typ[idx] != "low":
                    i += 1
                    continue
                if direction == "short" and extra_typ[idx] != "high":
                    i += 1
                    continue
            else:
                sub = extra_df_f[extra_df_f["timestamp"] <= pivot_ts]
                if len(sub) < 3:
                    i += 1
                    continue
                if direction == "long" and not sub["fractal_low"].any():
                    i += 1
                    continue
                if direction == "short" and not sub["fractal_high"].any():
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
                "total_return": 0, "profit_factor": 0, "max_drawdown": 0}

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

    return {
        "trades": total, "wins": wins, "losses": losses,
        "win_rate": float(win_rate), "total_return": float(cum_ret),
        "profit_factor": float(pf) if pf != float('inf') else 999.0,
        "max_drawdown": float(max_dd),
    }


def fmt(n):
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.2f}万"
    return f"{n:.2f}"


def main():
    print("=" * 90)
    print("回测：额外周期共振（4h/2h）对胜率和收益的影响")
    print("基础：30m分型(5,2) + 1h共振 + RR=1 + 冷却3根")
    print("=" * 90)

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*90}")
        print(f"  【{name}】")
        print(f"{'─'*90}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])

        # 聚合 4h 和 2h
        h4 = resample_ohlcv(m30, "4h")
        h2 = resample_ohlcv(m30, "2h")
        print(f"  30m={len(m30)}根, 1h={len(h1)}根, 2h={len(h2)}根, 4h={len(h4)}根")

        configs = [
            ("基线(30m+1h宽松)", None, "loose", None),
            ("+4h严格共振", h4, "loose", "strict"),
            ("+4h宽松共振", h4, "loose", "loose"),
            ("+2h严格共振", h2, "loose", "strict"),
            ("+2h宽松共振", h2, "loose", "loose"),
            ("+4h严格+1h严格", h4, "strict", "strict"),
        ]

        print(f"\n  {'方案':<22}{'交易':>6}{'胜率':>9}{'累计收益':>18}{'盈亏因子':>10}{'最大回撤':>11}")
        print("  " + "-" * 80)

        results = {}
        for label, extra, h1_mode, extra_mode in configs:
            trades = run_backtest(m30, h1, cfg, h1_mode=h1_mode, extra_df=extra, extra_mode=extra_mode)
            s = compute_stats(trades)
            results[label] = s
            print(f"  {label:<22}{s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{fmt(s['total_return']*100):>17}{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%")

    print("\n" + "=" * 90)
    print("回测完成")
    print("=" * 90)


if __name__ == "__main__":
    main()
