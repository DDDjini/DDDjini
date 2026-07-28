import pandas as pd
import numpy as np
import argparse
from pathlib import Path
from datetime import datetime, timezone

from backtest_fractal import (
    add_fractals, fetch_ohlcv, ensure_dir, utc_ms
)


# =========================
# RSI 和 PSY 指标
# =========================

def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    df[f"rsi_{period}"] = 100 - (100 / (1 + rs))
    return df


def add_psy(df: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    """
    PSY(心理线) = N日内上涨天数 / N * 100
    上涨: close > previous close
    """
    up = (df["close"] > df["close"].shift(1)).astype(int)
    df[f"psy_{period}"] = up.rolling(period).mean() * 100
    return df


# =========================
# 多币种分型策略回测（带 RSI/PSY 过滤）
# =========================

def backtest_multi_asset(
    symbol: str,
    m30_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    left: int = 5,
    right: int = 2,
    rr: float = 2.0,
    sl_buffer: float = 0.0005,
    fee_rate: float = 0.0005,
    use_rsi: bool = False,
    use_psy: bool = False,
    rsi_period: int = 14,
    psy_period: int = 12,
    rsi_long_max: float = 40,   # 做多时 RSI 必须 < 40
    rsi_short_min: float = 60,  # 做空时 RSI 必须 > 60
    psy_long_max: float = 40,   # 做多时 PSY 必须 < 40
    psy_short_min: float = 60,  # 做空时 PSY 必须 > 60
):
    """
    分型 + 多周期共振 + RSI/PSY 过滤回测
    """
    m30_df = m30_df.copy()
    h1_df = h1_df.copy()

    # 计算指标
    if use_rsi:
        m30_df = add_rsi(m30_df, rsi_period)
    if use_psy:
        m30_df = add_psy(m30_df, psy_period)

    # 计算分型
    m30_df = add_fractals(m30_df, left, right)
    h1_df = add_fractals(h1_df, 2, 2)

    trades = []
    n = len(m30_df)

    for i in range(left + right + 1, n - 1):
        pivot_idx = i - right
        if pivot_idx < 0:
            continue

        signal = None
        if m30_df.loc[pivot_idx, "fractal_low"]:
            signal = "long"
        elif m30_df.loc[pivot_idx, "fractal_high"]:
            signal = "short"

        if signal is None:
            continue

        # 多周期共振
        current_ts = m30_df.loc[pivot_idx, "timestamp"]
        h1_subset = h1_df[h1_df["timestamp"] <= current_ts]
        if len(h1_subset) < 5:
            continue

        if signal == "long":
            if not h1_subset["fractal_low"].any():
                continue
        else:
            if not h1_subset["fractal_high"].any():
                continue

        # RSI 过滤
        if use_rsi:
            rsi_col = f"rsi_{rsi_period}"
            if rsi_col not in m30_df.columns:
                continue
            rsi_val = m30_df.loc[pivot_idx, rsi_col]
            if pd.isna(rsi_val):
                continue
            if signal == "long" and rsi_val > rsi_long_max:
                continue
            if signal == "short" and rsi_val < rsi_short_min:
                continue

        # PSY 过滤
        if use_psy:
            psy_col = f"psy_{psy_period}"
            if psy_col not in m30_df.columns:
                continue
            psy_val = m30_df.loc[pivot_idx, psy_col]
            if pd.isna(psy_val):
                continue
            if signal == "long" and psy_val > psy_long_max:
                continue
            if signal == "short" and psy_val < psy_short_min:
                continue

        # 进场
        entry_idx = i + 1
        if entry_idx >= n:
            continue

        entry_price = m30_df.loc[entry_idx, "open"]

        if signal == "long":
            pivot_low = m30_df.loc[pivot_idx, "low"]
            sl = pivot_low * (1 - sl_buffer)
            risk = entry_price - sl
            if risk <= 0:
                continue
            tp = entry_price + rr * risk
        else:
            pivot_high = m30_df.loc[pivot_idx, "high"]
            sl = pivot_high * (1 + sl_buffer)
            risk = sl - entry_price
            if risk <= 0:
                continue
            tp = entry_price - rr * risk

        # 找出场
        exit_idx = None
        exit_price = None
        result = None

        for j in range(entry_idx + 1, n):
            high = m30_df.loc[j, "high"]
            low = m30_df.loc[j, "low"]

            if signal == "long":
                if low <= sl and high >= tp:
                    exit_idx = j
                    exit_price = sl
                    result = "loss"
                    break
                elif low <= sl:
                    exit_idx = j
                    exit_price = sl
                    result = "loss"
                    break
                elif high >= tp:
                    exit_idx = j
                    exit_price = tp
                    result = "win"
                    break
            else:
                if high >= sl and low <= tp:
                    exit_idx = j
                    exit_price = sl
                    result = "loss"
                    break
                elif high >= sl:
                    exit_idx = j
                    exit_price = sl
                    result = "loss"
                    break
                elif low <= tp:
                    exit_idx = j
                    exit_price = tp
                    result = "win"
                    break

        if exit_idx is None:
            continue

        if signal == "long":
            gross_ret = (exit_price - entry_price) / entry_price
        else:
            gross_ret = (entry_price - exit_price) / entry_price

        net_ret = gross_ret - fee_rate * 2

        trades.append({
            "symbol": symbol,
            "side": signal,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "result": result,
            "net_return": net_ret,
            "rr": rr,
        })

    return pd.DataFrame(trades)


def analyze(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_return": 0, "profit_factor": 0}

    total = len(trades)
    wins = len(trades[trades["net_return"] > 0])
    losses = total - wins
    win_rate = wins / total if total > 0 else 0
    total_return = (1 + trades["net_return"]).cumprod().iloc[-1] - 1

    gross_profit = trades.loc[trades["net_return"] > 0, "net_return"].sum()
    gross_loss = abs(trades.loc[trades["net_return"] < 0, "net_return"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_return": total_return,
        "profit_factor": profit_factor,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2025-01-01")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = parser.parse_args()

    ensure_dir("data")
    ensure_dir("results")

    symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    configs = [
        {"name": "基准(分型+共振)", "use_rsi": False, "use_psy": False},
        {"name": "+RSI过滤", "use_rsi": True, "use_psy": False, "rsi_long_max": 40, "rsi_short_min": 60},
        {"name": "+PSY过滤", "use_rsi": False, "use_psy": True, "psy_long_max": 40, "psy_short_min": 60},
        {"name": "+RSI+PSY", "use_rsi": True, "use_psy": True, "rsi_long_max": 40, "rsi_short_min": 60, "psy_long_max": 40, "psy_short_min": 60},
    ]

    results = []

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"交易对: {symbol}")
        print(f"{'='*60}")

        # 获取数据
        m30_csv = f"data/okx_{symbol.replace('/', '_').replace(':', '_')}_30m.csv"
        h1_csv = f"data/okx_{symbol.replace('/', '_').replace(':', '_')}_1h.csv"

        if Path(m30_csv).exists():
            m30_df = pd.read_csv(m30_csv)
            m30_df["datetime"] = pd.to_datetime(m30_df["datetime"])
        else:
            m30_df = fetch_ohlcv(symbol, "30m", args.since, exchange_id="okx", proxy=args.proxy)
            m30_df.to_csv(m30_csv, index=False)

        if Path(h1_csv).exists():
            h1_df = pd.read_csv(h1_csv)
            h1_df["datetime"] = pd.to_datetime(h1_df["datetime"])
        else:
            h1_df = fetch_ohlcv(symbol, "1h", args.since, exchange_id="okx", proxy=args.proxy)
            h1_df.to_csv(h1_csv, index=False)

        for cfg in configs:
            trades = backtest_multi_asset(
                symbol=symbol,
                m30_df=m30_df,
                h1_df=h1_df,
                left=5, right=2, rr=2.0, sl_buffer=0.0005,
                **{k: v for k, v in cfg.items() if k != "name"}
            )
            stats = analyze(trades)
            stats["symbol"] = symbol
            stats["config"] = cfg["name"]
            results.append(stats)

            print(f"  {cfg['name']:15s}: 交易{stats['trades']:4d} | 胜率{stats['win_rate']*100:5.1f}% | 收益{stats['total_return']*100:7.2f}% | 盈亏因子{stats['profit_factor']:.3f}")

    # 汇总保存
    df = pd.DataFrame(results)
    df.to_csv("results/multi_asset_comparison.csv", index=False)
    print(f"\n结果已保存: results/multi_asset_comparison.csv")

    print("\n" + "="*60)
    print("汇总对比")
    print("="*60)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
