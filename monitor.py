"""
OKX 信号监控脚本（GitHub Actions headless 版）
- 拉取 BTC + ETH 的 30m/1h K线（公开数据，无需 API Key）
- 检测分型共振信号 → 飞书推送
- --once 模式：跑一轮就退出
"""

import ccxt
import pandas as pd
import os
import sys
import requests
import traceback
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
ASSETS = {"BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT"}
LEFT, RIGHT = 5, 2
RR, SL_BUFFER = 2.0, 0.0005


# ═══════════════════════════════════════════════════════════════
# 飞书
# ═══════════════════════════════════════════════════════════════

def feishu_send(title: str, content: str, color: str = "blue"):
    if not FEISHU_WEBHOOK:
        print(f"[飞书] Webhook 为空，跳过: {title}")
        return
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text",
                         "content": f"🕐 {datetime.now().strftime('%m-%d %H:%M')} UTC"}
                    ]},
                ],
            },
        }
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        print(f"  [飞书] 状态={r.status_code} | {title}")
    except Exception as e:
        print(f"  [飞书] 发送异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 分型
# ═══════════════════════════════════════════════════════════════

def add_fractals(df, left, right):
    df = df.copy()
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]
    lm = pd.concat(low_shifts, axis=1)
    hm = pd.concat(high_shifts, axis=1)
    df["fractal_low"] = (lm.idxmin(axis=1) == left) & df["low"].notna()
    df["fractal_high"] = (hm.idxmax(axis=1) == left) & df["high"].notna()
    return df


# ═══════════════════════════════════════════════════════════════
# 核心
# ═══════════════════════════════════════════════════════════════

def fetch_ohlcv(exchange, symbol, tf, limit=100):
    rows = exchange.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    if len(df) > 1:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def check_signal(name, symbol, m30_df, h1_df):
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None
    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)
    i = n - 1
    pivot_idx = i - RIGHT
    if pivot_idx < 0:
        return None
    signal = None
    if m30.loc[pivot_idx, "fractal_low"]:
        signal = "long"
    elif m30.loc[pivot_idx, "fractal_high"]:
        signal = "short"
    if signal is None:
        return None
    current_ts = m30.loc[pivot_idx, "timestamp"]
    h1_subset = h1[h1["timestamp"] <= current_ts]
    if len(h1_subset) < 5:
        return None
    if signal == "long" and not h1_subset["fractal_low"].any():
        return None
    if signal == "short" and not h1_subset["fractal_high"].any():
        return None
    entry_price = m30.loc[i, "close"]
    if signal == "long":
        sl = m30.loc[pivot_idx, "low"] * (1 - SL_BUFFER)
        risk = entry_price - sl
        if risk <= 0: return None
        tp = entry_price + RR * risk
    else:
        sl = m30.loc[pivot_idx, "high"] * (1 + SL_BUFFER)
        risk = sl - entry_price
        if risk <= 0: return None
        tp = entry_price - RR * risk
    return {
        "asset": name, "symbol": symbol, "signal": signal,
        "entry_price": round(entry_price,2),
        "entry_time": str(m30.loc[i,"datetime"]),
        "stop_loss": round(sl,2), "take_profit": round(tp,2), "rr": RR,
    }


def run_once():
    """跑一轮"""
    now = datetime.now(timezone.utc)
    ts = now.strftime('%m-%d %H:%M')
    print(f"[{ts}] 开始扫描...")

    # ── 启动心跳（每次运行都发，证明 Actions 活着）──
    feishu_send(
        f"🟢 监控心跳 {ts}",
        f"**BTC**: {ASSETS['BTC']}\n**ETH**: {ASSETS['ETH']}\n**频率**: 5min",
        color="blue",
    )

    # ── 初始化 ──
    try:
        exchange = ccxt.okx({"enableRateLimit": True, "timeout": 15000,
                             "options": {"defaultType": "swap"}})
    except Exception as e:
        feishu_send("❌ ccxt 初始化失败", f"```{traceback.format_exc()[:500]}```", "red")
        return

    # ── 逐币种扫描 ──
    signals = []
    errors = []

    for name, symbol in ASSETS.items():
        print(f"\n── [{name}] ──")
        try:
            m30 = fetch_ohlcv(exchange, symbol, "30m", 100)
            h1 = fetch_ohlcv(exchange, symbol, "1h", 50)
            price = m30["close"].iloc[-1]
            print(f"  价格: {price:.2f} | 30m:{len(m30)}根 | 1h:{len(h1)}根")

            sig = check_signal(name, symbol, m30, h1)
            if sig:
                print(f"  🔔 {sig['signal'].upper()} @ {sig['entry_price']}")
                signals.append(sig)
            else:
                print(f"  无信号")
        except Exception as e:
            err = f"[{name}] {e}"
            print(f"  ❌ {err}")
            errors.append(err)

    # ── 飞书推送信号 ──
    if signals:
        for s in signals:
            emoji = "📈" if s["signal"]=="long" else "📉"
            feishu_send(
                f"{emoji} [{s['asset']}] 信号 - {s['signal'].upper()}",
                f"**进场**: {s['entry_price']}\n**止损**: {s['stop_loss']}\n"
                f"**止盈**: {s['take_profit']}\n**RR**: {RR}:1\n**K线**: {s['entry_time']}",
                color="yellow",
            )

    # ── 错误推送 ──
    if errors:
        feishu_send(
            "⚠️ 扫描异常",
            f"**时间**: {ts}\n" + "\n".join(f"- {e}" for e in errors),
            color="red",
        )

    # ── 汇总 ──
    summary = f"信号:{len(signals)} | 异常:{len(errors)}"
    print(f"\n{'='*50}")
    print(f"扫描完成 ─ {summary}")
    for s in signals:
        print(f"  [{s['asset']}] {s['signal'].upper()} @ {s['entry_price']}")
    print("[Done]")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        run_once()
    else:
        print("用法: python monitor.py --once")
