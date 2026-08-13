"""
OKX 实盘自动交易机器人（本地 VSCode 运行版）
=============================================
策略: 分型(5,2) + 1h宽松共振 + 4h严格共振 + RR=1:1 + 100x杠杆
仓位: 头仓3%保证金，浮亏40%(入场到止损2/5处)时加仓4%，总止损不变
调度: 每5分钟扫描一次，30分钟收盘节点执行完整信号检测
风控: 分型去重 + 持仓检查
通知: 飞书机器人实时推送

启动方式:
  python live_trader.py           # 持续运行
  python live_trader.py --once    # 单次扫描
  python live_trader.py --status  # 仅查看状态
  python live_trader.py --reset   # 重置持仓状态

依赖: ccxt, pandas, requests, python-dotenv
"""

import ccxt
import pandas as pd
import numpy as np
import os
import sys
import json
import time
import signal
import traceback
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# ═══════════════════════════════════════════════
# 密钥 & 配置
# ═══════════════════════════════════════════════

API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET = os.getenv("OKX_API_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

LEVERAGE = int(os.getenv("LEVERAGE", "100"))
MARGIN_PCT = float(os.getenv("MARGIN_PCT", "0.03"))       # 头仓保证金 3%
ADD_MARGIN_PCT = float(os.getenv("ADD_MARGIN_PCT", "0.04"))  # 加仓保证金 4%
RR = float(os.getenv("RR", "1.0"))
LEFT, RIGHT = int(os.getenv("LEFT", "5")), int(os.getenv("RIGHT", "2"))
SL_BUFFER = float(os.getenv("SL_BUFFER", "0.0005"))

# 30分钟K线间隔（毫秒）
BAR_30M_MS = 30 * 60 * 1000

ASSETS = {
    "BTC": {
        "symbol": "BTC/USDT:USDT",
        "instId": "BTC-USDT-SWAP",
        "max_stop_pct": 0.017,
        "ct_val": 0.01,  # 每张合约 0.01 BTC
        "add_frac": 0.40,  # 加仓点：浮亏40%（入场到止损走完2/5时加仓，回测最优）
    },
    "ETH": {
        "symbol": "ETH/USDT:USDT",
        "instId": "ETH-USDT-SWAP",
        "max_stop_pts": 50.0,
        "ct_val": 0.1,  # 每张合约 0.1 ETH
        "add_frac": 0.40,  # 加仓点：浮亏40%
    },
}

STATE_FILE = "trading_state.json"

# ═══════════════════════════════════════════════
# 飞书通知
# ═══════════════════════════════════════════════

def feishu(title, content, color="blue"):
    """发送飞书卡片消息"""
    if not FEISHU_WEBHOOK:
        print(f"  [飞书] 跳过(未配置): {title}")
        return
    try:
        import requests as req
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": [{"tag": "markdown", "content": content}],
            },
        }
        r = req.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        print(f"  [飞书] {r.status_code} | {title}")
    except Exception as e:
        print(f"  [飞书] 发送失败: {e}")


# ═══════════════════════════════════════════════
# 状态持久化
# ═══════════════════════════════════════════════

def load_state():
    """加载交易状态（已交易分型）"""
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
            # 把traded_pivots转为set of tuples
            s["traded_pivots"] = set(tuple(p) for p in s.get("traded_pivots", []))
            s.setdefault("active_positions", {})
            return s
        except Exception as e:
            print(f"  [状态] 加载失败: {e}")
    return {
        "traded_pivots": set(),  # {(timestamp_ms, 'long'/'short'), ...}
        "last_scan": "",         # 上次扫描时间
        "total_trades": 0,
        "total_wins": 0,
        "total_losses": 0,
        "active_positions": {},  # {name: {"pivot_ts":..,"direction":..,"entry":..,"sl":..,"tp":..,"added":bool,"contracts":..}}
    }


def save_state(state):
    """保存交易状态"""
    try:
        to_save = dict(state)
        to_save["traded_pivots"] = list(state["traded_pivots"])
        to_save["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(STATE_FILE, "w") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [状态] 保存失败: {e}")


