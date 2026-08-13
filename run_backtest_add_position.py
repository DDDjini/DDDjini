"""
对比回测：原始 RR=1 策略 vs 加仓策略
=====================================
加仓规则：
- 每次开仓 = 余额5%保证金 (100x杠杆 = 5x敞口)
- 当价格临近止损点 0.1%~0.15% 时，加仓一次5%保证金
- 总仓位止损不变（仍为原 sl）
- 加仓后平均成本摊低，止盈按新平均成本 × RR 重算
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
MARGIN_RATE = 0.05      # 5%保证金
NOTIONAL_MULT = LEVERAGE * MARGIN_RATE  # 5x

# 加仓触发距离（价格距止损点的比例）
ADD_PCT_LIST = [0.0010, 0.00125, 0.0015]  # 0.10% / 0.125% / 0.15%

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


def run_backtest(m30, h1, asset_cfg, add_pct=None):
    """
    回测核心。add_pct=None 表示原始策略（无加仓）。
    add_pct 为数值时启用加仓。
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

    # 加仓状态
    added = False
    add_price = 0.0
    entry1 = 0.0  # 首次入场价
    avg_entry = 0.0  # 加仓后平均成本

    max_i = len(m30f) - 1
    i = LEFT + RIGHT + 3

    while i < max_i:
        if in_pos:
            bar = m30.iloc[i]
            exit_flag = None
            exit_price = 0.0

            if pos_side == "long":
                # 先判止损（最保守：触及sl就平仓）
                if bar["low"] <= sl:
                    exit_flag, exit_price = "loss", sl
                # 加仓检查（仅当未加仓且价格触及加仓价但未触及止损）
                elif add_pct is not None and not added and bar["low"] <= add_price:
                    added = True
                    # 加仓成交价 = add_price（保守用 add_price 而非更低）
                    entry2 = add_price
                    avg_entry = (entry1 + entry2) / 2
                    # 新止盈 = 平均成本 + RR * (平均成本 - 止损)
                    new_risk = avg_entry - sl
                    tp = avg_entry + RR * new_risk
                elif bar["high"] >= tp:
                    exit_flag, exit_price = "win", tp
            else:
                if bar["high"] >= sl:
                    exit_flag, exit_price = "loss", sl
                elif add_pct is not None and not added and bar["high"] >= add_price:
                    added = True
                    entry2 = add_price
                    avg_entry = (entry1 + entry2) / 2
                    new_risk = sl - avg_entry
                    tp = avg_entry - RR * new_risk
                elif bar["low"] <= tp:
                    exit_flag, exit_price = "win", tp

            if exit_flag:
                # 账户收益计算
                if added:
                    # 两笔仓位：entry1 和 add_price 各自 5% 保证金
                    if pos_side == "long":
                        net1 = (exit_price - entry1) / entry1 - FEE * 2
                        net2 = (exit_price - add_price) / add_price - FEE * 2
                    else:
                        net1 = (entry1 - exit_price) / entry1 - FEE * 2
                        net2 = (add_price - exit_price) / add_price - FEE * 2
                    account_ret = (net1 + net2) * NOTIONAL_MULT
                    # 记录整体 net（用于展示）
                    net = (net1 + net2) / 2
                    trade_side_desc = f"{pos_side}+加仓"
                else:
                    gross = (exit_price - entry_price) / entry_price if pos_side == "long" else (entry_price - exit_price) / entry_price
                    net = gross - FEE * 2
                    account_ret = net * NOTIONAL_MULT
                    trade_side_desc = pos_side

                trades.append({
                    "side": trade_side_desc,
                    "entry_idx": int(entry_idx),
                    "exit_idx": int(i),
                    "entry_time": str(m30["datetime"].iloc[entry_idx]),
                    "entry_price": float(entry_price),
                    "exit_time": str(m30["datetime"].iloc[i]),
                    "exit_price": float(exit_price),
                    "stop_loss": float(sl),
                    "take_profit": float(tp),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                    "added": added,
                    "rr": RR,
                })
                in_pos = False
                added = False
                cooldown_until = i + COOLDOWN_BARS
                i += 1
                continue
            else:
                # 持仓中未触发平仓（或刚加仓）：继续扫描下一根K线
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

        # 加仓触发价
        if add_pct is not None:
            if direction == "long":
                add_price = sl * (1 + add_pct)
            else:
                add_price = sl * (1 - add_pct)

        in_pos = True
        pos_side = direction
        entry1 = entry_price
        avg_entry = entry_price
        added = False
        traded_pivots.add(pivot)
        cooldown_until = 0
        i = entry_idx + 1

    return trades


