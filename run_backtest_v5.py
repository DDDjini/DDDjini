"""
用 OKX 真实 4h/2h 数据佐证回测结果
对比：聚合数据(resample) vs 真实API数据
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from run_backtest_v4 import run_backtest, compute_stats, fmt, resample_ohlcv, ASSETS


def main():
    print("=" * 90)
    print("用 OKX 真实 4h/2h 数据佐证回测结果")
    print("对比：30m聚合(resample) vs 真实API调取")
    print("=" * 90)

    for name, cfg in ASSETS.items():
        print(f"\n{'─'*90}")
        print(f"  【{name}】")
        print(f"{'─'*90}")

        m30 = pd.read_csv(cfg["file_30m"])
        m30["datetime"] = pd.to_datetime(m30["datetime"])
        h1 = pd.read_csv(cfg["file_1h"])
        h1["datetime"] = pd.to_datetime(h1["datetime"])

        # 真实 4h/2h 数据
        base = "bt_BTC_USDT_USDT" if name == "BTC" else "bt_ETH_USDT_USDT"
        real_4h = pd.read_csv(f"data/{base}_4h_2025-08-12.csv")
        real_4h["datetime"] = pd.to_datetime(real_4h["datetime"])
        real_2h = pd.read_csv(f"data/{base}_2h_2025-08-12.csv")
        real_2h["datetime"] = pd.to_datetime(real_2h["datetime"])

        # 聚合 4h/2h
        agg_4h = resample_ohlcv(m30, "4h")
        agg_2h = resample_ohlcv(m30, "2h")

        print(f"  真实4h={len(real_4h)}根 聚合4h={len(agg_4h)}根 | "
              f"真实2h={len(real_2h)}根 聚合2h={len(agg_2h)}根")

        # 数据一致性检查：对比真实和聚合的 close
        # 对齐到共同的 timestamp
        merged_4h = pd.merge(
            real_4h[["timestamp", "close"]].rename(columns={"close": "real_close"}),
            agg_4h[["timestamp", "close"]].rename(columns={"close": "agg_close"}),
            on="timestamp", how="inner"
        )
        if len(merged_4h) > 0:
            diff = (merged_4h["real_close"] - merged_4h["agg_close"]).abs()
            print(f"  4h close 差异: 匹配{len(merged_4h)}根, 最大差{diff.max():.2f}, 平均差{diff.mean():.4f}")

        configs = [
            ("基线(无额外共振)", None, "loose", None),
            ("+4h严格[真实API]", real_4h, "loose", "strict"),
            ("+4h严格[30m聚合]", agg_4h, "loose", "strict"),
            ("+2h严格[真实API]", real_2h, "loose", "strict"),
            ("+2h严格[30m聚合]", agg_2h, "loose", "strict"),
        ]

        print(f"\n  {'方案':<22}{'交易':>6}{'胜率':>9}{'累计收益':>18}{'盈亏因子':>10}{'最大回撤':>11}")
        print("  " + "-" * 80)

        for label, extra, h1_mode, extra_mode in configs:
            trades = run_backtest(m30, h1, cfg, h1_mode=h1_mode, extra_df=extra, extra_mode=extra_mode)
            s = compute_stats(trades)
            print(f"  {label:<22}{s['trades']:>6}{s['win_rate']*100:>8.1f}%"
                  f"{fmt(s['total_return']*100):>17}{s['profit_factor']:>10.3f}"
                  f"{s['max_drawdown']*100:>10.2f}%")

    print("\n" + "=" * 90)
    print("佐证完成")
    print("=" * 90)


if __name__ == "__main__":
    main()