# ═══════════════════════════════════════════════
# 分型计算
# ═══════════════════════════════════════════════

def add_fractals(df, left, right):
    """向量化计算Williams分型"""
    df = df.copy()
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]
    lm = pd.concat(low_shifts, axis=1)
    hm = pd.concat(high_shifts, axis=1)
    min_low = lm.min(axis=1)
    max_high = hm.max(axis=1)
    count_low = (lm.values == df["low"].values[:, None]).sum(axis=1)
    count_high = (hm.values == df["high"].values[:, None]).sum(axis=1)
    df["fractal_low"] = (df["low"] == min_low) & (count_low == 1)
    df["fractal_high"] = (df["high"] == max_high) & (count_high == 1)
    return df


# ═══════════════════════════════════════════════
# OKX 交易接口
# ═══════════════════════════════════════════════

class OKXTrader:
    def __init__(self):
        if not all([API_KEY, API_SECRET, PASSPHRASE]):
            raise RuntimeError("❌ OKX API Key 未配置！请检查 .env 文件")

        cfg = {
            "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
            "enableRateLimit": True, "timeout": 30000,
            "options": {"defaultType": "swap"},
        }
        # 代理支持（国内环境可能需要 Clash/V2Ray 代理）
        proxy_url = os.getenv("OKX_PROXY", "")
        if proxy_url:
            cfg["proxies"] = {"http": proxy_url, "https": proxy_url}
            print(f"  使用代理: {proxy_url}")

        self.exchange = ccxt.okx(cfg)
        # ★ 实盘模式（非沙盒）
        self.exchange.set_sandbox_mode(False)

        print("正在连接 OKX 实盘...")
        for attempt in range(3):
            try:
                self.exchange.load_markets(reload=True)
                break
            except Exception as e:
                print(f"  load_markets 第{attempt+1}次失败: {e}")
                if attempt == 2:
                    raise
                time.sleep(3)

        print("✅ OKX 实盘连接成功")
        self._verify_connection()

    def _verify_connection(self):
        """验证API连接和账户信息"""
        try:
            bal = self.fetch_balance()
            print(f"  账户余额: {bal:.2f} USDT")
        except Exception as e:
            print(f"  ⚠️ 余额查询失败: {e}")

    def fetch_balance(self):
        """查询 USDT 余额"""
        bal = self.exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        return float(usdt.get("free", 0) or usdt.get("total", 0) or 0)

    def fetch_position(self, name):
        """查询持仓"""
        cfg = ASSETS[name]
        try:
            positions = self.exchange.fetch_positions([cfg["symbol"]])
            for p in positions:
                contracts = float(p.get("contracts", 0))
                if abs(contracts) > 0:
                    pos_side = p.get("posSide", p.get("side", "long"))
                    return {
                        "side": pos_side,
                        "contracts": contracts,
                        "entry": float(p.get("entryPrice", 0)),
                        "pnl": float(p.get("unrealizedPnl", 0)),
                    }
            return None
        except Exception as e:
            print(f"  ⚠️ 持仓查询失败 [{name}]: {e}")
            return None

    def get_position_state(self, name, equity):
        """
        从 OKX 恢复完整持仓状态：方向、张数、入场价、TP/SL、是否已加仓。
        用于机器人重启后重建 active_positions 状态。
        """
        pos = self.fetch_position(name)
        if not pos:
            return None
        cfg = ASSETS[name]

        # 从挂单读取 TP/SL
        sl = 0.0
        tp = 0.0
        for ord_type in ["oco", "conditional", "move_order_stop"]:
            try:
                pending = self.exchange.private_get_trade_orders_algo_pending({
                    "instId": cfg["instId"],
                    "ordType": ord_type,
                })
                data = pending.get("data", []) if isinstance(pending, dict) else []
                for item in data:
                    sl_v = float(item.get("slTriggerPx", 0) or 0)
                    tp_v = float(item.get("tpTriggerPx", 0) or 0)
                    if sl_v > 0:
                        sl = sl_v
                    if tp_v > 0:
                        tp = tp_v
            except Exception:
                pass

        # 判断是否已加仓：当前持仓张数 vs 首次开仓应有张数
        # 首次开仓 = 3%保证金 × 100x / (入场价 × 合约面值)
        ct_val = cfg["ct_val"]
        entry = float(pos["entry"])
        current = float(pos["contracts"])
        expected = equity * MARGIN_PCT * LEVERAGE / (entry * ct_val) if entry > 0 and equity > 0 else 0
        added = (expected > 0) and (current > expected * 1.5)

        print(f"  [{name}] 重建状态: {pos['side']} {current}张 @{entry} SL={sl} TP={tp} "
              f"预期首仓{expected:.2f}张 → {'已加仓' if added else '未加仓'}")

        return {
            "direction": pos["side"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "added": added,
            "contracts": current,
        }

    def fetch_ohlcv(self, name, tf, limit=120):
        """获取K线数据"""
        cfg = ASSETS[name]
        rows = self.exchange.fetch_ohlcv(cfg["symbol"], tf, limit=limit)
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        # 去掉最后一根未完成的K线
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    def fetch_price(self, name):
        """获取最新实时价格（用 ticker，比拉K线更可靠）"""
        cfg = ASSETS[name]
        try:
            ticker = self.exchange.fetch_ticker(cfg["symbol"])
            return float(ticker.get("last", 0) or 0)
        except Exception as e:
            print(f"  [{name}] 获取实时价失败: {e}")
            return 0.0

    def set_leverage(self, name):
        """设置杠杆倍数"""
        cfg = ASSETS[name]
        try:
            self.exchange.set_leverage(
                LEVERAGE, cfg["symbol"],
                params={
                    "instId": cfg["instId"],
                    "lever": str(LEVERAGE),
                    "mgnMode": "cross",
                }
            )
        except Exception as e:
            print(f"  [{name}] 杠杆设置(可能已设置): {e}")

    def open_trade(self, name, signal, entry_price, sl, tp, equity):
        """开仓 + 挂止盈止损单"""
        cfg = ASSETS[name]
        ct_val = cfg["ct_val"]
        pos_side = signal  # "long" or "short"
        order_side = "buy" if signal == "long" else "sell"

        # 动态仓位: 3% 保证金 × 100x 杠杆（头仓）
        margin = equity * MARGIN_PCT
        notional = margin * LEVERAGE
        contracts = max(round(notional / (entry_price * ct_val), 2), 1)

        body = {
            "instId": cfg["instId"],
            "tdMode": "cross",
            "side": order_side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(contracts),
            "attachAlgoOrds": [{
                "tpTriggerPx": str(round(tp, 2)),
                "tpOrdPx": "-1",
                "slTriggerPx": str(round(sl, 2)),
                "slOrdPx": "-1",
                "sz": str(contracts),
            }],
        }

        print(f"  [{name}] 开仓: {order_side.upper()} {contracts}张 @{entry_price:.2f} "
              f"SL={sl:.2f} TP={tp:.2f}")

        try:
            order = self.exchange.private_post_trade_order(body)
            print(f"  [{name}] ✅ 开仓成功!")
            return contracts, margin
        except Exception as e:
            print(f"  [{name}] 下单失败: {e}")
            # 重试（去掉posSide）
            try:
                body2 = dict(body)
                del body2["posSide"]
                for ao in body2.get("attachAlgoOrds", []):
                    ao.pop("posSide", None)
                order = self.exchange.private_post_trade_order(body2)
                print(f"  [{name}] ✅ 重试成功!")
                return contracts, margin
            except Exception as e2:
                print(f"  [{name}] ❌ 重试也失败: {e2}")
                raise

    def cancel_all_algos(self, name):
        """撤销该币种所有挂着的止盈止损 algo 单"""
        cfg = ASSETS[name]
        inst_id = cfg["instId"]
        cancelled = 0
        # OKX 的 orders-algo-pending 需要 ordType 参数
        # 止盈止损可能是 oco（双向）或 conditional（单向）或 move_order_stop（移动止损）
        for ord_type in ["oco", "conditional", "move_order_stop"]:
            try:
                pending = self.exchange.private_get_trade_orders_algo_pending({
                    "instId": inst_id,
                    "ordType": ord_type,
                })
                data = pending.get("data", []) if isinstance(pending, dict) else []
                for item in data:
                    algo_id = item.get("algoId")
                    if not algo_id:
                        continue
                    try:
                        self.exchange.private_post_trade_cancel_algo({
                            "instId": inst_id,
                            "algoId": algo_id,
                        })
                        cancelled += 1
                    except Exception as e:
                        print(f"    ⚠️ 撤销 algo {algo_id} 失败: {e}")
            except Exception as e:
                print(f"  [{name}] 查询 {ord_type} 挂单失败: {e}")
        print(f"  [{name}] 已撤销 {cancelled} 个止盈止损单")
        return cancelled

    def add_to_position(self, name, signal, add_price, sl, new_tp, original_contracts, equity):
        """
        加仓 + 重挂统一止盈止损
        - 市价加仓 4% 保证金（先加仓并验证成交）
        - 撤销原有 algo 单
        - 按加仓后平均成本重挂 TP/SL（SL不变）
        """
        cfg = ASSETS[name]
        ct_val = cfg["ct_val"]
        pos_side = signal
        order_side = "buy" if signal == "long" else "sell"

        # 加仓 4% 保证金
        margin = equity * ADD_MARGIN_PCT
        notional = margin * LEVERAGE
        contracts = max(round(notional / (add_price * ct_val), 2), 1)
        total_contracts = original_contracts + contracts

        # 1. 市价加仓
        body = {
            "instId": cfg["instId"],
            "tdMode": "cross",
            "side": order_side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(contracts),
        }
        print(f"  [{name}] 加仓: {order_side.upper()} {contracts}张 @{add_price:.2f}")

        order = None
        try:
            order = self.exchange.private_post_trade_order(body)
        except Exception as e:
            print(f"  [{name}] 加仓失败(尝试无posSide): {e}")
            body2 = dict(body)
            del body2["posSide"]
            order = self.exchange.private_post_trade_order(body2)

        # 2. 检查下单返回的 sCode（ccxt 不检查 code=0 但 sCode!=0 的情况）
        if isinstance(order, dict):
            data = order.get("data", [])
            if data:
                s_code = str(data[0].get("sCode", "0"))
                s_msg = data[0].get("sMsg", "")
                if s_code != "0":
                    raise RuntimeError(f"加仓下单被拒绝: sCode={s_code} {s_msg}")

        # 3. 验证加仓是否真的成交（回查持仓张数）
        time.sleep(2)  # 等订单成交
        pos_after = self.fetch_position(name)
        after_contracts = float(pos_after["contracts"]) if pos_after else 0.0
        if after_contracts <= original_contracts:
            raise RuntimeError(
                f"加仓后仓位未增加: {original_contracts}张 -> {after_contracts}张"
                f"（下单可能被拒或未成交）"
            )
        print(f"  [{name}] ✅ 加仓成功，持仓 {original_contracts} -> {after_contracts} 张")

        # 4. 撤销旧 algo（加仓成功后）
        self.cancel_all_algos(name)

        # 5. 重挂统一 TP/SL（oco 单，覆盖全仓）
        close_side = "sell" if signal == "long" else "buy"
        algo_body = {
            "instId": cfg["instId"],
            "tdMode": "cross",
            "side": close_side,
            "posSide": pos_side,
            "ordType": "oco",
            "sz": str(after_contracts),  # 用实际成交后的张数
            "tpTriggerPx": str(round(new_tp, 2)),
            "tpOrdPx": "-1",
            "slTriggerPx": str(round(sl, 2)),
            "slOrdPx": "-1",
        }
        try:
            self.exchange.private_post_trade_order_algo(algo_body)
            print(f"  [{name}] ✅ 重挂统一止盈止损: SL={sl:.2f} TP={new_tp:.2f} ({after_contracts}张)")
        except Exception as e:
            print(f"  [{name}] ⚠️ 重挂止盈止损失败(需手动检查): {e}")

        return contracts, margin, after_contracts


# ═══════════════════════════════════════════════
# 信号检测（和回测逻辑一致）
# ═══════════════════════════════════════════════

def detect_signal(name, m30_df, h1_df, h4_df, cfg, state):
    """
    扫描最新的30分钟K线，检测分型 + 1h宽松共振 + 4h严格共振 信号。
    4h严格共振：4h 最近一个分型方向必须匹配（做多须4h最近是底分型）
    返回: dict or None
    """
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None

    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)

    # 4h 分型事件（严格共振用，最近分型方向匹配）
    h4 = add_fractals(h4_df.copy(), 2, 2)
    h4_mask = h4['fractal_low'].values | h4['fractal_high'].values
    h4_ts = h4['timestamp'].values[h4_mask]
    h4_typ = np.where(h4['fractal_low'].values[h4_mask], 'low', 'high')
    if len(h4_ts) > 1:
        order = np.argsort(h4_ts)
        h4_ts = h4_ts[order]
        h4_typ = h4_typ[order]

    # 从最新往前扫3根
    for offset in range(3):
        i = n - 1 - offset
        pivot = i - RIGHT
        if pivot < 0:
            continue

        direction = None
        if m30.loc[pivot, "fractal_low"]:
            direction = "long"
        elif m30.loc[pivot, "fractal_high"]:
            direction = "short"
        if direction is None:
            continue

        pivot_ts = int(m30.loc[pivot, "timestamp"])

        # ── 分型去重 ──
        if (pivot_ts, direction) in state["traded_pivots"]:
            continue

        # ── 1h宽松共振 ──
        sub = h1[h1["timestamp"] <= pivot_ts]
        if len(sub) < 5:
            continue
        if direction == "long" and not sub["fractal_low"].any():
            continue
        if direction == "short" and not sub["fractal_high"].any():
            continue

        # ── 4h严格共振（最近4h分型方向必须匹配）──
        if len(h4_ts) > 0:
            idx4 = np.searchsorted(h4_ts, pivot_ts, side='right') - 1
            if idx4 < 0:
                continue
            if direction == "long" and h4_typ[idx4] != "low":
                continue
            if direction == "short" and h4_typ[idx4] != "high":
                continue

        # ── 计算止损止盈 ──
        entry = float(m30.loc[i, "close"])

        if direction == "long":
            sl = float(m30.loc[pivot, "low"]) * (1 - SL_BUFFER)
            risk = entry - sl
            if risk <= 0: continue
            max_stop = cfg.get("max_stop_pct")
            if max_stop and risk > entry * max_stop:
                risk = entry * max_stop
                sl = entry - risk
            tp = entry + RR * risk
        else:
            sl = float(m30.loc[pivot, "high"]) * (1 + SL_BUFFER)
            risk = sl - entry
            if risk <= 0: continue
            max_pts = cfg.get("max_stop_pts")
            if max_pts and risk > max_pts:
                risk = max_pts
                sl = entry + risk
            tp = entry - RR * risk

        return {
            "asset": name,
            "signal": direction,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "pivot_ts": pivot_ts,
            "time": str(m30.loc[i, "datetime"]),
        }

    return None


