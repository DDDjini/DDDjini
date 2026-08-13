"""
完整策略回测 + 生成交互式网页数据
严格按策略：分型(5,2) + 多周期共振(1h) + AI过滤 + 风控
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import json
from pathlib import Path

from backtest_fractal import add_fractals, analyze_trades
from risk_control import RiskController, AIFilter


def run_backtest(
    df_30m: pd.DataFrame,
    df_1h: pd.DataFrame,
    symbol: str,
    left: int = 5,
    right: int = 2,
    rr: float = 2.0,
    sl_buffer: float = 0.0005,
    fee_rate: float = 0.0005,
    initial_capital: float = 10000.0,
    use_ai_filter: bool = True,
    use_risk_control: bool = True,
    max_loss_per_trade: float = 0.02,
    max_daily_loss: float = 0.05,
    max_consecutive_losses: int = 3,
    pause_after_losses: int = 5,
    max_stop_pct: float = 0.017,
    alt_rr: float = 1.0,
):
    """
    严格按策略回测：
    - 分型信号检测 (left, right)
    - 1h 多周期共振过滤
    - AI 技术指标过滤 (PSY + MACD + 布林带 + ATR)
    - 风控模块 (单笔止损、日亏、连续亏损暂停、回撤)
    - 止损截断 (max_stop_pct)
    """
    # 添加分型到30m数据
    df = df_30m.copy()
    df = add_fractals(df, left, right)

    # 多周期共振：1h分型过滤
    h1 = df_1h.copy()
    h1 = add_fractals(h1, left=2, right=2)
    mask = h1['fractal_low'].values | h1['fractal_high'].values
    ts_vals = h1['timestamp'].values[mask]
    type_vals = np.where(h1['fractal_low'].values[mask], 'low', 'high')
    h1_events = list(zip(ts_vals, type_vals))
    h1_events.sort(key=lambda x: x[0])

    ts_array = df['timestamp'].values
    event_ts = np.array([e[0] for e in h1_events])
    event_types = np.array([e[1] for e in h1_events])
    idx = np.searchsorted(event_ts, ts_array, side='right') - 1
    h1_trend = np.where(idx >= 0, event_types[idx], None)
    df['h1_trend'] = h1_trend

    # AI 指标过滤
    ai_filter = AIFilter(df) if use_ai_filter else None
    df = ai_filter.get_df() if use_ai_filter else df

    # 风控模块
    risk_config = {
        "initial_capital": initial_capital,
        "max_loss_per_trade": max_loss_per_trade,
        "max_daily_loss": max_daily_loss,
        "max_consecutive_losses": max_consecutive_losses,
        "pause_after_losses": pause_after_losses,
        "max_drawdown_limit": 1.0,
    }
    risk = RiskController(**risk_config)

    trades = []
    equity_curve = []  # 每笔交易后的资金曲线
    i = left + right + 1

    bar_count = 0
    while i < len(df) - 1:
        bar_count += 1
        pivot_idx = i - right
        if pivot_idx < 0:
            i += 1
            continue

        signal = None
        if df.loc[pivot_idx, "fractal_low"]:
            signal = "long"
        elif df.loc[pivot_idx, "fractal_high"]:
            signal = "short"

        if signal is None:
            i += 1
            continue

        # 多周期共振过滤
        h1_trend_val = df.loc[pivot_idx, 'h1_trend']
        if h1_trend_val is None or (isinstance(h1_trend_val, float) and np.isnan(h1_trend_val)):
            i += 1
            continue
        if signal == 'long' and h1_trend_val != 'low':
            i += 1
            continue
        if signal == 'short' and h1_trend_val != 'high':
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            break

        entry_price = df.loc[entry_idx, "open"]
        entry_time = df.loc[entry_idx, "datetime"]
        bar_dt = pd.to_datetime(entry_time)

        if signal == "long":
            pivot_low = df.loc[pivot_idx, "low"]
            stop_loss = pivot_low * (1 - sl_buffer)
            risk_amount = entry_price - stop_loss
            if risk_amount <= 0:
                i += 1
                continue

            used_rr = rr
            if max_stop_pct is not None:
                max_risk = entry_price * max_stop_pct
                if risk_amount > max_risk:
                    risk_amount = max_risk
                    stop_loss = entry_price - max_risk
                    used_rr = alt_rr if alt_rr is not None else rr

            take_profit = entry_price + used_rr * risk_amount
        else:
            pivot_high = df.loc[pivot_idx, "high"]
            stop_loss = pivot_high * (1 + sl_buffer)
            risk_amount = stop_loss - entry_price
            if risk_amount <= 0:
                i += 1
                continue

            used_rr = rr
            if max_stop_pct is not None:
                max_risk = entry_price * max_stop_pct
                if risk_amount > max_risk:
                    risk_amount = max_risk
                    stop_loss = entry_price + max_risk
                    used_rr = alt_rr if alt_rr is not None else rr

            take_profit = entry_price - used_rr * risk_amount

        # 风控检查
        if use_risk_control and not risk.can_trade(entry_idx, bar_dt):
            i += 1
            continue

        # AI 过滤
        if use_ai_filter:
            allowed, reason = ai_filter.filter_signal(entry_idx, signal)
            if not allowed:
                i += 1
                continue

        # 计算仓位
        position_size = risk.position_size(entry_price, stop_loss) if use_risk_control else 1.0

        # 找出场
        exit_idx = None
        exit_price = None
        result = None
        j = entry_idx + 1

        while j < len(df):
            high = df.loc[j, "high"]
            low = df.loc[j, "low"]

            if signal == "long":
                hit_sl = low <= stop_loss
                hit_tp = high >= take_profit
                if hit_sl and hit_tp:
                    exit_idx = j
                    exit_price = stop_loss
                    result = "loss"
                    break
                elif hit_sl:
                    exit_idx = j
                    exit_price = stop_loss
                    result = "loss"
                    break
                elif hit_tp:
                    exit_idx = j
                    exit_price = take_profit
                    result = "win"
                    break
            else:
                hit_sl = high >= stop_loss
                hit_tp = low <= take_profit
                if hit_sl and hit_tp:
                    exit_idx = j
                    exit_price = stop_loss
                    result = "loss"
                    break
                elif hit_sl:
                    exit_idx = j
                    exit_price = stop_loss
                    result = "loss"
                    break
                elif hit_tp:
                    exit_idx = j
                    exit_price = take_profit
                    result = "win"
                    break
            j += 1

        if exit_idx is None:
            break

        exit_time = df.loc[exit_idx, "datetime"]

        if signal == "long":
            gross_return = (exit_price - entry_price) / entry_price
        else:
            gross_return = (entry_price - exit_price) / entry_price

        net_return = gross_return - fee_rate * 2
        gross_pnl = net_return * entry_price * position_size if use_risk_control else net_return

        # 记录风控
        if use_risk_control:
            risk.record_trade(exit_idx, net_return, gross_pnl)

        entry_idx_val = int(entry_idx)
        exit_idx_val = int(exit_idx)
        pivot_idx_val = int(pivot_idx)

        trades.append({
            "entry_idx": entry_idx_val,
            "exit_idx": exit_idx_val,
            "pivot_idx": pivot_idx_val,
            "side": signal,
            "entry_time": str(entry_time),
            "exit_time": str(exit_time),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
            "result": result,
            "net_return": float(net_return),
            "risk_amount": float(risk_amount),
            "rr": float(used_rr),
        })

        equity_curve.append({
            "trade_no": len(equity_curve) + 1,
            "bar_idx": exit_idx_val,
            "capital": float(risk.capital) if use_risk_control else float(10000 * (1 + sum(t["net_return"] for t in trades))),
        })

        i = exit_idx + 1

    trades_df = pd.DataFrame(trades)

    # 统计
    if trades_df.empty:
        stats = {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_return": 0, "avg_return": 0, "profit_factor": 0,
            "max_drawdown": 0,
            "final_capital": initial_capital,
            "initial_capital": initial_capital,
            "long_trades": 0, "short_trades": 0,
            "avg_win_return": 0, "avg_loss_return": 0,
            "max_win": 0, "max_loss": 0,
            "avg_hold_bars": 0,
        }
    else:
        total = len(trades_df)
        wins = len(trades_df[trades_df["net_return"] > 0])
        losses = total - wins
        win_rate = wins / total if total > 0 else 0

        if use_risk_control:
            final_capital = risk.capital
            total_return = (final_capital - initial_capital) / initial_capital
        else:
            equity = (1 + trades_df["net_return"]).cumprod()
            total_return = equity.iloc[-1] - 1
            final_capital = initial_capital * (1 + total_return)

        avg_return = trades_df["net_return"].mean()

        gross_profit = trades_df.loc[trades_df["net_return"] > 0, "net_return"].sum()
        gross_loss = abs(trades_df.loc[trades_df["net_return"] <= 0, "net_return"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # 资金曲线最大回撤
        if use_risk_control and hasattr(risk, '_max_dd'):
            max_drawdown = risk._max_dd
        else:
            equity_series = (1 + trades_df["net_return"]).cumprod()
            rolling_max = equity_series.cummax()
            drawdown = equity_series / rolling_max - 1
            max_drawdown = abs(drawdown.min())

        long_trades = int(len(trades_df[trades_df["side"] == "long"]))
        short_trades = int(len(trades_df[trades_df["side"] == "short"]))

        win_trades = trades_df[trades_df["net_return"] > 0]
        loss_trades = trades_df[trades_df["net_return"] <= 0]
        avg_win_return = float(win_trades["net_return"].mean()) if len(win_trades) > 0 else 0
        avg_loss_return = float(loss_trades["net_return"].mean()) if len(loss_trades) > 0 else 0
        max_win = float(trades_df["net_return"].max())
        max_loss = float(trades_df["net_return"].min())
        avg_hold_bars = float((trades_df["exit_idx"] - trades_df["entry_idx"]).mean())

        stats = {
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": float(win_rate),
            "total_return": float(total_return),
            "avg_return": float(avg_return),
            "profit_factor": float(profit_factor) if profit_factor != float('inf') else 999.0,
            "max_drawdown": float(max_drawdown),
            "final_capital": float(final_capital),
            "initial_capital": float(initial_capital),
            "long_trades": long_trades,
            "short_trades": short_trades,
            "avg_win_return": avg_win_return,
            "avg_loss_return": avg_loss_return,
            "max_win": max_win,
            "max_loss": max_loss,
            "avg_hold_bars": avg_hold_bars,
        }

    return trades_df, stats, df, equity_curve


def main():
    print("=" * 60)
    print("策略回测系统：分型(5,2) + 多周期共振(1h) + AI过滤 + 风控")
    print("=" * 60)

    results = {}

    # BTC 回测
    print("\n>>> 加载 BTC 数据...")
    btc_30m_path = "data/bt_BTC_USDT_USDT_30m_2025-08-12.csv"
    btc_1h_path = "data/bt_BTC_USDT_USDT_1h_2025-08-12.csv"

    if Path(btc_30m_path).exists():
        btc_30m = pd.read_csv(btc_30m_path)
        btc_30m["datetime"] = pd.to_datetime(btc_30m["datetime"])
        btc_1h = pd.read_csv(btc_1h_path)
        btc_1h["datetime"] = pd.to_datetime(btc_1h["datetime"])
        print(f"  BTC 30m: {len(btc_30m)} bars, {btc_30m['datetime'].iloc[0]} ~ {btc_30m['datetime'].iloc[-1]}")
        print(f"  BTC 1h:  {len(btc_1h)} bars")

        print("\n>>> BTC 回测运行中...")
        trades, stats, df_with_indicators, equity = run_backtest(
            btc_30m, btc_1h, "BTC/USDT",
            left=5, right=2, rr=2.0, sl_buffer=0.0005,
            use_ai_filter=True, use_risk_control=True,
            max_stop_pct=0.017, alt_rr=1.0,
        )
        results["BTC"] = {"stats": stats, "trades_count": len(trades)}

        print(f"\n--- BTC 回测结果 ---")
        print(f"  交易次数: {stats['trades']}")
        print(f"  胜率: {stats['win_rate']*100:.2f}%")
        print(f"  累计收益: {stats['total_return']*100:.2f}%")
        print(f"  盈亏因子: {stats['profit_factor']:.3f}")
        print(f"  最大回撤: {stats['max_drawdown']*100:.2f}%")
        print(f"  最终资金: {stats['final_capital']:.2f} USDT")
        print(f"  多头交易: {stats['long_trades']}, 空头交易: {stats['short_trades']}")

        # 保存交易记录
        trades.to_csv("results/btc_backtest_trades.csv", index=False)
        print(f"  交易明细已保存: results/btc_backtest_trades.csv")

        # 生成 K线 + 买卖点 JSON 数据 (采样展示，防止过大)
        kline_data = []
        sample_step = max(1, len(df_with_indicators) // 3000)  # 最多3000根K线
        for i in range(0, len(df_with_indicators), sample_step):
            row = df_with_indicators.iloc[i]
            kline_data.append({
                "idx": int(i),
                "time": str(row["datetime"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })

        trade_marks = []
        for _, t in trades.iterrows():
            trade_marks.append({
                "entry_idx": int(t["entry_idx"]),
                "exit_idx": int(t["exit_idx"]),
                "pivot_idx": int(t["pivot_idx"]),
                "side": t["side"],
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "entry_price": float(t["entry_price"]),
                "exit_price": float(t["exit_price"]),
                "stop_loss": float(t["stop_loss"]),
                "take_profit": float(t["take_profit"]),
                "result": t["result"],
                "net_return": float(t["net_return"]),
                "rr": float(t["rr"]),
            })

        # 保存完整JSON供网页使用
        web_data = {
            "symbol": "BTC/USDT",
            "params": {
                "left": 5, "right": 2, "rr": 2.0, "sl_buffer": 0.0005,
                "timeframe": "30m", "higher_tf": "1h",
                "ai_filter": True, "risk_control": True,
                "max_stop_pct": 0.017, "alt_rr": 1.0,
                "fee_rate": 0.0005,
            },
            "stats": stats,
            "kline": kline_data,
            "trades": trade_marks,
            "equity": equity,
        }
        with open("results/btc_web_data.json", "w", encoding="utf-8") as f:
            json.dump(web_data, f, ensure_ascii=False, default=str)
        print(f"  网页数据已保存: results/btc_web_data.json ({len(kline_data)} K线, {len(trade_marks)} 交易)")
    else:
        print(f"  BTC数据文件不存在: {btc_30m_path}")

    # ETH 回测
    print("\n>>> 加载 ETH 数据...")
    eth_30m_path = "data/bt_ETH_USDT_USDT_30m_2025-08-12.csv"
    eth_1h_path = "data/bt_ETH_USDT_USDT_1h_2025-08-12.csv"

    if Path(eth_30m_path).exists():
        eth_30m = pd.read_csv(eth_30m_path)
        eth_30m["datetime"] = pd.to_datetime(eth_30m["datetime"])
        eth_1h = pd.read_csv(eth_1h_path)
        eth_1h["datetime"] = pd.to_datetime(eth_1h["datetime"])
        print(f"  ETH 30m: {len(eth_30m)} bars, {eth_30m['datetime'].iloc[0]} ~ {eth_30m['datetime'].iloc[-1]}")
        print(f"  ETH 1h:  {len(eth_1h)} bars")

        print("\n>>> ETH 回测运行中...")
        trades, stats, df_with_indicators, equity = run_backtest(
            eth_30m, eth_1h, "ETH/USDT",
            left=5, right=2, rr=2.0, sl_buffer=0.0005,
            use_ai_filter=True, use_risk_control=True,
            max_stop_pct=0.017, alt_rr=1.0,
        )
        results["ETH"] = {"stats": stats, "trades_count": len(trades)}

        print(f"\n--- ETH 回测结果 ---")
        print(f"  交易次数: {stats['trades']}")
        print(f"  胜率: {stats['win_rate']*100:.2f}%")
        print(f"  累计收益: {stats['total_return']*100:.2f}%")
        print(f"  盈亏因子: {stats['profit_factor']:.3f}")
        print(f"  最大回撤: {stats['max_drawdown']*100:.2f}%")
        print(f"  最终资金: {stats['final_capital']:.2f} USDT")
        print(f"  多头交易: {stats['long_trades']}, 空头交易: {stats['short_trades']}")

        trades.to_csv("results/eth_backtest_trades.csv", index=False)

        kline_data = []
        sample_step = max(1, len(df_with_indicators) // 3000)
        for i in range(0, len(df_with_indicators), sample_step):
            row = df_with_indicators.iloc[i]
            kline_data.append({
                "idx": int(i),
                "time": str(row["datetime"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })

        trade_marks = []
        for _, t in trades.iterrows():
            trade_marks.append({
                "entry_idx": int(t["entry_idx"]),
                "exit_idx": int(t["exit_idx"]),
                "pivot_idx": int(t["pivot_idx"]),
                "side": t["side"],
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "entry_price": float(t["entry_price"]),
                "exit_price": float(t["exit_price"]),
                "stop_loss": float(t["stop_loss"]),
                "take_profit": float(t["take_profit"]),
                "result": t["result"],
                "net_return": float(t["net_return"]),
                "rr": float(t["rr"]),
            })

        web_data = {
            "symbol": "ETH/USDT",
            "params": {
                "left": 5, "right": 2, "rr": 2.0, "sl_buffer": 0.0005,
                "timeframe": "30m", "higher_tf": "1h",
                "ai_filter": True, "risk_control": True,
                "max_stop_pct": 0.017, "alt_rr": 1.0,
                "fee_rate": 0.0005,
            },
            "stats": stats,
            "kline": kline_data,
            "trades": trade_marks,
            "equity": equity,
        }
        with open("results/eth_web_data.json", "w", encoding="utf-8") as f:
            json.dump(web_data, f, ensure_ascii=False, default=str)
        print(f"  网页数据已保存: results/eth_web_data.json ({len(kline_data)} K线, {len(trade_marks)} 交易)")

    # 总结
    print("\n" + "=" * 60)
    print("回测总结")
    print("=" * 60)
    for sym, r in results.items():
        s = r["stats"]
        print(f"  {sym}: 交易{r['trades_count']}笔, 胜率{s['win_rate']*100:.2f}%, "
              f"收益{s['total_return']*100:.2f}%, PF={s['profit_factor']:.3f}, "
              f"回撤{s['max_drawdown']*100:.2f}%")

    print("\n[OK] 回测全部完成！")


if __name__ == "__main__":
    main()
