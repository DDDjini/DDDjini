"""
OKX 信号监控脚本（GitHub Actions headless 版）
- 拉取 BTC + ETH 的 30m/1h K线（公开数据，无需 API Key）
- 检测分型共振信号
- 飞书推送告警
- --once 模式：跑一轮就退出
"""

import ccxt
import pandas as pd
import numpy as np
import os
import sys
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

ASSETS = {
    "BTC": "BTC/USDT:USDT",
    "ETH": "ETH/USDT:USDT",
}

LEFT, RIGHT = 5, 2      # 分型参数
RR = 2.0
SL_BUFFER = 0.0005


# ═══════════════════════════════════════════════════════════════
# 分型计算（内联，避免依赖 backtest_fractal）
# ═══════════════════════════════════════════════════════════════

def add_fractals(df: pd.DataFrame, left: int, right: int) -> pd.DataFrame:
    df = df.copy()
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]
    low_matrix = pd.concat(low_shifts, axis=1)
    high_matrix = pd.concat(high_shifts, axis=1)
    current_low, current_high = df["low"], df["high"]
    df["fractal_low"] = (low_matrix.idxmin(axis=1) == left) & current_low.notna()
    df["fractal_high"] = (high_matrix.idxmax(axis=1) == left) & current_high.notna()
    return df


# ═══════════════════════════════════════════════════════════════
# 飞书通知
# ═══════════════════════════════════════════════════════════════

def feishu_send(title: str, content: str, color: str = "blue"):
    if not FEISHU_WEBHOOK:
        print(f"[飞书] Webhook 未配置，跳过: {title}")
        return
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text", "content": f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"}
                    ]},
                ],
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"  [飞书] ✓ 已推送: {title}")
        else:
            print(f"  [飞书] ✗ 失败: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  [飞书] 异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════

def fetch_ohlcv(exchange, symbol: str, tf: str, limit: int = 100) -> pd.DataFrame:
    """拉取 K 线（公开接口，无需 Key）"""
    rows = exchange.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    if len(df) > 1:
        df = df.iloc[:-1].reset_index(drop=True)  # 丢弃未收盘
    return df


def check_signal(name: str, symbol: str, m30_df: pd.DataFrame, h1_df: pd.DataFrame):
    """检测单个币种的分型共振信号"""
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None

    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)

    i = n - 1
    pivot_idx = i - RIGHT
    if pivot_idx < 0:
        return None

    # 检测分型方向
    signal = None
    if m30.loc[pivot_idx, "fractal_low"]:
        signal = "long"
    elif m30.loc[pivot_idx, "fractal_high"]:
        signal = "short"
    if signal is None:
        return None

    # 1h 共振检查
    current_ts = m30.loc[pivot_idx, "timestamp"]
    h1_subset = h1[h1["timestamp"] <= current_ts]
    if len(h1_subset) < 5:
        return None
    if signal == "long" and not h1_subset["fractal_low"].any():
        return None
    if signal == "short" and not h1_subset["fractal_high"].any():
        return None

    entry_price = m30.loc[i, "close"]
    entry_time = m30.loc[i, "datetime"]

    if signal == "long":
        pivot_low = m30.loc[pivot_idx, "low"]
        sl = pivot_low * (1 - SL_BUFFER)
        risk = entry_price - sl
        if risk <= 0:
            return None
        tp = entry_price + RR * risk
    else:
        pivot_high = m30.loc[pivot_idx, "high"]
        sl = pivot_high * (1 + SL_BUFFER)
        risk = sl - entry_price
        if risk <= 0:
            return None
        tp = entry_price - RR * risk

    return {
        "asset": name, "symbol": symbol, "signal": signal,
        "entry_price": round(entry_price, 2),
        "entry_time": str(entry_time),
        "stop_loss": round(sl, 2),
        "take_profit": round(tp, 2),
        "rr": RR,
    }


def run_once():
    """执行一轮扫描"""
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] 开始扫描...")

    # 初始化 OKX（公开接口）
    exchange = ccxt.okx({"enableRateLimit": True, "timeout": 15000,
                         "options": {"defaultType": "swap"}})

    signals = []

    for name, symbol in ASSETS.items():
        print(f"\n── [{name}] {symbol} ──")
        try:
            m30 = fetch_ohlcv(exchange, symbol, "30m", limit=100)
            h1 = fetch_ohlcv(exchange, symbol, "1h", limit=50)
            print(f"  30m: {len(m30)} 根 | 1h: {len(h1)} 根")
            print(f"  最新价: {m30['close'].iloc[-1]:.2f}")
            print(f"  最新30m: {m30['datetime'].iloc[-1]}")

            sig = check_signal(name, symbol, m30, h1)
            if sig:
                emoji = "📈" if sig["signal"] == "long" else "📉"
                print(f"  {emoji} 发现信号: {sig['signal'].upper()}")
                print(f"     进场: {sig['entry_price']} | 止损: {sig['stop_loss']} | 止盈: {sig['take_profit']}")
                signals.append(sig)
            else:
                print(f"  无信号")
        except Exception as e:
            print(f"  ❌ 错误: {e}")

    # ── 汇总 ──
    print(f"\n{'='*50}")
    print(f"扫描完成: {len(signals)} 个信号")
    if signals:
        for s in signals:
            print(f"  [{s['asset']}] {s['signal'].upper()} @ {s['entry_price']}")
    else:
        print("  本轮无共振信号")

    # ── 飞书推送 ──
    if signals:
        for s in signals:
            emoji = "📈" if s["signal"] == "long" else "📉"
            feishu_send(
                f"{emoji} [{s['asset']}] 信号 - {s['signal'].upper()}",
                f"**币种**: {s['asset']}\n"
                f"**方向**: {s['signal'].upper()}\n"
                f"**进场价**: {s['entry_price']}\n"
                f"**止损**: {s['stop_loss']}\n"
                f"**止盈**: {s['take_profit']}\n"
                f"**盈亏比**: {RR}:1\n"
                f"**K线时间**: {s['entry_time']}",
                color="yellow",
            )
    else:
        # 每 6 小时发一次心跳（可选，避免太频繁）
        now = datetime.now(timezone.utc)
        if now.hour % 6 == 0 and now.minute < 30:
            feishu_send(
                "💤 无交易信号",
                f"**时间**: {now.strftime('%Y-%m-%d %H:%M')} UTC\n"
                f"**BTC + ETH**: 本轮未检测到分型共振信号\n"
                f"**状态**: 监控运行中",
                color="blue",
            )

    print("\n[Done]")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="跑一轮退出")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        print("用法: python monitor.py --once")
        print("  GitHub Actions 中会自动以 --once 模式运行")
        sys.exit(0)
