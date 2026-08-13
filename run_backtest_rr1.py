"""
严格按 GitHub 版 RR=1 策略回测：
- RR = 1.0 (1:1 盈亏比)
- 分型(5,2) + 1h共振
- 平仓后冷却3根K线
- 已交易分型去重
- BTC: max_stop_pct=1.7%, ETH: max_stop=50点
- sl_buffer=0.0005, 手续费=0.0005
- 100x杠杆, 5%保证金 → 名义敞口5倍
- 无AI过滤, 无风控模块
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import json
from pathlib import Path


LEFT, RIGHT = 5, 2
RR = 1.0
SL_BUFFER = 0.0005
FEE = 0.0005
COOLDOWN_BARS = 3  # 平仓后冷却K线数
LEVERAGE = 100
MARGIN_RATE = 0.05   # 5%保证金
NOTIONAL_MULT = LEVERAGE * MARGIN_RATE  # = 5x

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
    """向量化计算分型（同GitHub版）"""
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


def run_rr1_backtest(m30, h1, asset_cfg):
    """
    严格按GitHub RR=1逻辑回测
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
                    "stop_loss": float(sl),
                    "take_profit": float(tp),
                    "result": exit_flag,
                    "net_return": float(net),
                    "account_return": float(account_ret),
                    "rr": RR,
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

        # 跳过已交易分型
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

        # 1h 共振过滤 (最新分型方向需匹配)
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

        # 从下一根开盘入场
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
            if max_stop_pts and risk > max_stop_pts:
                risk = max_stop_pts
                sl = entry_price - risk
            tp = entry_price + RR * risk
        else:
            sl = float(m30f.loc[pivot, "high"]) * (1 + SL_BUFFER)
            risk = sl - entry_price
            if risk <= 0:
                i += 1
                continue
            if max_stop_pct and risk > entry_price * max_stop_pct:
                risk = entry_price * max_stop_pct
                sl = entry_price + risk
            if max_stop_pts and risk > max_stop_pts:
                risk = max_stop_pts
                sl = entry_price + risk
            tp = entry_price - RR * risk

        in_pos = True
        pos_side = direction
        traded_pivots.add(pivot)
        cooldown_until = 0
        i = entry_idx + 1

    # 生成统计
    td = pd.DataFrame(trades) if trades else pd.DataFrame()
    total = len(td)
    if total:
        wins = len(td[td["result"] == "win"])
        losses = total - wins
        wr = wins / total if total > 0 else 0

        # 价格层面统计
        avg_ret = td["net_return"].mean()
        gp = td[td["result"] == "win"]["net_return"].sum()
        gl = abs(td[td["result"] == "loss"]["net_return"].sum())
        pf = gp / gl if gl > 0 else float('inf') if gp > 0 else 0

        # 杠杆后统计 (100x × 5%保证金 = 5x)
        cum_ret = (1 + td["account_return"]).prod() - 1

        # 最大回撤 (杠杆后)
        equity = (1 + td["account_return"]).cumprod()
        rolling_max = equity.cummax()
        drawdown = (equity / rolling_max) - 1
        max_dd = abs(drawdown.min())

        # 连续亏损
        cons = 0
        max_cons = 0
        for _, r in td.iterrows():
            cons = cons + 1 if r["result"] == "loss" else 0
            max_cons = max(max_cons, cons)

        # 多头/空头拆分
        long_trades = td[td["side"] == "long"]
        short_trades = td[td["side"] == "short"]
        long_wins = len(long_trades[long_trades["result"] == "win"])
        short_wins = len(short_trades[short_trades["result"] == "win"])
        long_wr = long_wins / len(long_trades) if len(long_trades) > 0 else 0
        short_wr = short_wins / len(short_trades) if len(short_trades) > 0 else 0

        avg_win_ret = td[td["net_return"] > 0]["net_return"].mean() if len(td[td["net_return"] > 0]) > 0 else 0
        avg_loss_ret = td[td["net_return"] < 0]["net_return"].mean() if len(td[td["net_return"] < 0]) > 0 else 0
        max_win = td["net_return"].max()
        max_loss = td["net_return"].min()

        # 平均持仓K线
        avg_hold = float((td["exit_idx"] - td["entry_idx"]).mean())

        stats = {
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": float(wr),
            "total_return": float(cum_ret),
            "avg_return": float(avg_ret),
            "profit_factor": float(pf) if pf != float('inf') else 999.0,
            "max_drawdown": float(max_dd),
            "initial_capital": 10000.0,
            "final_capital": float(10000.0 * (1 + cum_ret)),
            "long_trades": int(len(long_trades)),
            "short_trades": int(len(short_trades)),
            "long_win_rate": float(long_wr),
            "short_win_rate": float(short_wr),
            "avg_win_return": float(avg_win_ret),
            "avg_loss_return": float(avg_loss_ret),
            "max_win": float(max_win),
            "max_loss": float(max_loss),
            "max_consecutive_losses": int(max_cons),
            "avg_hold_bars": avg_hold,
            "leverage": LEVERAGE,
            "margin_rate": MARGIN_RATE,
            "notional_mult": NOTIONAL_MULT,
            "rr": RR,
        }

        # 资金曲线 (每笔交易后)
        equity_curve = []
        equity_val = 10000.0
        for idx, row in td.iterrows():
            equity_val *= (1 + row["account_return"])
            equity_curve.append({
                "trade_no": int(idx) + 1,
                "bar_idx": int(row["exit_idx"]),
                "capital": float(equity_val),
                "account_return": float(row["account_return"]),
            })
    else:
        stats = {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_return": 0, "avg_return": 0, "profit_factor": 0,
            "max_drawdown": 0, "initial_capital": 10000.0, "final_capital": 10000.0,
            "long_trades": 0, "short_trades": 0, "long_win_rate": 0, "short_win_rate": 0,
            "avg_win_return": 0, "avg_loss_return": 0, "max_win": 0, "max_loss": 0,
            "max_consecutive_losses": 0, "avg_hold_bars": 0,
            "leverage": LEVERAGE, "margin_rate": MARGIN_RATE, "notional_mult": NOTIONAL_MULT,
            "rr": RR,
        }
        equity_curve = []

    return td, stats, m30f, equity_curve