def compute_stats(trades):
    """计算统计指标"""
    td = pd.DataFrame(trades) if trades else pd.DataFrame()
    total = len(td)
    if total == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_return": 0, "profit_factor": 0, "max_drawdown": 0,
            "added_count": 0, "added_win_rate": 0,
        }

    wins = len(td[td["result"] == "win"])
    losses = total - wins
    win_rate = wins / total

    # 累计收益（含杠杆）
    cum_ret = (1 + td["account_return"]).prod() - 1

    # 盈亏因子（用价格层面 net_return 的盈亏比，加仓的用平均net）
    gp = td[td["net_return"] > 0]["net_return"].sum()
    gl = abs(td[td["net_return"] < 0]["net_return"].sum())
    pf = gp / gl if gl > 0 else float('inf') if gp > 0 else 0

    # 最大回撤
    equity = (1 + td["account_return"]).cumprod()
    rolling_max = equity.cummax()
    drawdown = (equity / rolling_max) - 1
    max_dd = abs(drawdown.min())

    # 加仓统计
    added_trades = td[td["added"] == True] if "added" in td.columns else pd.DataFrame()
    added_count = len(added_trades)
    added_wins = len(added_trades[added_trades["result"] == "win"])
    added_wr = added_wins / added_count if added_count > 0 else 0

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": float(win_rate),
        "total_return": float(cum_ret),
        "profit_factor": float(pf) if pf != float('inf') else 999.0,
        "max_drawdown": float(max_dd),
        "added_count": added_count,
        "added_win_rate": float(added_wr),
    }


def main():
    print("=" * 70)
    print("对比回测：原始 RR=1 策略 vs 加仓策略")
    print("加仓规则：临近止损点 0.1%~0.15% 时加仓 5%，总止损不变")
    print("=" * 70)

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*70}")
        print(f"  [{name}]")
        print(f"{'─'*70}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])
        print(f"  数据: 30m={len(m30)}根, 1h={len(h1)}根, "
              f"{m30['datetime'].iloc[0].strftime('%Y-%m-%d')} ~ {m30['datetime'].iloc[-1].strftime('%Y-%m-%d')}")

        # 原始策略
        base_trades = run_backtest(m30, h1, cfg, add_pct=None)
        base_stats = compute_stats(base_trades)

        print(f"\n  【原始策略】(无加仓):")
        print(f"    交易 {base_stats['trades']}笔 | 胜率 {base_stats['win_rate']*100:.1f}% | "
              f"累计收益 {base_stats['total_return']*100:+.2f}% | "
              f"PF {base_stats['profit_factor']:.3f} | 回撤 {base_stats['max_drawdown']*100:.2f}%")

        # 加仓策略（多个加仓距离）
        print(f"\n  【加仓策略】(各加仓距离对比):")
        print(f"  {'加仓距离':<10}{'交易':>6}{'胜率':>9}{'累计收益':>18}{'盈亏因子':>10}{'最大回撤':>11}{'加仓次数':>9}{'加仓胜率':>9}")

        results = {}
        for add_pct in ADD_PCT_LIST:
            add_trades = run_backtest(m30, h1, cfg, add_pct=add_pct)
            s = compute_stats(add_trades)
            results[add_pct] = s
            print(f"  {add_pct*100:.3f}%  {s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{s['total_return']*100:>17.2f}%{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%{s['added_count']:>9}{s['added_win_rate']*100:>8.1f}%")

        # 保存对比结果
        print()

    # 汇总表
    print("\n" + "=" * 70)
    print("总结：加仓 vs 不加仓（收益率对比）")
    print("=" * 70)


if __name__ == "__main__":
    main()
