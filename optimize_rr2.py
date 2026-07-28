import pandas as pd
import numpy as np
import itertools
from pathlib import Path
from backtest_fractal import backtest_fractal, analyze_trades, add_fractals, ensure_dir

def main():
    ensure_dir("results")

    # 加载 30m 和 1h 数据
    m30_csv = "data/okx_BTC_USDT_USDT_30m.csv"
    h1_csv = "data/okx_BTC_USDT_USDT_1h.csv"

    if not Path(m30_csv).exists() or not Path(h1_csv).exists():
        print("缺少数据文件，请先运行 backtest_fractal.py 拉取数据")
        return

    print("加载 30m 数据...")
    m30_df = pd.read_csv(m30_csv)
    m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
    m30_df = m30_df.tail(10000).reset_index(drop=True)
    print(f"  截取后: {len(m30_df)} 根 K 线")

    print("加载 1h 数据...")
    h1_df = pd.read_csv(h1_csv)
    h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])
    min_ts = m30_df["timestamp"].min()
    max_ts = m30_df["timestamp"].max()
    h1_df = h1_df[(h1_df["timestamp"] >= min_ts) & (h1_df["timestamp"] <= max_ts)].reset_index(drop=True)
    h1_df = add_fractals(h1_df, left=2, right=2)
    print(f"1h 分型: 低点={h1_df['fractal_low'].sum()}, 高点={h1_df['fractal_high'].sum()}")

    # ====================
    # RR = 2.0 固定，扩大搜索范围
    # ====================
    param_grid = {
        "left": [2, 3, 4, 5, 6],
        "right": [2, 3, 4, 5],
        "sl_buffer": [0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003],
    }
    rr = 2.0
    total_combos = np.prod([len(v) for v in param_grid.values()])
    print(f"\nRR=2.0 专属参数优化，共 {total_combos} 种组合...")

    results = []
    best_by_winrate = None
    best_by_profit = None
    best_by_pf = None

    for idx, (left, right, sl_buffer) in enumerate(itertools.product(*param_grid.values())):
        if (idx + 1) % 10 == 0 or idx == 0:
            print(f"  进度: {idx+1}/{total_combos}")

        trades = backtest_fractal(
            df=m30_df,
            timeframe="30m",
            left=left,
            right=right,
            rr=rr,
            sl_buffer=sl_buffer,
            fee_rate=0.0005,
            higher_tf_df=h1_df,
        )

        stats = analyze_trades(trades)
        if stats["trades"] < 10:
            continue

        row = {
            "left": left,
            "right": right,
            "rr": rr,
            "sl_buffer": sl_buffer,
            **stats,
        }
        results.append(row)

        # 更新最优
        if best_by_winrate is None or stats["win_rate"] > best_by_winrate["win_rate"]:
            best_by_winrate = row.copy()
        if best_by_profit is None or stats["total_return"] > best_by_profit["total_return"]:
            best_by_profit = row.copy()
        if best_by_pf is None or stats["profit_factor"] > best_by_pf["profit_factor"]:
            best_by_pf = row.copy()

    if not results:
        print("没有找到有效参数")
        return

    results_df = pd.DataFrame(results)
    results_df.to_csv("results/optimization_rr2_all.csv", index=False)

    top_winrate = results_df.sort_values("win_rate", ascending=False).head(20)
    top_winrate.to_csv("results/optimization_rr2_top_winrate.csv", index=False)

    top_profit = results_df.sort_values("total_return", ascending=False).head(20)
    top_profit.to_csv("results/optimization_rr2_top_profit.csv", index=False)

    top_pf = results_df.sort_values("profit_factor", ascending=False).head(20)
    top_pf.to_csv("results/optimization_rr2_top_pf.csv", index=False)

    print("\n" + "=" * 60)
    print("RR = 2.0 专属参数优化完成")
    print("=" * 60)

    print("\n【最优胜率参数】")
    print(f"  left={best_by_winrate['left']}, right={best_by_winrate['right']}, sl_buffer={best_by_winrate['sl_buffer']}")
    print(f"  交易次数: {best_by_winrate['trades']}, 胜率: {best_by_winrate['win_rate']*100:.2f}%")
    print(f"  累计收益: {best_by_winrate['total_return']*100:.2f}%, 盈亏因子: {best_by_winrate['profit_factor']:.3f}")
    print(f"  最大回撤: {best_by_winrate['max_drawdown']*100:.2f}%")

    print("\n【最优收益参数】")
    print(f"  left={best_by_profit['left']}, right={best_by_profit['right']}, sl_buffer={best_by_profit['sl_buffer']}")
    print(f"  交易次数: {best_by_profit['trades']}, 胜率: {best_by_profit['win_rate']*100:.2f}%")
    print(f"  累计收益: {best_by_profit['total_return']*100:.2f}%, 盈亏因子: {best_by_profit['profit_factor']:.3f}")
    print(f"  最大回撤: {best_by_profit['max_drawdown']*100:.2f}%")

    print("\n【最优盈亏因子参数】")
    print(f"  left={best_by_pf['left']}, right={best_by_pf['right']}, sl_buffer={best_by_pf['sl_buffer']}")
    print(f"  交易次数: {best_by_pf['trades']}, 胜率: {best_by_pf['win_rate']*100:.2f}%")
    print(f"  累计收益: {best_by_pf['total_return']*100:.2f}%, 盈亏因子: {best_by_pf['profit_factor']:.3f}")
    print(f"  最大回撤: {best_by_pf['max_drawdown']*100:.2f}%")

    print("\n【胜率 Top 10】")
    cols = ["left", "right", "sl_buffer", "trades", "win_rate", "total_return", "profit_factor", "max_drawdown"]
    print(top_winrate[cols].head(10).to_string(index=False))

    print("\n【收益 Top 10】")
    print(top_profit[cols].head(10).to_string(index=False))

    print("\n[OK] 结果已保存到 results/optimization_rr2_*.csv")


if __name__ == "__main__":
    main()
