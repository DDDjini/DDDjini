"""
OKX 模拟盘自动交易（GitHub Actions 版）
- 每轮：查持仓 → 拉行情 → 检测信号 → 动态仓位 → 下单
- 全部通过 GitHub Secrets 注入 API Key，只连 Sandbox
"""

import ccxt
import pandas as pd
import os
import sys
import requests
import traceback
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════
# 密钥（从环境变量读取）
# ═══════════════════════════════════════════════════════════════

API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET = os.getenv("OKX_API_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

# ═══════════════════════════════════════════════════════════════
# 策略参数
# ═══════════════════════════════════════════════════════════════

ASSETS = {
    "BTC": {
        "symbol": "BTC/USDT:USDT",
        "max_stop_pct": 0.017,     # 1.7%
    },
    "ETH": {
        "symbol": "ETH/USDT:USDT",
        "max_stop_pts": 50.0,      # 50 点
    },
}

LEFT, RIGHT = 5, 2
RR = 2.0
SL_BUFFER = 0.0005
LEVERAGE = 100
MARGIN_PCT = 0.05          # 5%


# ═══════════════════════════════════════════════════════════════
# 飞书
# ═══════════════════════════════════════════════════════════════

def feishu(title: str, content: str, color: str = "blue"):
    if not FEISHU_WEBHOOK:
        print(f"[飞书] 跳过: {title}")
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
        print(f"  [飞书] {r.status_code} | {title}")
    except Exception as e:
        print(f"  [飞书] 异常: {e}")


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
# OKX API 封装
# ═══════════════════════════════════════════════════════════════

class OKXTrader:
    def __init__(self):
        if not all([API_KEY, API_SECRET, PASSPHRASE]):
            raise RuntimeError("OKX API Key 未配置")

        self.exchange = ccxt.okx({
            "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
            "enableRateLimit": True, "timeout": 15000,
            "options": {"defaultType": "swap"},
        })
        self.exchange.set_sandbox_mode(True)  # 始终模拟盘
        self.exchange.load_markets()

        # 缓存合约信息
        self.contracts = {}
        for name, cfg in ASSETS.items():
            sym = cfg["symbol"]
            mkt = self.exchange.market(sym)
            self.contracts[name] = {
                "ct_val": float(mkt.get("contractSize", 1)),
                "min_qty": float(mkt.get("limits", {}).get("amount", {}).get("min", 0.01)),
            }

    def balance(self):
        try:
            bal = self.exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            return float(usdt.get("free", 0)) or float(usdt.get("total", 0))
        except Exception as e:
            print(f"  余额查询失败: {e}")
            return 0

    def position(self, name):
        sym = ASSETS[name]["symbol"]
        try:
            positions = self.exchange.fetch_positions([sym])
            for p in positions:
                if p.get("symbol") == sym and abs(float(p.get("contracts", 0))) > 0:
                    side = p.get("posSide", p.get("side", "long"))
                    return {
                        "side": side, "contracts": float(p["contracts"]),
                        "entry": float(p.get("entryPrice", 0)),
                    }
            return None
        except Exception as e:
            print(f"  持仓查询失败 [{name}]: {e}")
            return None

    def set_leverage(self, name):
        sym = ASSETS[name]["symbol"]
        inst_id = sym.replace("/", "-").replace(":", "-")
        try:
            self.exchange.set_leverage(LEVERAGE, sym,
                                       params={"instId": inst_id, "lever": str(LEVERAGE), "mgnMode": "cross"})
            print(f"  [{name}] 杠杆: {LEVERAGE}x")
        except Exception as e:
            print(f"  [{name}] 杠杆设置失败: {e}")

    def open(self, name: str, signal: str, entry_price: float,
             sl: float, tp: float, equity: float):
        sym = ASSETS[name]["symbol"]
        ct_val = self.contracts[name]["ct_val"]
        min_qty = self.contracts[name]["min_qty"]
        pos_side = "long" if signal == "long" else "short"
        order_side = "buy" if signal == "long" else "sell"

        # 动态仓位
        margin = equity * MARGIN_PCT
        notional = margin * LEVERAGE
        contracts = max(round(notional / (entry_price * ct_val), 2), min_qty)

        params = {
            "tdMode": "cross", "posSide": pos_side,
            "attachAlgoOrds": [{
                "tpTriggerPx": str(tp), "tpOrdPx": "-1",
                "slTriggerPx": str(sl), "slOrdPx": "-1",
                "sz": str(contracts), "posSide": pos_side,
            }],
        }
        order = self.exchange.create_market_order(sym, order_side, contracts, params)
        print(f"  [{name}] ✅ 开仓 {order_side} {contracts}张 @ {entry_price}")
        return contracts, margin

    def fetch_ohlcv(self, name, tf, limit=100):
        sym = ASSETS[name]["symbol"]
        rows = self.exchange.fetch_ohlcv(sym, tf, limit=limit)
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        return df


# ═══════════════════════════════════════════════════════════════
# 信号检测
# ═══════════════════════════════════════════════════════════════

def detect_signal(name, m30_df, h1_df, cfg):
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None
    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)
    i = n - 1; pivot = i - RIGHT
    if pivot < 0:
        return None

    dir_ = None
    if m30.loc[pivot, "fractal_low"]: dir_ = "long"
    elif m30.loc[pivot, "fractal_high"]: dir_ = "short"
    if dir_ is None: return None

    ts = m30.loc[pivot, "timestamp"]
    sub = h1[h1["timestamp"] <= ts]
    if len(sub) < 5: return None
    if dir_ == "long" and not sub["fractal_low"].any(): return None
    if dir_ == "short" and not sub["fractal_high"].any(): return None

    entry = m30.loc[i, "close"]
    if dir_ == "long":
        sl = m30.loc[pivot, "low"] * (1 - SL_BUFFER)
        risk = entry - sl; if risk <= 0: return None
        max_stop = cfg.get("max_stop_pct")
        if max_stop and risk > entry * max_stop:
            risk = entry * max_stop; sl = entry - risk
        max_pts = cfg.get("max_stop_pts")
        if max_pts and risk > max_pts:
            risk = max_pts; sl = entry - risk
        tp = entry + RR * risk
    else:
        sl = m30.loc[pivot, "high"] * (1 + SL_BUFFER)
        risk = sl - entry; if risk <= 0: return None
        max_stop = cfg.get("max_stop_pct")
        if max_stop and risk > entry * max_stop:
            risk = entry * max_stop; sl = entry + risk
        max_pts = cfg.get("max_stop_pts")
        if max_pts and risk > max_pts:
            risk = max_pts; sl = entry + risk
        tp = entry - RR * risk

    return {
        "asset": name, "signal": dir_,
        "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2),
        "time": str(m30.loc[i, "datetime"]),
    }


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def run_once():
    ts = datetime.now(timezone.utc).strftime('%m-%d %H:%M')
    print(f"[{ts}] 模拟盘交易扫描...")

    try:
        return _run_inner(ts)
    except Exception as e:
        err = traceback.format_exc()
        print(f"[严重错误]\n{err}")
        feishu(f"❌ 脚本崩溃 {ts}", f"```{err[:800]}```", color="red")


