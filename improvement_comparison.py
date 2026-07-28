"""
策略改进前后对比回测
对比维度：
- 原版 (分型+共振+RSI过滤+风控)
- 改进版 (止损截断+PSY过滤+移动止损)
"""

import pandas as pd
import numpy as np
from pathlib import Path

from backtest_fractal import backtest_fractal, analyze_trades, add_fractals


def run_comparison(symbol, m30_csv, h1_csv):
    print(f"\n{'='*70}")
    print(f"  {symbol} - 改进前后对比")
    print(f"{'='*70}")

    m30_df = pd.read_csv(m30_csv)
    m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    h1_df = pd.read_csv(h1_csv)
    h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])

    configs = [
        ("原版: 基础分型+共振", {
            "left": 5, "right": 2, "rr": 2.0, "sl_buffer": 0.0005,
            "higher_tf_df": h1_df,
            "max_stop_pct": None, "alt_rr": None,
            "use_trailing_stop": False,
        }),
        ("+ 止损截断 (1.7%/1.4%, RR=1.0)", {
            "left": 5, "right": 2, "rr": 2.0, "sl_buffer": 0.0005,
            "higher_tf_df": h1_df,
            "max_stop_pct": 0.017 if "BTC" in symbol else 0.014,
            "alt_rr": 1.0,
            "use_trailing_stop": False,
        }),
        ("+ 移动止损 (1R触发, ATRx2)", {
            "left": 5, "right": 2, "rr": 2.0, "sl_buffer": 0.0005,
            "higher_tf_df": h1_df,
            "max_stop_pct": 0.017 if "BTC" in symbol else 0.014,
            "alt_rr": 1.0,
            "use_trailing_stop": True,
            "trailing_activation_r": 1.0,
            "trailing_atr_multiplier": 2.0,
        }),
    ]

    results = []
    for name, params in configs:
        trades = backtest_fractal(
            df=m30_df,
            timeframe="30m",
            fee_rate=0.0005,
            **params
        )
        stats = analyze_trades(trades)
        stats["config"] = name
        stats["symbol"] = symbol
        results.append(stats)

    # 打印对比
    print(f"\n  {'配置':<40} {'交易数':>6} {'胜率':>8} {'收益':>10} {'盈亏比':>8} {'回撤':>10}")
    print(f"  {'-'*80}")
    for r in results:
        print(f"  {r['config']:<40} {r['trades']:>6} {r['win_rate']*100:>7.2f}% {r['total_return']*100:>9.2f}% {r['profit_factor']:>8.3f} {r['max_drawdown']*100:>9.2f}%")

    return results


def main():
    print("#" * 70)
    print("  策略改进效果对比回测")
    print("#" * 70)
    print("  基础: left=5, right=2, RR=2.0, 30m+1h共振")
    print("  改进1: 止损截断 + 动态RR")
    print("  改进2: + 移动止损 (ATR追踪)")
    print("#" * 70)

    all_results = []

    # BTC
    btc_results = run_comparison(
        "BTC/USDT:USDT",
        "data/okx_BTC_USDT_USDT_30m.csv",
        "data/okx_BTC_USDT_USDT_1h.csv"
    )
    all_results.extend(btc_results)

    # ETH
    eth_results = run_comparison(
        "ETH/USDT:USDT",
        "data/okx_ETH_USDT_USDT_30m.csv",
        "data/okx_ETH_USDT_USDT_1h.csv"
    )
    all_results.extend(eth_results)

    # 保存
    df = pd.DataFrame(all_results)
    df.to_csv("results/improvement_comparison.csv", index=False)
    print(f"\n结果已保存: results/improvement_comparison.csv")

    print(f"\n{'#'*70}")
    print("  改进总结")
    print(f"{'#'*70}")
    for symbol in df["symbol"].unique():
        sub = df[df["symbol"] == symbol].reset_index(drop=True)
        base = sub.iloc[0]["total_return"]
        best = sub["total_return"].max()
        improvement = (best - base) / abs(base) * 100 if base != 0 else float('inf')
        print(f"\n  {symbol}:")
        print(f"    原版收益: {base*100:.2f}%")
        print(f"    最优收益: {best*100:.2f}%")
        print(f"    提升幅度: {improvement:.1f}%")


if __name__ == "__main__":
    main()
