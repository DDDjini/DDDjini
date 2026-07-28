"""
多品种分散配置回测 v2
改进点：
1. 止损截断 + 动态RR
2. PSY心理线过滤（移除RSI）
3. 移动止损
4. 多品种分散（BTC + ETH）
5. 新闻事件过滤
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

from main_strategy import main_strategy_backtest, analyze_trades_with_capital
from risk_control import RiskController


def run_single_asset(symbol, m30_csv, h1_csv, initial_capital, risk_config,
                    max_stop_pct, alt_rr, use_trailing_stop,
                    trailing_activation_r, trailing_atr_multiplier,
                    news_events=None):
    """单品种回测"""
    print(f"\n加载 {symbol} 数据...")
    m30_df = pd.read_csv(m30_csv)
    m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    h1_df = pd.read_csv(h1_csv)
    h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])

    # 每个品种独立资金
    rc = risk_config.copy()
    rc["initial_capital"] = initial_capital

    trades, risk = main_strategy_backtest(
        df=m30_df,
        timeframe="30m",
        left=5,
        right=2,
        rr=2.0,
        sl_buffer=0.0005,
        fee_rate=0.0005,
        higher_tf_df=h1_df,
        use_ai_filter=True,
        risk_config=rc,
        max_stop_pct=max_stop_pct,
        alt_rr=alt_rr,
        use_trailing_stop=use_trailing_stop,
        trailing_activation_r=trailing_activation_r,
        trailing_atr_multiplier=trailing_atr_multiplier,
        news_events=news_events,
    )

    stats = analyze_trades_with_capital(trades, risk)
    stats["symbol"] = symbol
    return trades, stats


def merge_portfolio(all_trades_dict, total_initial):
    """
    合并多品种资金曲线，计算组合表现
    假设：等权分配初始资金
    """
    n_assets = len(all_trades_dict)
    per_asset_capital = total_initial / n_assets

    # 收集所有交易，按时间排序
    all_trades = []
    for symbol, trades in all_trades_dict.items():
        if trades.empty:
            continue
        t = trades.copy()
        t["symbol"] = symbol
        t["weight"] = 1.0 / n_assets
        # 按比例缩放收益
        t["port_return"] = t["net_return"] * (1.0 / n_assets)
        all_trades.append(t)

    if not all_trades:
        return None, None

    combined = pd.concat(all_trades, ignore_index=True)
    combined = combined.sort_values("entry_time").reset_index(drop=True)

    # 计算组合资金曲线
    combined["port_equity"] = (1 + combined["port_return"]).cumprod()
    combined["capital"] = total_initial * combined["port_equity"]

    # 统计指标
    total_trades = len(combined)
    wins = len(combined[combined["net_return"] > 0])
    win_rate = wins / total_trades if total_trades > 0 else 0

    total_return = combined["port_equity"].iloc[-1] - 1
    avg_return = combined["port_return"].mean()

    gross_profit = combined.loc[combined["port_return"] > 0, "port_return"].sum()
    gross_loss = abs(combined.loc[combined["port_return"] < 0, "port_return"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    rolling_max = combined["port_equity"].cummax()
    drawdown = combined["port_equity"] / rolling_max - 1
    max_drawdown = drawdown.min()

    portfolio_stats = {
        "total_trades": total_trades,
        "wins": wins,
        "losses": total_trades - wins,
        "win_rate": win_rate,
        "total_return": total_return,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "final_capital": total_initial * (1 + total_return),
        "initial_capital": total_initial,
        "n_assets": n_assets,
    }

    return combined, portfolio_stats


def print_single_stats(stats):
    print(f"  {stats['symbol']}:")
    print(f"    交易数: {stats['trades']}, 胜率: {stats['win_rate']*100:.2f}%")
    print(f"    收益: {stats['total_return']*100:.2f}%, 盈亏比: {stats['profit_factor']:.3f}")
    print(f"    最大回撤: {stats['max_drawdown']*100:.2f}%")
    print(f"    最终资金: {stats['final_capital']:.2f} USDT")


def print_portfolio_stats(stats):
    print(f"\n{'='*60}")
    print(f"  组合表现（{stats['n_assets']}品种等权分散）")
    print(f"{'='*60}")
    print(f"  总交易数    : {stats['total_trades']}")
    print(f"  胜率        : {stats['win_rate']*100:.2f}%")
    print(f"  累计收益    : {stats['total_return']*100:.2f}%")
    print(f"  盈亏因子    : {stats['profit_factor']:.3f}")
    print(f"  最大回撤    : {stats['max_drawdown']*100:.2f}%")
    print(f"  初始资金    : {stats['initial_capital']:.2f} USDT")
    print(f"  最终资金    : {stats['final_capital']:.2f} USDT")


def main():
    print("=" * 70)
    print("  多品种分散配置回测 v2 (改进版)")
    print("=" * 70)
    print("  策略: 30m分型 + 1h共振 + PSY过滤 + 止损截断 + 移动止损")
    print("  品种: BTC + ETH 等权配置")
    print("=" * 70)

    # 配置
    total_capital = 20000.0  # 总资金
    per_asset = total_capital / 2

    risk_config = {
        "max_loss_per_trade": 0.02,
        "max_daily_loss": 0.05,
        "max_consecutive_losses": 3,
        "pause_after_losses": 5,
        "max_drawdown_limit": 0.30,
    }

    # 改进参数
    max_stop_pct = 0.017  # BTC 1.7%, ETH 1.4%
    alt_rr = 1.0
    use_trailing_stop = True
    trailing_activation_r = 1.0
    trailing_atr_multiplier = 2.0

    print(f"\n配置:")
    print(f"  总资金: {total_capital} USDT (每品种 {per_asset} USDT)")
    print(f"  止损截断: {max_stop_pct*100:.1f}%, 截断后RR={alt_rr}")
    print(f"  移动止损: 触发{trailing_activation_r}R, ATRx{trailing_atr_multiplier}")
    print(f"  AI过滤: PSY心理线 + MACD + 布林带 + ATR")

    # 单品种回测
    assets = [
        ("BTC/USDT:USDT", "data/okx_BTC_USDT_USDT_30m.csv", "data/okx_BTC_USDT_USDT_1h.csv", 0.017),
        ("ETH/USDT:USDT", "data/okx_ETH_USDT_USDT_30m.csv", "data/okx_ETH_USDT_USDT_1h.csv", 0.014),
    ]

    all_trades_dict = {}
    all_stats = []

    print(f"\n{'='*60}")
    print("  单品种表现")
    print(f"{'='*60}")

    for symbol, m30_csv, h1_csv, msp in assets:
        trades, stats = run_single_asset(
            symbol, m30_csv, h1_csv, per_asset, risk_config,
            msp, alt_rr, use_trailing_stop,
            trailing_activation_r, trailing_atr_multiplier
        )
        all_trades_dict[symbol] = trades
        all_stats.append(stats)
        print_single_stats(stats)

    # 组合表现
    combined, port_stats = merge_portfolio(all_trades_dict, total_capital)
    print_portfolio_stats(port_stats)

    # 保存结果
    combined.to_csv("results/multi_asset_v2_trades.csv", index=False)
    pd.DataFrame(all_stats).to_csv("results/multi_asset_v2_single_stats.csv", index=False)

    print(f"\n结果已保存:")
    print(f"  results/multi_asset_v2_trades.csv - 组合交易明细")
    print(f"  results/multi_asset_v2_single_stats.csv - 单品种统计")

    print("\n[OK] 多品种回测完成")


if __name__ == "__main__":
    main()
