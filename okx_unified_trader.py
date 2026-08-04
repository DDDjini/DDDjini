"""
OKX 模拟盘统一交易机器人
- 同时监控 BTC、ETH 双币种
- 策略：30m 分型 + 1h 分型共振
- 动态仓位：每次开仓前实时查询模拟盘账户余额，计算 5% 保证金
- 飞书通知：信号/开仓/平仓/异常 实时推送
- 杠杆：100x 全仓

运行模式:
  python okx_unified_trader.py                → 本地模拟（不下单，飞书推信号）
  python okx_unified_trader.py --trade        → 模拟盘真实下单 + 设杠杆 + 查余额
"""

import ccxt
import pandas as pd
import numpy as np
import time
import json
import os
import requests
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from backtest_fractal import add_fractals, ensure_dir

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# 飞书通知
# ═══════════════════════════════════════════════════════════════

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/7c275d82-a87a-42a6-aba5-a306a1140353"


def feishu_send(title: str, content: str, color: str = "blue"):
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": color,
                },
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text", "content": f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
                    ]},
                ],
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  [飞书] 发送失败: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  [飞书] 异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 资产配置
# ═══════════════════════════════════════════════════════════════

ASSET_CONFIGS = {
    "BTC": {
        "symbol": "BTC/USDT:USDT",
        "max_stop_pct": 0.017,
        "max_stop_pts": None,
        "log_dir": "results/paper_trading/btc",
    },
    "ETH": {
        "symbol": "ETH/USDT:USDT",
        "max_stop_pct": None,
        "max_stop_pts": 50.0,
        "log_dir": "results/paper_trading/eth",
    },
}