# ═══════════════════════════════════════════════
# 主扫描逻辑
# ═══════════════════════════════════════════════

def is_30min_node():
    """判断当前是否为30分钟K线收盘节点（收盘后2分钟内）"""
    now = datetime.now()
    minute = now.minute
    # 30分钟K线在 00分/30分 收盘，收盘后0-2分钟内判定为节点
    # 不在收盘前提前判定，避免拿到未收完的K线
    return (minute % 30 <= 2)


def run_scan(state=None):
    """执行一次完整扫描"""
    if state is None:
        state = load_state()

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    node_flag = "🔔30min节点" if is_30min_node() else "⏱常规扫描"
    print(f"\n{'='*60}")
    print(f"[{ts_str}] {node_flag}")
    print(f"{'='*60}")

    # ── 连接 OKX ──
    try:
        trader = OKXTrader()
    except Exception as e:
        msg = f"❌ OKX 连接失败: {e}"
        print(msg)
        feishu("❌ OKX 连接失败", msg, "red")
        return state

    # ── 账户余额 ──
    equity = trader.fetch_balance()
    print(f"账户余额: {equity:.2f} USDT")

    # ── 当前持仓 ──
    btc_pos = trader.fetch_position("BTC")
    eth_pos = trader.fetch_position("ETH")

    btc_str = f"{btc_pos['side']} {btc_pos['contracts']}张 @{btc_pos['entry']:.1f} PnL:{btc_pos['pnl']:.2f}" if btc_pos else "空仓"
    eth_str = f"{eth_pos['side']} {eth_pos['contracts']}张 @{eth_pos['entry']:.1f} PnL:{eth_pos['pnl']:.2f}" if eth_pos else "空仓"
    print(f"BTC: {btc_str}")
    print(f"ETH: {eth_str}")

    # ── 信号检测 ──
    signals = []
    details = []
    add_events = []

    for name, cfg in ASSETS.items():
        pos = trader.fetch_position(name)
        add_frac = cfg.get("add_frac", 0.40)

        if pos:
            # ── 已有持仓：检查是否需要加仓 ──
            active = state["active_positions"].get(name)
            if active is None or active.get("sl", 0) == 0:
                # 状态丢失或 sl 未记录（旧版本遗留），从 OKX 重建完整状态
                rebuilt = trader.get_position_state(name, equity)
                if rebuilt is None:
                    continue
                state["active_positions"][name] = rebuilt
                active = rebuilt
                if rebuilt["added"]:
                    print(f"  [{name}] 持仓中(已加仓)，跳过")
                    details.append(f"**{name}**: 持仓中 ({rebuilt['direction']} {rebuilt['contracts']}张) 已加仓")
                    continue
                # 未加仓且 sl 已恢复，继续走加仓判断

            if active.get("added"):
                print(f"  [{name}] 持仓中(已加仓)，跳过")
                details.append(f"**{name}**: 持仓中 ({pos['side']} {pos['contracts']}张) 已加仓")
                continue

            # 检查是否触发加仓
            direction = active["direction"]
            sl = active["sl"]
            entry = active["entry"]

            # 用实时最新价判断（ticker，比拉K线更可靠且不触发限流）
            price = trader.fetch_price(name)

            trigger = False
            add_price = 0.0
            if direction == "long" and sl > 0:
                # 浮亏 add_frac（40%）处加仓：加仓价 = 入场价 - add_frac × 风险区间
                add_price = entry - add_frac * (entry - sl)
                trigger = price > 0 and price <= add_price
            elif direction == "short" and sl > 0:
                add_price = entry + add_frac * (sl - entry)
                trigger = price > 0 and price >= add_price

            if not trigger:
                print(f"  [{name}] 持仓中，价格 {price:.2f} 未到加仓区(触发价 {add_price:.2f})")
                details.append(f"**{name}**: 持仓中 ({direction} {pos['contracts']}张) @{entry:.1f}，加仓触发价 {add_price:.2f}")
                continue

            # ── 触发加仓 ──
            print(f"  🔔 [{name}] 价格 {price:.2f} 触及加仓区 {add_price:.2f}，执行加仓!")
            # 加仓后平均成本
            avg_entry = (entry + add_price) / 2
            if direction == "long":
                new_risk = avg_entry - sl
                new_tp = avg_entry + RR * new_risk
            else:
                new_risk = sl - avg_entry
                new_tp = avg_entry - RR * new_risk

            try:
                # 用当前实际持仓张数（而非状态里可能过期的记录）
                original_contracts = float(pos["contracts"])
                add_contracts, add_margin, total_contracts = trader.add_to_position(
                    name, direction, add_price, sl, new_tp,
                    original_contracts, equity
                )
                # 更新状态
                active["added"] = True
                active["entry"] = avg_entry
                active["tp"] = new_tp
                active["contracts"] = total_contracts
                add_events.append(
                    f"📈 **{name} {direction.upper()} 加仓** @{add_price:.2f}\n"
                    f"加仓 {add_contracts}张 | 总 {total_contracts}张 | 平均成本 {avg_entry:.2f}\n"
                    f"止损不变 {sl:.2f} | 新止盈 {new_tp:.2f}"
                )
            except Exception as e:
                err_msg = str(e)[:200]
                print(f"  [{name}] ❌ 加仓失败: {err_msg}")
                feishu(f"❌ [{name}] 加仓失败", f"错误: `{err_msg}`", "red")
            continue

        # ── 无持仓：正常信号检测 ──
        try:
            m30 = trader.fetch_ohlcv(name, "30m", 120)
            h1 = trader.fetch_ohlcv(name, "1h", 60)
            h4 = trader.fetch_ohlcv(name, "4h", 100)
        except Exception as e:
            print(f"  [{name}] 数据拉取失败: {e}")
            details.append(f"**{name}**: 数据拉取失败 `{str(e)[:80]}`")
            continue

        price = float(m30["close"].iloc[-1]) if len(m30) > 0 else 0
        print(f"  [{name}] 最新价: {price:.2f}")

        sig = detect_signal(name, m30, h1, h4, cfg, state)
        if not sig:
            print(f"  [{name}] 无信号")
            details.append(f"**{name}**: 无信号 @{price:.2f}")
            continue

        print(f"  🔥 [{name}] 发现信号: {sig['signal'].upper()} @{sig['entry']:.2f} "
              f"SL={sig['sl']:.2f} TP={sig['tp']:.2f}")

        # ── 执行开仓 ──
        try:
            trader.set_leverage(name)
            contracts, margin = trader.open_trade(
                name, sig["signal"], sig["entry"],
                sig["sl"], sig["tp"], equity
            )

            # 更新状态
            state["traded_pivots"].add((sig["pivot_ts"], sig["signal"]))
            state["total_trades"] += 1
            state["active_positions"][name] = {
                "pivot_ts": sig["pivot_ts"],
                "direction": sig["signal"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "added": False,
                "contracts": float(contracts),
            }
            signals.append(sig)
            # 计算加仓触发价（浮亏 add_frac 处）用于展示
            if sig["signal"] == "long":
                add_trigger = sig["entry"] - add_frac * (sig["entry"] - sig["sl"])
            else:
                add_trigger = sig["entry"] + add_frac * (sig["sl"] - sig["entry"])
            details.append(
                f"🔥 **{name} {sig['signal'].upper()}** @{sig['entry']:.2f}\n"
                f"止损 {sig['sl']:.2f} | 止盈 {sig['tp']:.2f} | {contracts}张 | 保证金 {margin:.2f}\n"
                f"加仓触发价 ≈ {add_trigger:.2f}"
            )

            # 清理旧的分型记录（保留最近500个）
            if len(state["traded_pivots"]) > 500:
                sorted_pivots = sorted(state["traded_pivots"], reverse=True)
                state["traded_pivots"] = set(sorted_pivots[:500])

        except Exception as e:
            err_msg = str(e)[:200]
            print(f"  [{name}] ❌ 开仓失败: {err_msg}")
            details.append(f"❌ **{name}** 开仓失败: `{err_msg}`")
            feishu(f"❌ [{name}] 开仓失败", f"信号: {sig['signal'].upper()} @{sig['entry']:.2f}\n错误: `{err_msg}`", "red")

    # ── 清理已平仓的 active_positions ──
    for name in list(state["active_positions"].keys()):
        pos = trader.fetch_position(name)
        if pos is None:
            print(f"  [{name}] 仓位已平，清理状态")
            del state["active_positions"][name]

    # ── 日常通知（30min节点必发，其他时候有信号/加仓才发）──
    if is_30min_node() or signals or add_events:
        sig_text = "\n".join(details) if details else "本轮无信号"
        add_text = "\n".join(add_events) if add_events else ""
        action = "🎯 已下单" if signals else ("📈 已加仓" if add_events else ("👀 30min节点扫描" if is_30min_node() else "👀 等待信号"))

        # 统计信息
        stats_text = (
            f"📊 **运行统计**: 总{state['total_trades']}笔\n\n"
            f"**BTC**: {btc_str}\n"
            f"**ETH**: {eth_str}\n\n"
            f"**本轮信号**:\n{sig_text}"
        )
        if add_text:
            stats_text += f"\n\n**本轮加仓**:\n{add_text}"

        feishu(
            f"{action} | {ts_str}",
            f"**✅ OKX实盘在线** | 余额: {equity:.2f} USDT\n\n{stats_text}",
            color="green" if (signals or add_events) else "blue",
        )

    # ── 保存状态 ──
    save_state(state)

    print(f"\n余额={equity:.2f} | BTC={btc_str} | ETH={eth_str} | 信号={len(signals)} | 加仓={len(add_events)}")
    print(f"已交易分型: {len(state['traded_pivots'])}个 | 活跃持仓: {len(state['active_positions'])}个")
    print("[Done]")

    return state


# ═══════════════════════════════════════════════
# 持续运行循环
# ═══════════════════════════════════════════════

running = True

def handle_shutdown(signum, frame):
    global running
    print("\n⏹ 收到停止信号，安全退出...")
    running = False

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def run_loop():
    """持续运行：每5分钟扫描一次"""
    print("=" * 60)
    print("  OKX 实盘交易机器人 启动")
    print("  策略: 分型(5,2) + 1h宽松共振 + 4h严格共振 + RR=1:1 + 100x杠杆")
    print("  仓位: 头仓3% + 浮亏40%加仓4%，总止损不变")
    print("  调度: 每5分钟扫描 | 30min收盘节点重点检测")
    print("  停止: Ctrl+C")
    print("=" * 60)

    state = load_state()
    print(f"已加载状态: {state['total_trades']}笔历史交易, {len(state['traded_pivots'])}个已交易分型")

    # 启动时先发一条上线通知
    feishu(
        "🚀 交易机器人已上线",
        f"**策略**: 分型(5,2)+1h宽松共振+4h严格共振 RR=1:1\n"
        f"**杠杆**: {LEVERAGE}x | 头仓: {MARGIN_PCT*100:.0f}% | 加仓: {ADD_MARGIN_PCT*100:.0f}%\n"
        f"**加仓点**: 浮亏40%(入场到止损2/5处)\n"
        f"**扫描频率**: 每5分钟 | 30min收盘节点重点\n"
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        color="turquoise",
    )

    while running:
        try:
            state = run_scan(state)
        except KeyboardInterrupt:
            break
        except Exception as e:
            err = traceback.format_exc()
            print(f"\n[异常] {err}")
            feishu("⚠️ 扫描异常", f"```{err[:800]}```", "red")
            time.sleep(30)  # 异常后等30秒再重试
            continue

        # 等待5分钟后再次扫描
        wait_seconds = 5 * 60
        print(f"\n⏳ 等待 {wait_seconds//60} 分钟后下次扫描... (Ctrl+C 停止)")
        for _ in range(wait_seconds):
            if not running:
                break
            time.sleep(1)

    print("\n👋 交易机器人已停止")
    feishu("⏹ 交易机器人已下线", f"停止时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", color="grey")
    save_state(state)


# ═══════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OKX 实盘交易机器人")
    parser.add_argument("--once", action="store_true", help="单次扫描后退出")
    parser.add_argument("--status", action="store_true", help="仅查看当前状态")
    parser.add_argument("--reset", action="store_true", help="重置持仓状态（清空 active_positions，重新从OKX读取）")
    args = parser.parse_args()

    if args.reset:
        state = load_state()
        state["active_positions"] = {}
        save_state(state)
        print("✅ 已重置持仓状态（active_positions 已清空）")
        print("下次扫描时会从 OKX 重新读取持仓的止损/止盈/加仓状态")
        sys.exit(0)

    if args.status:
        state = load_state()
        print("当前交易状态:")
        print(f"  总交易: {state['total_trades']}笔")
        print(f"  胜利: {state['total_wins']}笔")
        print(f"  失败: {state['total_losses']}笔")
        print(f"  已交易分型: {len(state['traded_pivots'])}个")
        print(f"  上次扫描: {state['last_scan']}")
        print(f"  活跃持仓:")
        for name, ap in state.get("active_positions", {}).items():
            print(f"    {name}: {ap.get('direction')} {ap.get('contracts')}张 "
                  f"SL={ap.get('sl')} TP={ap.get('tp')} added={ap.get('added')}")
        sys.exit(0)

    if args.once:
        state = load_state()
        run_scan(state)
    else:
        run_loop()
