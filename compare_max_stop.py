import pandas as pd
import numpy as np
from datetime import datetime, timezone
from backtest_fractal import backtest_fractal, analyze_trades, add_fractals

def run_comparison(symbol, m30_csv, h1_csv, max_stop_pct, label):
    m30_df = pd.read_csv(m30_csv)
    m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    h1_df = pd.read_csv(h1_csv)
    h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])

    # 无限制
    trades_unlimited = backtest_fractal(
        m30_df, "30m", 5, 2, 2.0, 0.0005, 0.0005, h1_df, max_stop_pct=None
    )
    stats_unlimited = analyze_trades(trades_unlimited)

    # 有限制
    trades_limited = backtest_fractal(
        m30_df, "30m", 5, 2, 2.0, 0.0005, 0.0005, h1_df, max_stop_pct=max_stop_pct
    )
    stats_limited = analyze_trades(trades_limited)

    print(f"\n{'='*70}")
    print(f"  {symbol} - 最大止损限制: {label}")
    print(f"{'='*70}")
    print(f"  {'指标':<20} {'无限制':<20} {'有限制':<20}")
    print(f"  {'-'*60}")
    print(f"  {'交易次数':<20} {stats_unlimited['trades']:<20} {stats_limited['trades']:<20}")
    print(f"  {'胜率':<20} {stats_unlimited['win_rate']*100:.2f}%{'':<14} {stats_limited['win_rate']*100:.2f}%")
    print(f"  {'累计收益':<20} {stats_unlimited['total_return']*100:.2f}%{'':<14} {stats_limited['total_return']*100:.2f}%")
    print(f"  {'盈亏因子':<20} {stats_unlimited['profit_factor']:.3f}{'':<15} {stats_limited['profit_factor']:.3f}")
    print(f"  {'最大回撤':<20} {stats_unlimited['max_drawdown']*100:.2f}%{'':<14} {stats_limited['max_drawdown']*100:.2f}%")
    print(f"  {'平均单笔':<20} {stats_unlimited['avg_return']*100:.4f}%{'':<14} {stats_limited['avg_return']*100:.4f}%")

    return stats_unlimited, stats_limited


if __name__ == "__main__":
    print(f"\n{'#'*70}")
    print("  最大止损限制回测对比")
    print(f"{'#'*70}")
    print(f"  策略: 30m分型 + 1h分型共振 | left=5, right=2, RR=2.0")
    print(f"  BTC 最大止损: 1.7%")
    print(f"  ETH 最大止损: ~1.4% (约50点@3500)")
    print(f"{'#'*70}")

    btc_stats = run_comparison(
        "BTC/USDT:USDT",
        "data/okx_BTC_USDT_USDT_30m.csv",
        "data/okx_BTC_USDT_USDT_1h.csv",
        max_stop_pct=0.017,
        label="1.7%",
    )

    eth_stats = run_comparison(
        "ETH/USDT:USDT",
        "data/okx_ETH_USDT_USDT_30m.csv",
        "data/okx_ETH_USDT_USDT_1h.csv",
        max_stop_pct=0.014,
        label="~1.4% (50点@3500)",
    )

    print(f"\n{'#'*70}")
    print("  总结")
    print(f"{'#'*70}")