def main():
    print("=" * 65)
    print("回测: 30m分型(5,2) + 1h共振 | RR=1:1 | 100x杠杆 | 冷却3根")
    print("=" * 65)

    results = {}

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*50}")
        print(f"  [{name}] 加载数据...")
        print(f"{'─'*50}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])

        print(f"  30m: {len(m30)}根 | 1h: {len(h1)}根")
        print(f"  时间范围: {m30['datetime'].iloc[0]} ~ {m30['datetime'].iloc[-1]}")

        trades_df, stats, m30f, equity = run_rr1_backtest(
            m30, h1,
            {"max_stop_pct": cfg["max_stop_pct"], "max_stop_pts": cfg["max_stop_pts"]}
        )

        results[name] = {"stats": stats, "trades_count": len(trades_df)}

        print(f"\n  >>> {name} 回测结果 (RR=1:1, {LEVERAGE}x杠杆 × {MARGIN_RATE*100:.0f}%保证金 = {NOTIONAL_MULT}x敞口):")
        print(f"  交易次数: {stats['trades']} (胜{stats['wins']} / 负{stats['losses']})")
        print(f"  胜率: {stats['win_rate']*100:.1f}%")
        print(f"  累计收益(含杠杆): {stats['total_return']*100:+.2f}%")
        print(f"  平均单笔(杠杆后): {stats['avg_return']*NOTIONAL_MULT*100:+.2f}%")
        print(f"  盈利因子: {stats['profit_factor']:.3f}")
        print(f"  最大回撤: {stats['max_drawdown']*100:.2f}%")
        print(f"  最大连续亏损: {stats['max_consecutive_losses']}笔")
        print(f"  多头交易: {stats['long_trades']} (胜率{stats['long_win_rate']*100:.1f}%)")
        print(f"  空头交易: {stats['short_trades']} (胜率{stats['short_win_rate']*100:.1f}%)")

        # 保存交易CSV
        if len(trades_df) > 0:
            output_csv = f"results/rr1_{name}_trades.csv"
            trades_df.to_csv(output_csv, index=False)
            print(f"  交易明细已保存: {output_csv}")

        # 按月统计
        if len(trades_df) > 0:
            td = trades_df.copy()
            td["month"] = pd.to_datetime(td["entry_time"]).dt.strftime("%Y-%m")
            print("  按月收益(含杠杆):")
            for m, grp in td.groupby("month"):
                w = len(grp[grp["result"] == "win"])
                l_len = len(grp) - w
                r = (1 + grp["account_return"]).prod() - 1
                print(f"    {m}: {len(grp)}笔 胜{w}负{l_len} 月收益{r*100:+.1f}%")

        # 生成网页JSON数据
        # K线采样
        kline_data = []
        sample_step = max(1, len(m30f) // 600)
        for idx in range(0, len(m30f), sample_step):
            row = m30f.iloc[idx]
            kline_data.append({
                "idx": int(idx),
                "time": str(row["datetime"]),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": float(row["volume"]),
            })

        trade_marks = []
        for _, t in trades_df.iterrows():
            trade_marks.append({
                "entry_idx": int(t["entry_idx"]),
                "exit_idx": int(t["exit_idx"]),
                "side": t["side"],
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "entry_price": round(float(t["entry_price"]), 2),
                "exit_price": round(float(t["exit_price"]), 2),
                "stop_loss": round(float(t["stop_loss"]), 2),
                "take_profit": round(float(t["take_profit"]), 2),
                "result": t["result"],
                "net_return": round(float(t["net_return"]), 6),
                "account_return": round(float(t["account_return"]), 6),
                "rr": RR,
            })

        web_data = {
            "symbol": f"{name}/USDT",
            "params": {
                "left": LEFT, "right": RIGHT, "rr": RR,
                "sl_buffer": SL_BUFFER, "fee": FEE,
                "timeframe": "30m", "higher_tf": "1h",
                "cooldown_bars": COOLDOWN_BARS,
                "leverage": LEVERAGE, "margin_rate": MARGIN_RATE,
                "notional_mult": NOTIONAL_MULT,
                "max_stop_pct": cfg["max_stop_pct"],
                "max_stop_pts": cfg["max_stop_pts"],
                "ai_filter": False,
                "risk_control": False,
            },
            "stats": stats,
            "kline": kline_data,
            "trades": trade_marks,
            "equity": equity,
        }

        json_path = f"results/rr1_{name}_web_data.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(web_data, f, ensure_ascii=False, default=str)
        print(f"  网页数据已保存: {json_path} ({len(kline_data)} K线, {len(trade_marks)} 交易)")

    # 总结
    print(f"\n{'='*65}")
    print("回测总结 (RR=1:1, 100x杠杆)")
    print(f"{'='*65}")
    for sym, r in results.items():
        s = r["stats"]
        print(f"  {sym}: {r['trades_count']}笔 胜率{s['win_rate']*100:.1f}% "
              f"累计收益{s['total_return']*100:+.2f}% PF={s['profit_factor']:.3f} "
              f"最大回撤{s['max_drawdown']*100:.2f}%")

    print("\n[OK] RR=1 回测全部完成！")


if __name__ == "__main__":
    main()
