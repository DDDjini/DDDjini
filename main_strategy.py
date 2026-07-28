import pandas as pd
import numpy as np
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

from backtest_fractal import (
    add_fractals, fetch_ohlcv, generate_mock_data, ensure_dir, utc_ms
)
from risk_control import RiskController, AIFilter


# =========================
# 主策略：分型 + 多周期共振 + AI过滤 + 风控
# =========================

def main_strategy_backtest(
    df: pd.DataFrame,
    timeframe: str,
    left: int,
    right: int,
    rr: float,
    sl_buffer: float,
    fee_rate: float = 0.0005,
    higher_tf_df: pd.DataFrame = None,
    use_ai_filter: bool = True,
    risk_config: dict = None,
    max_stop_pct: float = None,
    alt_rr: float = None,
    use_trailing_stop: bool = False,
    trailing_activation_r: float = 1.0,
    trailing_atr_multiplier: float = 2.0,
    news_events: list = None,
    news_pause_minutes: int = 30,
):
    """
    完整策略回测：分型 + 多周期共振 + AI过滤 + 风控 + 止损截断 + 移动止损 + 新闻事件过滤
    """

    df = add_fractals(df, left, right)

    # 多周期共振
    if higher_tf_df is not None:
        higher_tf_df = add_fractals(higher_tf_df.copy(), left=2, right=2)
        mask = higher_tf_df['fractal_low'].values | higher_tf_df['fractal_high'].values
        ts_vals = higher_tf_df['timestamp'].values[mask]
        type_vals = np.where(higher_tf_df['fractal_low'].values[mask], 'low', 'high')
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

    # 计算ATR（移动止损用）
    if use_trailing_stop and 'atr' not in df.columns:
        tr1 = df['high'] - df['low']
        tr2 = abs(df['high'] - df['close'].shift(1))
        tr3 = abs(df['low'] - df['close'].shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean()

    # 预处理新闻事件时间（转为时间戳便于快速比较）
    news_timestamps = []
    if news_events:
        for evt_time in news_events:
            if isinstance(evt_time, str):
                evt_time = pd.to_datetime(evt_time)
            news_timestamps.append(evt_time.timestamp() * 1000)

    # 风控模块
    risk_config = risk_config or {}
    risk = RiskController(**risk_config)

    trades = []
    i = left + right + 1

    while i < len(df) - 1:
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
        if higher_tf_df is not None:
            h1_trend = df.loc[pivot_idx, 'h1_trend']
            if h1_trend is None or pd.isna(h1_trend):
                i += 1
                continue
            if signal == 'long' and h1_trend != 'low':
                i += 1
                continue
            if signal == 'short' and h1_trend != 'high':
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

            # 最大止损限制 + 截断后 RR 调整
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

            # 最大止损限制 + 截断后 RR 调整
            used_rr = rr
            if max_stop_pct is not None:
                max_risk = entry_price * max_stop_pct
                if risk_amount > max_risk:
                    risk_amount = max_risk
                    stop_loss = entry_price + max_risk
                    used_rr = alt_rr if alt_rr is not None else rr

            take_profit = entry_price - used_rr * risk_amount

        # 风控：检查是否可交易
        if not risk.can_trade(entry_idx, bar_dt):
            i += 1
            continue

        # 新闻事件过滤：重大数据公布前30分钟暂停开仓
        if news_timestamps:
            entry_ts = df.loc[entry_idx, "timestamp"]
            pause_ms = news_pause_minutes * 60 * 1000
            near_news = any(abs(entry_ts - evt_ts) < pause_ms for evt_ts in news_timestamps)
            if near_news:
                i += 1
                continue

        # 风控：计算仓位
        position_size = risk.position_size(entry_price, stop_loss)
        if position_size <= 0:
            i += 1
            continue

        # AI 过滤
        if use_ai_filter:
            allowed, reason = ai_filter.filter_signal(entry_idx, signal)
            if not allowed:
                i += 1
                continue

        # 找出场
        exit_idx = None
        exit_price = None
        result = None
        j = entry_idx + 1

        # 移动止损状态
        trailing_activated = False
        current_sl = stop_loss

        while j < len(df):
            high = df.loc[j, "high"]
            low = df.loc[j, "low"]
            close = df.loc[j, "close"]

            # 移动止损更新逻辑
            if use_trailing_stop:
                if signal == "long":
                    unrealized_r = (close - entry_price) / risk_amount
                    if not trailing_activated and unrealized_r >= trailing_activation_r:
                        trailing_activated = True
                    if trailing_activated:
                        atr_val = df.loc[j, 'atr'] if 'atr' in df.columns else risk_amount * 0.5
                        new_sl = close - trailing_atr_multiplier * atr_val
                        if new_sl > current_sl:
                            current_sl = new_sl
                else:  # short
                    unrealized_r = (entry_price - close) / risk_amount
                    if not trailing_activated and unrealized_r >= trailing_activation_r:
                        trailing_activated = True
                    if trailing_activated:
                        atr_val = df.loc[j, 'atr'] if 'atr' in df.columns else risk_amount * 0.5
                        new_sl = close + trailing_atr_multiplier * atr_val
                        if new_sl < current_sl:
                            current_sl = new_sl

            sl_to_use = current_sl if use_trailing_stop else stop_loss

            if signal == "long":
                hit_sl = low <= sl_to_use
                hit_tp = high >= take_profit
                if hit_sl and hit_tp:
                    exit_idx = j
                    exit_price = sl_to_use
                    result = "loss"
                    break
                elif hit_sl:
                    exit_idx = j
                    exit_price = sl_to_use
                    result = "loss"
                    break
                elif hit_tp:
                    exit_idx = j
                    exit_price = take_profit
                    result = "win"
                    break
            else:
                hit_sl = high >= sl_to_use
                hit_tp = low <= take_profit
                if hit_sl and hit_tp:
                    exit_idx = j
                    exit_price = sl_to_use
                    result = "loss"
                    break
                elif hit_sl:
                    exit_idx = j
                    exit_price = sl_to_use
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
        gross_pnl = net_return * entry_price * position_size

        # 记录风控
        risk.record_trade(exit_idx, net_return)

        trades.append({
            "timeframe": timeframe,
            "side": signal,
            "entry_time": entry_time,
            "entry_price": entry_price,
            "exit_time": exit_time,
            "exit_price": exit_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "result": result,
            "gross_return": gross_return,
            "net_return": net_return,
            "position_size": position_size,
            "capital_after": risk.capital,
            "rr": used_rr,
            "left": left,
            "right": right,
            "sl_buffer": sl_buffer,
            "trailing_used": use_trailing_stop,
            "trailing_activated": trailing_activated if use_trailing_stop else False,
        })

        i = exit_idx + 1

    trades_df = pd.DataFrame(trades)
    return trades_df, risk


def analyze_trades_with_capital(trades: pd.DataFrame, risk: RiskController):
    """带资金曲线的统计"""
    if trades.empty:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_return": 0, "avg_return": 0, "profit_factor": 0, "max_drawdown": 0,
            "final_capital": risk.capital, "initial_capital": risk.initial_capital,
        }

    trades = trades.copy()
    total = len(trades)
    wins = len(trades[trades["net_return"] > 0])
    losses = total - wins
    win_rate = wins / total if total > 0 else 0

    total_return = (risk.capital - risk.initial_capital) / risk.initial_capital
    avg_return = trades["net_return"].mean()

    gross_profit = trades.loc[trades["net_return"] > 0, "net_return"].sum()
    gross_loss = abs(trades.loc[trades["net_return"] < 0, "net_return"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    max_drawdown = getattr(risk, '_max_dd', 0)
    if max_drawdown == 0:
        max_drawdown = (risk.peak_capital - risk.capital) / risk.peak_capital if risk.peak_capital > 0 else 0

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_return": total_return,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "final_capital": risk.capital,
        "initial_capital": risk.initial_capital,
    }


def print_report(name: str, stats: dict):
    print("\n" + "=" * 60)
    print(f"回测报告：{name}")
    print("=" * 60)
    print(f"交易次数       : {stats['trades']}")
    print(f"盈利次数       : {stats['wins']}")
    print(f"亏损次数       : {stats['losses']}")
    print(f"胜率           : {stats['win_rate'] * 100:.2f}%")
    print(f"初始资金       : {stats['initial_capital']:.2f} USDT")
    print(f"最终资金       : {stats['final_capital']:.2f} USDT")
    print(f"累计收益       : {stats['total_return'] * 100:.2f}%")
    print(f"平均单笔收益   : {stats['avg_return'] * 100:.4f}%")
    print(f"盈亏因子       : {stats['profit_factor']:.3f}")
    print(f"最大回撤       : {stats['max_drawdown'] * 100:.2f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="使用模拟数据")
    parser.add_argument("--exchange", type=str, default="okx")
    parser.add_argument("--symbol", type=str, default="BTC/USDT:USDT")
    parser.add_argument("--since", type=str, default="2025-01-01")
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--no-ai", action="store_true", help="关闭AI过滤")
    parser.add_argument("--no-risk", action="store_true", help="关闭风控模块")
    parser.add_argument("--capital", type=float, default=10000.0, help="初始资金")
    parser.add_argument("--max-loss", type=float, default=0.02, help="单笔最大亏损比例")
    parser.add_argument("--max-daily", type=float, default=0.05, help="日最大亏损比例")
    parser.add_argument("--consecutive", type=int, default=3, help="连续亏损暂停笔数")
    parser.add_argument("--pause", type=int, default=5, help="暂停K线数")
    # 新增参数
    parser.add_argument("--max-stop-pct", type=float, default=0.017, help="最大止损比例(止损截断)")
    parser.add_argument("--alt-rr", type=float, default=1.0, help="截断后的盈亏比目标")
    parser.add_argument("--trailing-stop", action="store_true", help="启用移动止损")
    parser.add_argument("--trailing-activation-r", type=float, default=1.0, help="移动止损触发倍数(1R)")
    parser.add_argument("--trailing-atr-mult", type=float, default=2.0, help="移动止损ATR倍数")
    args = parser.parse_args()

    ensure_dir("data")
    ensure_dir("results")

    # 加载数据
    m30_csv = f"data/{args.exchange}_{args.symbol.replace('/', '_').replace(':', '_')}_30m.csv"
    h1_csv = f"data/{args.exchange}_{args.symbol.replace('/', '_').replace(':', '_')}_1h.csv"

    if args.offline:
        m30_csv = m30_csv.replace(f"{args.exchange}_", "mock_")
        h1_csv = h1_csv.replace(f"{args.exchange}_", "mock_")

    print(f"加载 30m 数据: {m30_csv}")
    if Path(m30_csv).exists():
        m30_df = pd.read_csv(m30_csv)
        m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    else:
        if args.offline:
            m30_df = generate_mock_data("30m", args.since, bars=2000)
            m30_df.to_csv(m30_csv, index=False)
        else:
            m30_df = fetch_ohlcv(args.symbol, "30m", args.since, exchange_id=args.exchange, proxy="http://127.0.0.1:7897")
            m30_df.to_csv(m30_csv, index=False)

    print(f"加载 1h 数据: {h1_csv}")
    if Path(h1_csv).exists():
        h1_df = pd.read_csv(h1_csv)
        h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])
    else:
        if args.offline:
            h1_df = generate_mock_data("1h", args.since, bars=2000)
            h1_df.to_csv(h1_csv, index=False)
        else:
            h1_df = fetch_ohlcv(args.symbol, "1h", args.since, exchange_id=args.exchange, proxy="http://127.0.0.1:7897")
            h1_df.to_csv(h1_csv, index=False)

    # RR=2.0 最优参数
    PARAMS = {
        "left": 5,
        "right": 2,
        "rr": 2.0,
        "sl_buffer": 0.0005,
    }

    risk_config = {
        "initial_capital": args.capital,
        "max_loss_per_trade": 1.0,       # 无风控：单笔不限制
        "max_daily_loss": 1.0,            # 无风控：日亏不限制
        "max_consecutive_losses": 9999,   # 无风控：不暂停
        "pause_after_losses": 0,
        "max_drawdown_limit": 1.0,        # 无风控：不限制回撤
    } if args.no_risk else {
        "initial_capital": args.capital,
        "max_loss_per_trade": args.max_loss,
        "max_daily_loss": args.max_daily,
        "max_consecutive_losses": args.consecutive,
        "pause_after_losses": args.pause,
    }

    print(f"\n策略参数: {PARAMS}")
    print(f"多周期共振: 1h 分型过滤")
    print(f"AI 过滤: {'关闭' if args.no_ai else '开启'} (PSY心理线 + MACD + 布林带 + ATR)")
    print(f"风控模块: {'关闭' if args.no_risk else '开启'} (单笔{args.max_loss*100:.0f}% 日亏{args.max_daily*100:.0f}% 连续{args.consecutive}笔暂停{args.pause}根)")
    print(f"止损截断: 最大{args.max_stop_pct*100:.1f}%，截断后RR={args.alt_rr}")
    print(f"移动止损: {'开启' if args.trailing_stop else '关闭'} (触发{args.trailing_activation_r}R, ATRx{args.trailing_atr_mult})")

    trades, risk = main_strategy_backtest(
        df=m30_df,
        timeframe="30m",
        left=PARAMS["left"],
        right=PARAMS["right"],
        rr=PARAMS["rr"],
        sl_buffer=PARAMS["sl_buffer"],
        fee_rate=args.fee,
        higher_tf_df=h1_df,
        use_ai_filter=not args.no_ai,
        risk_config=risk_config,
        max_stop_pct=args.max_stop_pct,
        alt_rr=args.alt_rr,
        use_trailing_stop=args.trailing_stop,
        trailing_activation_r=args.trailing_activation_r,
        trailing_atr_multiplier=args.trailing_atr_mult,
    )

    if not trades.empty:
        trades.to_csv("results/main_strategy_trades.csv", index=False)
        print(f"交易明细已保存: results/main_strategy_trades.csv")

    stats = analyze_trades_with_capital(trades, risk)
    print_report("分型 + 共振 + AI + 风控 综合策略", stats)

    if not trades.empty:
        print(f"\n风控暂停触发次数: {len([r for r in risk.trade_history if r.get('paused', False)])}")
        print(f"当前连续亏损: {risk.consecutive_losses}")
        print(f"最终资金: {risk.capital:.2f} USDT")

    print("\n[OK] 回测完成")


if __name__ == "__main__":
    main()
