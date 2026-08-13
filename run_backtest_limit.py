"""
回测对比：市价开仓 vs 限价挂单开仓（挂单最优价格法）
=====================================================
策略基础：分型(5,2) + 1h共振 + RR=1:1 + 100x杠杆 + 冷却3根 + 分型去重

市价开仓（当前）：
  分型确认后，下一根K线开盘价直接市价成交

限价挂单（优化）：
  分型确认后，挂限价单在更优价格（接近分型点），等价格回落到挂单价才成交
  做多挂单价 = 分型低点 * (1 + entry_buffer)
  做空挂单价 = 分型高点 * (1 - entry_buffer)
  挂单有效期 valid_bars 根K线，未成交则放弃该信号
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


def run_backtest(m30, h1, asset_cfg, mode="market", entry_buffer=0.001, valid_bars=6):
    """
    mode: "market" 市价开仓 | "limit" 限价挂单开仓
    """
    m30f = add_fractals(m30, LEFT, RIGHT)
    h1f = add_fractals(h1, 2, 2)

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
                    "entry_time": str(m30["datetime"].iloc[entry_idx]),
                    "entry_price": float(entry_price),
                    "exit_time": str(m30["datetime"].iloc[i]),
                    "exit_price": float(exit_price),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                })
                in_pos = False
                cooldown_until = i + COOLDOWN_BARS
                i += 1
                continue
            else:
                # 持仓中未触发平仓：继续扫描下一根K线
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
        if pivot in traded_pivots:
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

        # 1h 共振
        ts_ = m30f.loc[pivot, "timestamp"]
        sub = h1f[h1f["timestamp"] <= ts_]
        if len(sub) < 5:
            i += 1
            continue
        if direction == "long" and not sub["fractal_low"].any():
            i += 1
            continue
        if direction == "short" and not sub["fractal_high"].any():
            i += 1
            continue

        # 计算止损
        if direction == "long":
            pivot_low = float(m30f.loc[pivot, "low"])
            sl = pivot_low * (1 - SL_BUFFER)
        else:
            pivot_high = float(m30f.loc[pivot, "high"])
            sl = pivot_high * (1 + SL_BUFFER)

        # 开仓方式
        if mode == "market":
            # 市价开仓：下一根开盘价
            if i + 1 >= len(m30):
                break
            entry_idx = i + 1
            entry_price = float(m30.iloc[entry_idx]["open"])
        else:
            # 限价挂单：挂单价更优（接近分型点）
            if direction == "long":
                limit_price = pivot_low * (1 + entry_buffer)
            else:
                limit_price = pivot_high * (1 - entry_buffer)
            # 等价格回落到挂单价，valid_bars 内有效
            entry_idx = None
            entry_price = None
            for j in range(i + 1, min(i + 1 + valid_bars, len(m30))):
                bar = m30.iloc[j]
                if direction == "long" and bar["low"] <= limit_price:
                    entry_idx = j
                    entry_price = limit_price
                    break
                elif direction == "short" and bar["high"] >= limit_price:
                    entry_idx = j
                    entry_price = limit_price
                    break

            if entry_idx is None:
                # 挂单未成交，放弃该信号
                traded_pivots.add(pivot)
                i += 1
                continue

        # 校验止损和风险
        if direction == "long":
            risk = entry_price - sl
            if risk <= 0:
                i += 1
                continue
            if max_stop_pct and risk > entry_price * max_stop_pct:
                risk = entry_price * max_stop_pct
                sl = entry_price - risk
            tp = entry_price + RR * risk
        else:
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
        traded_pivots.add(pivot)
        cooldown_until = 0
        i = entry_idx + 1

    return trades


def compute_stats(trades):
    td = pd.DataFrame(trades) if trades else pd.DataFrame()
    total = len(td)
    if total == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_return": 0, "profit_factor": 0, "max_drawdown": 0,
                "avg_hold_bars": 0}

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

    avg_hold = float((td["exit_idx"] - td["entry_idx"]).mean())

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": float(win_rate),
        "total_return": float(cum_ret),
        "profit_factor": float(pf) if pf != float('inf') else 999.0,
        "max_drawdown": float(max_dd),
        "avg_hold_bars": avg_hold,
    }


def fmt_num(n):
    """格式化大数字"""
    if n >= 1e12:
        return f"{n/1e12:.2f}万亿"
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.2f}万"
    return f"{n:.2f}"


def main():
    print("=" * 80)
    print("回测对比：市价开仓 vs 限价挂单开仓（挂单最优价格法）")
    print("策略：分型(5,2) + 1h共振 + RR=1:1 + 100x杠杆 + 冷却3根")
    print("=" * 80)

    # 测试的挂单参数
    limit_configs = [
        ("市价开仓(基线)", "market", None, None),
        ("限价 buffer=0.05%", "limit", 0.0005, 6),
        ("限价 buffer=0.10%", "limit", 0.0010, 6),
        ("限价 buffer=0.20%", "limit", 0.0020, 6),
        ("限价 buffer=0.30%", "limit", 0.0030, 6),
        ("限价 buffer=0.50%", "limit", 0.0050, 6),
        ("限价 buffer=0.10% 有效期12", "limit", 0.0010, 12),
    ]

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*80}")
        print(f"  【{name}】")
        print(f"{'─'*80}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])
        print(f"  数据: 30m={len(m30)}根, 1h={len(h1)}根, "
              f"{m30['datetime'].iloc[0].strftime('%Y-%m-%d')} ~ {m30['datetime'].iloc[-1].strftime('%Y-%m-%d')}")

        print(f"\n  {'方案':<24}{'交易':>6}{'胜率':>9}{'累计收益':>20}{'盈亏因子':>10}{'最大回撤':>11}{'持仓K线':>9}")
        print("  " + "-" * 90)

        for label, mode, buf, vb in limit_configs:
            trades = run_backtest(m30, h1, cfg, mode=mode, entry_buffer=buf, valid_bars=vb)
            s = compute_stats(trades)
            print(f"  {label:<24}{s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{fmt_num(s['total_return']*100):>19}{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%{s['avg_hold_bars']:>8.1f}")

    print("\n" + "=" * 80)
    print("回测完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
