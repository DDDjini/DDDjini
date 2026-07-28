import ccxt
import pandas as pd
import numpy as np
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone


# =========================
# 1. 策略参数
# =========================

PARAMS = {
    "30m": {
        "left": 2,
        "right": 2,
        "rr": 1.4,
        "sl_buffer": 0.001,
    },
    "1h": {
        "left": 2,
        "right": 2,
        "rr": 1.6,
        "sl_buffer": 0.0015,
    },
    "4h": {
        "left": 3,
        "right": 3,
        "rr": 1.8,
        "sl_buffer": 0.002,
    },
    "1d": {
        "left": 3,
        "right": 3,
        "rr": 2.0,
        "sl_buffer": 0.003,
    },
}


# =========================
# 2. 工具函数
# =========================

def utc_ms(date_str: str) -> int:
    """
    输入格式：2023-01-01
    输出 UTC 毫秒时间戳
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ensure_dir(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def generate_mock_data(timeframe: str, since: str, bars: int = 2000):
    """
    离线模式：基于几何布朗运动生成模拟 BTC 永续合约 K 线数据。
    参数校准到真实 BTC 波动特征（年化波动率 ~80%）。
    """
    np.random.seed(42)

    # 时间周期映射（毫秒）
    tf_ms = {
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    if timeframe not in tf_ms:
        raise ValueError(f"不支持的时间周期: {timeframe}")

    interval_ms = tf_ms[timeframe]
    start_ms = utc_ms(since)

    # 生成价格序列（几何布朗运动）
    # mu ~ 0, sigma 按周期缩放
    annual_sigma = 0.80
    periods_per_year = {
        "30m": 365 * 24 * 2,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
    }
    sigma = annual_sigma / np.sqrt(periods_per_year[timeframe])

    returns = np.random.normal(0, sigma, size=bars)
    prices = 30000 * np.exp(np.cumsum(returns))

    rows = []
    for i in range(bars):
        ts = start_ms + i * interval_ms
        close = prices[i]
        # 模拟每根 K 线内的波动
        intrabar_vol = close * sigma * 0.5
        high = close + abs(np.random.normal(0, intrabar_vol))
        low = close - abs(np.random.normal(0, intrabar_vol))
        open_price = low + np.random.random() * (high - low) if i == 0 else rows[-1][4]
        # 确保 open 在 [low, high] 内
        open_price = max(low, min(high, open_price))
        volume = np.random.exponential(1000) + 500
        rows.append([ts, open_price, high, low, close, volume])

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.astype({
        "timestamp": "int64",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
    })
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# =========================
# 3. 数据获取（在线 / 离线）
# =========================

def fetch_ohlcv(symbol: str, timeframe: str, since: str, exchange_id: str = "binance", limit_per_call: int = 300, proxy: str = None):
    """
    获取历史K线。支持通过 exchange_id 切换交易所。
    默认使用 binance（国内网络通常可达）。
    OKX 需要代理环境：exchange_id="okx"

    proxy 格式: "http://127.0.0.1:7897"
    """

    config = {
        "enableRateLimit": True,
        "timeout": 15000,
        "options": {
            "defaultType": "swap",
        }
    }
    if proxy:
        config["proxies"] = {"http": proxy, "https": proxy}

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class(config)

    since_ms = utc_ms(since)
    all_rows = []

    print(f"\n开始获取数据: {exchange_id.upper()} {symbol} {timeframe} since={since}")

    while True:
        try:
            rows = exchange.fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                since=since_ms,
                limit=limit_per_call
            )
        except Exception as e:
            print("获取数据异常，等待后重试:", e)
            time.sleep(3)
            continue

        if not rows:
            break

        all_rows.extend(rows)

        last_ts = rows[-1][0]
        since_ms = last_ts + 1

        last_time = pd.to_datetime(last_ts, unit="ms")
        print(f"已获取到: {last_time}, 总K线数: {len(all_rows)}")

        # 防止无限请求
        now_ms = int(time.time() * 1000)
        if last_ts >= now_ms - 60_000:
            break

        time.sleep(exchange.rateLimit / 1000)

        # 简单保护，防止一次拉太多
        if len(all_rows) > 20000:
            print("超过20000根K线，停止继续拉取。")
            break

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    df = df.drop_duplicates(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")

    return df


# =========================
# 4. 计算高低点分型
# =========================

def add_fractals(df: pd.DataFrame, left: int, right: int):
    """
    向量化计算高低点分型（性能优化版）
    """
    df = df.copy()

    # 构建 shift 矩阵：每列是 shift 不同偏移量后的 low/high
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]

    low_matrix = pd.concat(low_shifts, axis=1)
    high_matrix = pd.concat(high_shifts, axis=1)

    current_low = df["low"]
    current_high = df["high"]

    # 低点分型：当前是最小值，且唯一
    min_low = low_matrix.min(axis=1)
    count_min = (low_matrix == current_low.values[:, None]).sum(axis=1)
    df["fractal_low"] = (current_low == min_low) & (count_min == 1)

    # 高点分型：当前是最大值，且唯一
    max_high = high_matrix.max(axis=1)
    count_max = (high_matrix == current_high.values[:, None]).sum(axis=1)
    df["fractal_high"] = (current_high == max_high) & (count_max == 1)

    return df


# =========================
# 5. 回测逻辑
# =========================

def backtest_fractal(
    df: pd.DataFrame,
    timeframe: str,
    left: int,
    right: int,
    rr: float,
    sl_buffer: float,
    fee_rate: float = 0.0005,
    higher_tf_df: pd.DataFrame = None,
    max_stop_pct: float = None,
    alt_rr: float = None,
    use_trailing_stop: bool = False,
    trailing_activation_r: float = 1.0,
    trailing_atr_period: int = 14,
    trailing_atr_multiplier: float = 2.0,
):
    """
    回测逻辑：

    低点分型确认后：
        下一根K线开盘做多
        止损 = 分型低点 * (1 - sl_buffer)
        止盈 = entry + rr * risk

    高点分型确认后：
        下一根K线开盘做空
        止损 = 分型高点 * (1 + sl_buffer)
        止盈 = entry - rr * risk

    移动止损（可选）：
        浮盈达到 activation_r 倍风险后启动
        用 ATR * multiplier 作为追踪距离

    同一时间只持有一笔仓位。
    """

    df = add_fractals(df, left, right)

    # 计算ATR（移动止损用）
    if use_trailing_stop and 'atr' not in df.columns:
        tr1 = df['high'] - df['low']
        tr2 = abs(df['high'] - df['close'].shift(1))
        tr3 = abs(df['low'] - df['close'].shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = tr.rolling(trailing_atr_period).mean()

    # 多周期共振：更高周期分型过滤
    if higher_tf_df is not None:
        higher_tf_df = add_fractals(higher_tf_df.copy(), left=2, right=2)
        # 向量化获取分型事件
        mask = higher_tf_df['fractal_low'].values | higher_tf_df['fractal_high'].values
        ts_vals = higher_tf_df['timestamp'].values[mask]
        type_vals = np.where(higher_tf_df['fractal_low'].values[mask], 'low', 'high')
        h1_events = list(zip(ts_vals, type_vals))
        h1_events.sort(key=lambda x: x[0])

        # 向量化构建趋势映射
        ts_array = df['timestamp'].values
        event_ts = np.array([e[0] for e in h1_events])
        event_types = np.array([e[1] for e in h1_events])
        idx = np.searchsorted(event_ts, ts_array, side='right') - 1
        h1_trend = np.where(idx >= 0, event_types[idx], None)
        df['h1_trend'] = h1_trend

    trades = []
    i = left + right + 1

    while i < len(df) - 1:

        # 分型位置
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
            if h1_trend is None or (h1_trend is not None and pd.isna(h1_trend)):
                i += 1
                continue
            if signal == 'long' and h1_trend != 'low':
                i += 1
                continue
            if signal == 'short' and h1_trend != 'high':
                i += 1
                continue

        # 进场在当前K线的下一根开盘价
        entry_idx = i + 1

        if entry_idx >= len(df):
            break

        entry_price = df.loc[entry_idx, "open"]
        entry_time = df.loc[entry_idx, "datetime"]

        if signal == "long":
            pivot_low = df.loc[pivot_idx, "low"]
            stop_loss = pivot_low * (1 - sl_buffer)
            risk = entry_price - stop_loss

            if risk <= 0:
                i += 1
                continue

            # 最大止损限制 + 截断后 RR 调整
            used_rr = rr
            if max_stop_pct is not None:
                max_risk = entry_price * max_stop_pct
                if risk > max_risk:
                    risk = max_risk
                    stop_loss = entry_price - max_risk
                    used_rr = alt_rr if alt_rr is not None else rr

            take_profit = entry_price + used_rr * risk

        else:
            pivot_high = df.loc[pivot_idx, "high"]
            stop_loss = pivot_high * (1 + sl_buffer)
            risk = stop_loss - entry_price

            if risk <= 0:
                i += 1
                continue

            # 最大止损限制 + 截断后 RR 调整
            used_rr = rr
            if max_stop_pct is not None:
                max_risk = entry_price * max_stop_pct
                if risk > max_risk:
                    risk = max_risk
                    stop_loss = entry_price + max_risk
                    used_rr = alt_rr if alt_rr is not None else rr

            take_profit = entry_price - used_rr * risk

        # 开始往后找止盈止损
        exit_idx = None
        exit_price = None
        result = None

        # 移动止损状态
        trailing_activated = False
        current_sl = stop_loss

        j = entry_idx + 1

        while j < len(df):
            high = df.loc[j, "high"]
            low = df.loc[j, "low"]
            close = df.loc[j, "close"]

            # 移动止损更新逻辑
            if use_trailing_stop:
                if signal == "long":
                    unrealized_r = (close - entry_price) / risk
                    if not trailing_activated and unrealized_r >= trailing_activation_r:
                        trailing_activated = True
                    if trailing_activated:
                        # ATR追踪止损
                        atr_val = df.loc[j, 'atr'] if 'atr' in df.columns else risk * 0.5
                        new_sl = close - trailing_atr_multiplier * atr_val
                        if new_sl > current_sl:
                            current_sl = new_sl
                else:  # short
                    unrealized_r = (entry_price - close) / risk
                    if not trailing_activated and unrealized_r >= trailing_activation_r:
                        trailing_activated = True
                    if trailing_activated:
                        atr_val = df.loc[j, 'atr'] if 'atr' in df.columns else risk * 0.5
                        new_sl = close + trailing_atr_multiplier * atr_val
                        if new_sl < current_sl:
                            current_sl = new_sl

            sl_to_use = current_sl if use_trailing_stop else stop_loss

            if signal == "long":
                hit_sl = low <= sl_to_use
                hit_tp = high >= take_profit

                # 如果同一根K线同时碰到止损止盈，保守按止损算
                if hit_sl and hit_tp:
                    exit_idx = j
                    exit_price = sl_to_use
                    result = "loss"
                    break
                elif hit_sl:
                    exit_idx = j
                    exit_price = sl_to_use
                    result = "loss" if use_trailing_stop and trailing_activated else "loss"
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

        # 开仓和平仓各收一次手续费
        net_return = gross_return - fee_rate * 2

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
            "rr": rr,
            "left": left,
            "right": right,
            "sl_buffer": sl_buffer,
            "trailing_used": use_trailing_stop,
            "trailing_activated": trailing_activated if use_trailing_stop else False,
        })

        # 出场之后再找下一笔，避免重叠持仓
        i = exit_idx + 1

    trades_df = pd.DataFrame(trades)

    return trades_df


# =========================
# 6. 统计结果
# =========================

def analyze_trades(trades: pd.DataFrame):
    if trades.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_return": 0,
            "avg_return": 0,
            "profit_factor": 0,
            "max_drawdown": 0,
        }

    trades = trades.copy()
    trades["equity"] = (1 + trades["net_return"]).cumprod()

    total = len(trades)
    wins = len(trades[trades["net_return"] > 0])
    losses = total - wins
    win_rate = wins / total if total > 0 else 0

    total_return = trades["equity"].iloc[-1] - 1
    avg_return = trades["net_return"].mean()

    gross_profit = trades.loc[trades["net_return"] > 0, "net_return"].sum()
    gross_loss = abs(trades.loc[trades["net_return"] < 0, "net_return"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    rolling_max = trades["equity"].cummax()
    drawdown = trades["equity"] / rolling_max - 1
    max_drawdown = drawdown.min()

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_return": total_return,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
    }


def print_report(name: str, stats: dict):
    print("\n" + "=" * 60)
    print(f"回测报告：{name}")
    print("=" * 60)
    print(f"交易次数       : {stats['trades']}")
    print(f"盈利次数       : {stats['wins']}")
    print(f"亏损次数       : {stats['losses']}")
    print(f"胜率           : {stats['win_rate'] * 100:.2f}%")
    print(f"累计收益       : {stats['total_return'] * 100:.2f}%")
    print(f"平均单笔收益   : {stats['avg_return'] * 100:.4f}%")
    print(f"盈亏因子       : {stats['profit_factor']:.3f}")
    print(f"最大回撤       : {stats['max_drawdown'] * 100:.2f}%")


# =========================
# 7. 主程序
# =========================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="使用模拟数据离线回测，无需网络")
    parser.add_argument("--exchange", type=str, default="binance", choices=["binance", "okx", "bybit"])
    parser.add_argument("--symbol", type=str, default="BTC/USDT:USDT")
    parser.add_argument("--since", type=str, default="2023-01-01")
    parser.add_argument("--proxy", type=str, default="http://127.0.0.1:7897", help="HTTP代理地址，默认Clash 7897端口")
    parser.add_argument("--limit", type=int, default=300, help="每次API拉取K线数量，默认300")
    parser.add_argument("--fee", type=float, default=0.0005)
    args = parser.parse_args()

    ensure_dir("data")
    ensure_dir("results")

    all_trades = []

    for timeframe, p in PARAMS.items():
        if args.offline:
            csv_path = f"data/mock_{args.symbol.replace('/', '_').replace(':', '_')}_{timeframe}.csv"
            if Path(csv_path).exists():
                print(f"\n读取本地缓存数据: {csv_path}")
                df = pd.read_csv(csv_path)
                df["datetime"] = pd.to_datetime(df["datetime"])
            else:
                print(f"\n生成模拟数据: {timeframe}")
                df = generate_mock_data(timeframe, args.since, bars=2000)
                df.to_csv(csv_path, index=False)
                print(f"模拟数据已保存: {csv_path}")
        else:
            csv_path = f"data/{args.exchange}_{args.symbol.replace('/', '_').replace(':', '_')}_{timeframe}.csv"
            if Path(csv_path).exists():
                print(f"\n读取本地缓存数据: {csv_path}")
                df = pd.read_csv(csv_path)
                df["datetime"] = pd.to_datetime(df["datetime"])
            else:
                df = fetch_ohlcv(args.symbol, timeframe, args.since, exchange_id=args.exchange, limit_per_call=args.limit, proxy=args.proxy)
                df.to_csv(csv_path, index=False)
                print(f"数据已保存: {csv_path}")

        print(f"\n开始回测 {timeframe}")
        trades = backtest_fractal(
            df=df,
            timeframe=timeframe,
            left=p["left"],
            right=p["right"],
            rr=p["rr"],
            sl_buffer=p["sl_buffer"],
            fee_rate=args.fee,
        )

        if not trades.empty:
            out_path = f"results/trades_{timeframe}.csv"
            trades.to_csv(out_path, index=False)
            print(f"交易明细已保存: {out_path}")

        stats = analyze_trades(trades)
        print_report(f"{args.symbol} {timeframe} 分型回测", stats)

        if not trades.empty:
            all_trades.append(trades)

    # 合并所有周期报告
    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        combined = combined.sort_values("entry_time")
        combined["equity"] = (1 + combined["net_return"]).cumprod()
        combined.to_csv("results/trades_all_timeframes.csv", index=False)
        print("\n")
        print_report(f"{args.symbol} 全周期合并", analyze_trades(combined))

    print("\n[OK] 回测完成，所有结果已保存到 results/ 目录")


if __name__ == "__main__":
    main()
