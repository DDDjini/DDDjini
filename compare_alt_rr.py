import pandas as pd
import numpy as np
from backtest_fractal import backtest_fractal, analyze_trades, add_fractals

def run_alt_rr(symbol, m30_csv, h1_csv, max_stop_pct, label):
    m30_df = pd.read_csv(m30_csv)
    m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    h1_df = pd.read_csv(h1_csv)
    h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])

    configs = [
        ("无截断 RR=2.0", None, None),
        ("截断 RR=1.0", max_stop_pct, 1.0),
        ("截断 RR=1.3", max_stop_pct, 1.3),
        ("截断 RR=1.5", max_stop_pct, 1.5),
        ("截断 RR=2.0", max_stop_pct, 2.0),
    ]

    results = []
    for name, msp, alt_rr in configs:
        trades = backtest_fractal(
            m30_df, "30m", 5, 2, 2.0, 0.0005, 0.0005, h1_df, msp, alt_rr
        )
        stats = analyze_trades(trades)
        stats["config"] = name
        stats["symbol"] = symbol
        results.append(stats)
        print(f"  {name:18s}: 交易{stats['trades']:4d} | 胜率{stats['win_rate']*100:5.1f}% | 收益{stats['total_return']*100:8.2f}% | 盈亏因子{stats['profit_factor']:.3f} | 回撤{stats['max_drawdown']*100:6.2f}%")
    return results


if __name__ == "__main__":
    print("="*80)
    print("  截断止损后不同 RR 回测对比")
    print("  策略: 30m分型 + 1h分型共振 | left=5, right=2")
    print("  正常情况 RR=2.0, 截断后分别用 RR=1.0/1.3/1.5/2.0")
    print("="*80)

    all_results = []

    print(f"\n{'='*80}")
    print("  BTC/USDT:USDT - 最大止损 1.7%")
    print(f"{'='*80}")
    all_results.extend(run_alt_rr("BTC/USDT:USDT",
        "data/okx_BTC_USDT_USDT_30m.csv",
        "data/okx_BTC_USDT_USDT_1h.csv",
        0.017, "1.7%"))

    print(f"\n{'='*80}")
    print("  ETH/USDT:USDT - 最大止损 1.4%")
    print(f"{'='*80}")
    all_results.extend(run_alt_rr("ETH/USDT:USDT",
        "data/okx_ETH_USDT_USDT_30m.csv",
        "data/okx_ETH_USDT_USDT_1h.csv",
        0.014, "1.4%"))

    df = pd.DataFrame(all_results)
    df.to_csv("results/alt_rr_comparison.csv", index=False)
    print(f"\n结果已保存: results/alt_rr_comparison.csv")

    print(f"\n{'='*80}")
    print("  汇总")
    print(f"{'='*80}")
    for symbol in df["symbol"].unique():
        subset = df[df["symbol"] == symbol].sort_values("total_return", ascending=False)
        print(f"\n  {symbol} - 按收益排序:")
        for _, row in subset.iterrows():
            print(f"    {row['config']:18s}: 收益{row['total_return']*100:8.2f}% | 胜率{row['win_rate']*100:5.1f}% | 回撤{row['max_drawdown']*100:6.2f}%")