def _run_inner(ts):
    try:
        trader = OKXTrader()
    except Exception as e:
        feishu("❌ OKX 初始化失败", f"```{traceback.format_exc()[:400]}```", "red")
        return

    equity = trader.balance()
    print(f"账户余额: {equity:.2f} USDT")

    lines = [f"**余额**: {equity:.2f} USDT"]

    for name, cfg in ASSETS.items():
        print(f"\n── [{name}] ──")

        # 查持仓
        pos = trader.position(name)
        if pos:
            print(f"  已有持仓: {pos['side']} {pos['contracts']}张 @ {pos['entry']}")
            lines.append(f"\n**{name}**: 持仓中 → {pos['side']} {pos['contracts']}张")
            continue

        # 拉K线
        try:
            m30 = trader.fetch_ohlcv(name, "30m", 100)
            h1 = trader.fetch_ohlcv(name, "1h", 50)
        except Exception as e:
            print(f"  数据拉取失败: {e}")
            lines.append(f"\n**{name}**: ❌ 拉取失败")
            continue

        price = m30["close"].iloc[-1]
        print(f"  价格: {price:.2f}")

        # 检测信号
        sig = detect_signal(name, m30, h1, cfg)
        if not sig:
            print(f"  无信号")
            lines.append(f"\n**{name}**: 无信号 ({price:.2f})")
            continue

        print(f"  🔔 {sig['signal'].upper()} @ {sig['entry']}")
        print(f"     SL:{sig['sl']} TP:{sig['tp']}")

        # 设置杠杆
        trader.set_leverage(name)

        # 开仓
        contracts, margin = trader.open(name, sig["signal"], sig["entry"],
                                         sig["sl"], sig["tp"], equity)
        lines.append(
            f"\n**{name}**: 🔔 {sig['signal'].upper()} | 入场={sig['entry']} | "
            f"止损={sig['sl']} | 止盈={sig['tp']} | {contracts}张 | 保证金={margin:.2f}"
        )

    # ── 飞书汇总 ──
    feishu(f"📊 交易扫描 {ts}", "\n".join(lines), color="blue")

    print(f"\n{'='*50}")
    print("[Done]")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        run_once()
    else:
        print("用法: python trader_action.py --once")