class AssetTracker:
    """单个币种：信号检测 + 持仓跟踪"""

    def __init__(self, name: str, config: dict,
                 left: int = 5, right: int = 2,
                 rr: float = 2.0, sl_buffer: float = 0.0005, fee_rate: float = 0.0005):
        self.name = name
        self.symbol = config["symbol"]
        self.max_stop_pct = config.get("max_stop_pct")
        self.max_stop_pts = config.get("max_stop_pts")
        self.log_dir = config["log_dir"]
        self.left, self.right = left, right
        self.rr, self.sl_buffer, self.fee_rate = rr, sl_buffer, fee_rate

        ensure_dir(self.log_dir)

        self.ct_val = 0.01
        self.min_contracts = 0.01
        self.position = None
        self.last_fractal_bar = -1
        self.trade_history = []

        self.signal_log_file = Path(self.log_dir) / "signals.jsonl"
        self.trade_file = Path(self.log_dir) / "trades.jsonl"

    def log_signal(self, signal: dict):
        signal["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.signal_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(signal, default=str) + "\n")

    def log_trade(self, trade: dict):
        trade["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.trade_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, default=str) + "\n")

    def check_fractal_signal(self, m30_df: pd.DataFrame, h1_df: pd.DataFrame) -> dict:
        n = len(m30_df)
        if n < self.left + self.right + 2:
            return None

        m30_df = add_fractals(m30_df.copy(), self.left, self.right)
        h1_df = add_fractals(h1_df.copy(), 2, 2)

        i = n - 1
        pivot_idx = i - self.right
        if pivot_idx < 0 or pivot_idx <= self.last_fractal_bar:
            return None

        signal = None
        if m30_df.loc[pivot_idx, "fractal_low"]:
            signal = "long"
        elif m30_df.loc[pivot_idx, "fractal_high"]:
            signal = "short"
        if signal is None:
            return None

        current_ts = m30_df.loc[pivot_idx, "timestamp"]
        h1_subset = h1_df[h1_df["timestamp"] <= current_ts]
        if len(h1_subset) < 5:
            return None
        if signal == "long" and not h1_subset["fractal_low"].any():
            return None
        if signal == "short" and not h1_subset["fractal_high"].any():
            return None

        entry_price = m30_df.loc[i, "close"]
        entry_time = m30_df.loc[i, "datetime"]

        if signal == "long":
            pivot_low = m30_df.loc[pivot_idx, "low"]
            sl = pivot_low * (1 - self.sl_buffer)
            risk = entry_price - sl
            if risk <= 0:
                return None
            if self.max_stop_pct:
                max_risk = entry_price * self.max_stop_pct
                if risk > max_risk:
                    risk = max_risk; sl = entry_price - max_risk
            elif self.max_stop_pts:
                if risk > self.max_stop_pts:
                    risk = self.max_stop_pts; sl = entry_price - self.max_stop_pts
            tp = entry_price + self.rr * risk
        else:
            pivot_high = m30_df.loc[pivot_idx, "high"]
            sl = pivot_high * (1 + self.sl_buffer)
            risk = sl - entry_price
            if risk <= 0:
                return None
            if self.max_stop_pct:
                max_risk = entry_price * self.max_stop_pct
                if risk > max_risk:
                    risk = max_risk; sl = entry_price + max_risk
            elif self.max_stop_pts:
                if risk > self.max_stop_pts:
                    risk = self.max_stop_pts; sl = entry_price + self.max_stop_pts
            tp = entry_price - self.rr * risk

        self.last_fractal_bar = pivot_idx
        return {
            "symbol": self.symbol, "asset": self.name, "signal": signal,
            "entry_price": round(entry_price, 2),
            "entry_time": entry_time.isoformat(),
            "stop_loss": round(sl, 2), "take_profit": round(tp, 2),
            "pivot_idx": int(pivot_idx),
        }

    def check_exit_bar(self, bar: pd.Series):
        if self.position is None:
            return None
        pos = self.position
        side, sl, tp, entry = pos["side"], pos["stop_loss"], pos["take_profit"], pos["entry_price"]
        high, low = float(bar["high"]), float(bar["low"])

        exited = exit_price = result = None
        if side == "long":
            if low <= sl:
                exited, exit_price, result = True, sl, "loss"
            elif high >= tp:
                exited, exit_price, result = True, tp, "win"
        else:
            if high >= sl:
                exited, exit_price, result = True, sl, "loss"
            elif low <= tp:
                exited, exit_price, result = True, tp, "win"

        if not exited:
            return None

        gross_ret = (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
        net_ret = gross_ret - self.fee_rate * 2
        notional = entry * pos["size"] * self.ct_val
        pnl = net_ret * notional

        self.position = None
        trade = {
            "action": "CLOSE", "asset": self.name, "side": side,
            "entry_price": entry, "exit_price": exit_price, "result": result,
            "gross_return": gross_ret, "net_return": net_ret,
            "notional": notional, "pnl": pnl,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "bar_time": str(bar["datetime"]),
        }
        self.trade_history.append(trade)
        self.log_trade(trade)
        return trade


# ═══════════════════════════════════════════════════════════════
# 统一交易机器人
# ═══════════════════════════════════════════════════════════════

class UnifiedTrader:
    """统一交易机器人 ─ 始终连接 OKX 模拟盘，--trade 控制是否真实下单"""

    def __init__(self, timeframe="30m", higher_tf="1h",
                 left=5, right=2, rr=1.0, sl_buffer=0.0005, fee_rate=0.0005,
                 margin_pct=0.05, leverage=100,
                 live_trade=False,
                 proxy="http://127.0.0.1:7897"):
        self.timeframe = timeframe
        self.higher_tf = higher_tf
        self.rr = rr
        self.margin_pct = margin_pct
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.live_trade = live_trade      # True=模拟盘真实下单, False=仅本地模拟
        self.proxy = proxy

        # ── 初始化交易所 ──
        api_key = os.getenv("OKX_API_KEY")
        api_secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_PASSPHRASE")

        print(f"API Key: {'✓' if api_key else '✗ 未配置'}")
        print(f"API Secret: {'✓' if api_secret else '✗ 未配置'}")
        print(f"Passphrase: {'✓' if passphrase else '✗ 未配置'}")

        config = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}}
        if api_key and api_secret and passphrase:
            config.update({"apiKey": api_key, "secret": api_secret, "password": passphrase})
        if proxy:
            config["proxies"] = {"http": proxy, "https": proxy}

        self.exchange = ccxt.okx(config)
        self.exchange.set_sandbox_mode(True)   # ← 始终连接模拟盘
        print("✓ 已连接 OKX Sandbox 模拟盘\n")

        # ── 创建资产跟踪器 ──
        self.assets = {}
        for name, acfg in ASSET_CONFIGS.items():
            self.assets[name] = AssetTracker(name, acfg, left, right, rr, sl_buffer, fee_rate)

        # ── 加载合约信息 ──
        self.exchange.load_markets()
        for name, tracker in self.assets.items():
            market = self.exchange.market(tracker.symbol)
            tracker.ct_val = float(market.get("contractSize", 1))
            tracker.min_contracts = float(market.get("limits", {}).get("amount", {}).get("min", 0.01))
            print(f"[{name}] {tracker.symbol}  面值={tracker.ct_val}  最小={tracker.min_contracts}张")

        # ── 设置杠杆 ──
        if self.live_trade:
            for name, tracker in self.assets.items():
                self._do_set_leverage(tracker.symbol)
        else:
            print("\n[本地模拟] 不设置杠杆（--trade 模式下才会设置）")

        # ── 全局状态 ──
        self.capital = 10000.0
        self.initial_capital = 10000.0
        self.peak_capital = 10000.0
        self.max_dd = 0.0
        self.daily_trades = 0
        self.current_day = datetime.now(timezone.utc).date()

        self.root_log = Path("results/paper_trading")
        ensure_dir(str(self.root_log))
        self.balance_log = self.root_log / "balance.jsonl"

    def log_balance(self, balance: dict):
        balance["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.balance_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(balance, default=str) + "\n")

    # ═══════════════════════════════════════════════════════════
    # OKX API 封装
    # ═══════════════════════════════════════════════════════════

    def get_balance(self) -> dict:
        try:
            bal = self.exchange.fetch_balance()
            usdt = bal.get("USDT", {})
            return {"total": float(usdt.get("total", 0)),
                    "free": float(usdt.get("free", 0)),
                    "used": float(usdt.get("used", 0))}
        except Exception as e:
            print(f"  [错误] 获取余额失败: {e}")
            return None

    def fetch_data(self, symbol: str, tf: str, limit=100) -> pd.DataFrame:
        try:
            rows = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            if len(df) > 1:
                df = df.iloc[:-1].reset_index(drop=True)
            return df
        except Exception as e:
            print(f"  [{symbol}] 获取{tf}数据失败: {e}")
            return None

    def get_real_position(self, symbol: str) -> dict:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                if pos.get("symbol") == symbol:
                    if abs(float(pos.get("contracts", 0))) > 0:
                        if not pos.get("posSide"):
                            pos["posSide"] = pos.get("side", "long")
                        return pos
            return None
        except Exception as e:
            print(f"  [{symbol}] 获取持仓失败: {e}")
            return None

    def _do_set_leverage(self, symbol: str):
        try:
            inst_id = symbol.replace("/", "-").replace(":", "-")
            result = self.exchange.set_leverage(
                self.leverage, symbol,
                params={"instId": inst_id, "lever": str(self.leverage), "mgnMode": "cross"},
            )
            print(f"  [{symbol}] 杠杆已设置: {self.leverage}x 全仓")
        except Exception as e:
            print(f"  [{symbol}] 设置杠杆失败: {e}")

    def _do_open_position(self, asset_name: str, signal: dict, contracts: float):
        tracker = self.assets[asset_name]
        side = signal["signal"]
        pos_side = "long" if side == "long" else "short"
        order_side = "buy" if side == "long" else "sell"

        params = {
            "tdMode": "cross",
            "posSide": pos_side,
            "attachAlgoOrds": [{
                "tpTriggerPx": str(signal["take_profit"]),
                "tpOrdPx": "-1",
                "slTriggerPx": str(signal["stop_loss"]),
                "slOrdPx": "-1",
                "sz": str(contracts),
                "posSide": pos_side,
            }],
        }
        order = self.exchange.create_market_order(
            symbol=tracker.symbol, side=order_side, amount=contracts, params=params)
        print(f"  [{asset_name}] ✅ 模拟盘下单成功: {order_side} {contracts}张 @ {signal['entry_price']}")
        print(f"         SL: {signal['stop_loss']} | TP: {signal['take_profit']}")
        return order

    def _do_close_position(self, asset_name: str, contracts: float, pos_side: str):
        tracker = self.assets[asset_name]
        close_side = "sell" if pos_side == "long" else "buy"
        order = self.exchange.create_market_order(
            symbol=tracker.symbol, side=close_side, amount=contracts,
            params={"tdMode": "cross", "posSide": pos_side, "reduceOnly": True})
        print(f"  [{asset_name}] 模拟盘平仓: {close_side} {contracts}张")
        return order

    # ═══════════════════════════════════════════════════════════
    # 动态仓位计算
    # ═══════════════════════════════════════════════════════════

    def dynamic_position_size(self, asset_name: str, entry_price: float) -> float:
        """实时查模拟盘余额，动态算 5% 仓位"""
        tracker = self.assets[asset_name]
        balance = self.get_balance()
        if balance is None:
            equity = self.capital
            print(f"  [{asset_name}] 无法获取余额，使用本地: {equity:.2f}")
        else:
            equity = balance["free"] if balance["free"] > 0 else balance["total"]
            self.capital = equity
            print(f"  [{asset_name}] 实时余额: {equity:.2f} USDT")

        margin = equity * self.margin_pct
        notional = margin * self.leverage
        contracts = notional / (entry_price * tracker.ct_val)
        contracts = max(contracts, tracker.min_contracts)
        print(f"    保证金: {margin:.2f} → 名义价值: {notional:.2f} → {contracts} 张")
        return round(contracts, 2)

    # ═══════════════════════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════════════════════

    def run_cycle(self):
        now = datetime.now(timezone.utc)
        print(f"\n{'='*60}")
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] BTC + ETH 统一检测")
        print(f"模式: {'🔴 模拟盘真实下单' if self.live_trade else '🟡 本地模拟(不下单)'}")

        if now.date() != self.current_day:
            self.current_day = now.date()
            self.daily_trades = 0

        # 查余额
        balance = self.get_balance()
        if balance:
            self.capital = balance["free"] if balance["free"] > 0 else balance["total"]
            self.initial_capital = max(self.initial_capital, 1)  # 保底
            print(f"账户余额: {balance['total']:.2f} USDT (可用: {balance['free']:.2f})")
            self.log_balance(balance)

        # 逐个币种
        for name, tracker in self.assets.items():
            print(f"\n  ── [{name}] ──")
            self._process_asset(name, tracker, now)

        # 资金统计
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital
        dd = (self.peak_capital - self.capital) / self.peak_capital if self.peak_capital > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd
        total_ret = (self.capital / self.initial_capital - 1) * 100 if self.initial_capital > 0 else 0
        print(f"\n  [全局] 资金: {self.capital:.2f} | 收益: {total_ret:.2f}% | 最大回撤: {self.max_dd*100:.2f}%")

    def _process_asset(self, name: str, tracker: AssetTracker, now: datetime):
        # 获取K线
        m30_df = self.fetch_data(tracker.symbol, self.timeframe, limit=100)
        h1_df = self.fetch_data(tracker.symbol, self.higher_tf, limit=50)
        if m30_df is None or h1_df is None:
            print(f"  [跳过] 数据获取失败")
            return

        latest_price = m30_df["close"].iloc[-1]
        print(f"  价格: {latest_price:.2f} | 30m: {m30_df['datetime'].iloc[-1]} | 1h: {h1_df['datetime'].iloc[-1]}")

        # ── 检查模拟盘真实持仓 ──
        if self.live_trade:
            real_pos = self.get_real_position(tracker.symbol)
            if real_pos:
                entry_px = real_pos.get("entryPrice", 0)
                side = real_pos.get("posSide", real_pos.get("side", "long"))
                contracts = float(real_pos.get("contracts", 0))
                print(f"  📊 模拟盘持仓: {side} {contracts}张, 开仓价={entry_px}")
                tracker.position = {
                    "side": side, "entry_price": float(entry_px),
                    "stop_loss": 0, "take_profit": 0,
                    "size": contracts, "entry_time": now.isoformat(),
                    "order_id": real_pos.get("id", "unknown"),
                }
                return  # 已有真实持仓，不检测新信号
            else:
                print(f"  模拟盘无持仓")
                tracker.position = None

        # ── 本地持仓止盈止损检查 ──
        if tracker.position:
            exit_trade = tracker.check_exit_bar(m30_df.iloc[-1])
            if exit_trade:
                result = exit_trade["result"]
                emoji = "🟢" if result == "win" else "🔴"
                print(f"  [平仓] {result.upper()} PnL={exit_trade['pnl']:.2f} USDT")

                if self.live_trade:
                    # 真实平仓
                    pos = tracker.position  # 已被 check_exit_bar 清空，但还有记录
                    # 从 exit_trade 重建平仓方向
                    side_for_close = exit_trade["side"]
                    try:
                        self._do_close_position(name, float(exit_trade["notional"]) / exit_trade["entry_price"] / tracker.ct_val,
                                                side_for_close)
                    except:
                        pass  # 可能已被止损止盈委托单自动平仓

                feishu_send(
                    f"{emoji} [{name}] 平仓 - {result.upper()}",
                    f"**币种**: {name}\n**方向**: {exit_trade['side'].upper()}\n"
                    f"**入场价**: {exit_trade['entry_price']}\n**出场价**: {exit_trade['exit_price']}\n"
                    f"**收益**: {exit_trade['net_return']*100:.4f}%\n"
                    f"**PnL**: {exit_trade['pnl']:.2f} USDT\n"
                    f"**名义价值**: {exit_trade['notional']:.2f} USDT",
                    color="green" if result == "win" else "red")
            else:
                print(f"  [持仓中] {tracker.position['side'].upper()} @ {tracker.position['entry_price']}")
            return

        # ── 信号检测 ──
        signal = tracker.check_fractal_signal(m30_df, h1_df)
        if signal is None:
            print(f"  [无信号]")
            return

        print(f"\n  🔔 [{name}] 信号: {signal['signal'].upper()}")
        print(f"     进场: {signal['entry_price']} | 止损: {signal['stop_loss']} | 止盈: {signal['take_profit']}")
        tracker.log_signal(signal)

        sig_emoji = "📈" if signal["signal"] == "long" else "📉"
        feishu_send(
            f"{sig_emoji} [{name}] 交易信号 - {signal['signal'].upper()}",
            f"**币种**: {name}\n**方向**: {signal['signal'].upper()}\n"
            f"**进场价**: {signal['entry_price']}\n**止损**: {signal['stop_loss']}\n"
            f"**止盈**: {signal['take_profit']}\n**盈亏比**: {self.rr}:1",
            color="yellow")

        # 动态仓位
        contracts = self.dynamic_position_size(name, signal["entry_price"])
        equity = self.capital
        margin_used = equity * self.margin_pct
        print(f"    最终仓位: {contracts} 张 (杠杆: {self.leverage}x)")

        if self.live_trade:
            # ── 模拟盘真实下单 ──
            try:
                order = self._do_open_position(name, signal, contracts)
                tracker.position = {
                    "side": signal["signal"], "entry_price": signal["entry_price"],
                    "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
                    "size": contracts, "entry_time": now.isoformat(),
                    "order_id": order.get("id", "unknown"),
                }
                tracker.trade_history.append({"action": "OPEN", **tracker.position, "asset": name})
                tracker.log_trade(tracker.trade_history[-1])
                self.daily_trades += 1

                feishu_send(
                    f"✅ [{name}] 已开仓 - {signal['signal'].upper()}",
                    f"**币种**: {name}\n**方向**: {signal['signal'].upper()}\n"
                    f"**数量**: {contracts} 张\n**入场价**: {signal['entry_price']}\n"
                    f"**止损**: {signal['stop_loss']}\n**止盈**: {signal['take_profit']}\n"
                    f"**保证金**: {margin_used:.2f} USDT\n**账户余额**: {equity:.2f} USDT",
                    color="blue")
            except Exception as e:
                print(f"  [{name}] ❌ 模拟盘下单失败: {e}")
                traceback.print_exc()
        else:
            # ── 本地模拟开仓 ──
            print(f"  [本地模拟] 开仓 {signal['signal'].upper()} {contracts}张 @ {signal['entry_price']}")
            tracker.position = {
                "side": signal["signal"], "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"], "take_profit": signal["take_profit"],
                "size": contracts, "entry_time": now.isoformat(),
                "order_id": "simulated",
            }
            tracker.trade_history.append({"action": "OPEN", **tracker.position, "asset": name})
            tracker.log_trade(tracker.trade_history[-1])
            self.daily_trades += 1

    # ═══════════════════════════════════════════════════════════
    # 调度
    # ═══════════════════════════════════════════════════════════

    def _smart_sleep(self, failed=False) -> float:
        if failed:
            return 60
        now = datetime.now(timezone.utc)
        next_10min = ((now.minute // 10) + 1) * 10
        extra = 0
        if next_10min >= 60:
            next_10min = 0; extra = 1
        target = now.replace(minute=next_10min, second=30, microsecond=0)
        if extra:
            target += timedelta(hours=1)
        if target <= now:
            target += timedelta(minutes=10)
        return max((target - now).total_seconds(), 5)

    def run(self):
        print(f"\n{'='*60}")
        print("OKX 统一交易机器人 ─ BTC + ETH")
        print(f"{'='*60}")
        print(f"下单模式: {'🔴 模拟盘真实下单' if self.live_trade else '🟡 本地模拟(不下单)'}")
        print(f"策略: 30m分型 + 1h共振 | RR={self.rr}")
        print(f"杠杆: {self.leverage}x 全仓 | 保证金: {self.margin_pct*100:.0f}%")
        print(f"调度: 每10分钟(00/10/20/30/40/50分+30秒)")
        print(f"飞书: {'已配置' if FEISHU_WEBHOOK else '未配置'}")
        print(f"{'='*60}\n")

        feishu_send(
            "🚀 交易机器人已启动",
            f"**模式**: {'模拟盘真实下单' if self.live_trade else '本地模拟'}\n"
            f"**监控**: BTC + ETH\n**杠杆**: {self.leverage}x | **保证金**: {self.margin_pct*100:.0f}%\n"
            f"**策略**: 30m分型 + 1h共振 | RR={self.rr}",
            color="blue")

        failed = False
        while True:
            try:
                self.run_cycle()
                failed = False
            except KeyboardInterrupt:
                print("\n\n🛑 用户中断")
                feishu_send("🛑 交易机器人已停止",
                            f"**原因**: 手动中断\n**最终余额**: {self.capital:.2f} USDT",
                            color="red")
                break
            except Exception as e:
                print(f"[严重错误] {e}")
                traceback.print_exc()
                feishu_send("⚠️ 交易机器人异常",
                            f"**错误**: {str(e)[:200]}\n**时间**: {datetime.now().strftime('%H:%M:%S')}",
                            color="red")
                failed = True

            wait = self._smart_sleep(failed=failed)
            next_time = datetime.now(timezone.utc) + timedelta(seconds=wait)
            print(f"\n⏳ 等待 {wait:.0f}秒... (下次: {next_time.strftime('%H:%M:%S')})")
            time.sleep(wait)


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OKX 统一交易机器人 - BTC+ETH 双币种")
    parser.add_argument("--trade", action="store_true",
                        help="模拟盘真实下单模式（设杠杆+开仓+平仓）")
    parser.add_argument("--rr", type=float, default=1.0)
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--leverage", type=int, default=100)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = parser.parse_args()

    trader = UnifiedTrader(
        rr=args.rr, margin_pct=args.margin, leverage=args.leverage,
        live_trade=args.trade,
        proxy=args.proxy,
    )
    trader.run()
