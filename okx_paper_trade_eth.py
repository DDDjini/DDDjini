import ccxt
import pandas as pd
import numpy as np
import time
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

from backtest_fractal import add_fractals, ensure_dir

load_dotenv()


class OKXPaperTrader:
    """
    OKX 模拟盘自动交易机器人
    
    策略：30m 分型 + 1h 分型共振
    杠杆：100x 全仓
    仓位：每次 5% 资金做保证金
    
    模式：
    - paper_mode=True (默认): 本地模拟，不真正下单
    - paper_mode=False: 连接OKX模拟盘，真实下单（需确认）
    """

    def __init__(
        self,
        symbol: str = "ETH/USDT:USDT",
        timeframe: str = "30m",
        higher_tf: str = "1h",
        left: int = 5,
        right: int = 2,
        rr: float = 2.0,
        sl_buffer: float = 0.0005,
        fee_rate: float = 0.0005,
        proxy: str = "http://127.0.0.1:7897",
        paper_mode: bool = True,
        capital: float = 10000.0,
        margin_pct: float = 0.05,      # 每次 5% 资金做保证金
        leverage: int = 100,            # 100倍杠杆
        log_dir: str = "results/paper_trading_eth",
        max_stop_pts: float = 50.0,   # ETH 最大止损 50 点
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.higher_tf = higher_tf
        self.left = left
        self.right = right
        self.rr = rr
        self.sl_buffer = sl_buffer
        self.fee_rate = fee_rate
        self.paper_mode = paper_mode
        self.capital = capital
        self.initial_capital = capital
        self.margin_pct = margin_pct
        self.leverage = leverage
        self.proxy = proxy
        self.log_dir = log_dir
        self.max_stop_pts = max_stop_pts

        ensure_dir(log_dir)

        # 初始化交易所
        api_key = os.getenv("OKX_API_KEY")
        api_secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_PASSPHRASE")

        print(f"API Key 状态: {'已配置' if api_key else '未配置'}")
        print(f"API Secret 状态: {'已配置' if api_secret else '未配置'}")
        print(f"Passphrase 状态: {'已配置' if passphrase else '未配置'}")

        config = {
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap"},
        }
        if api_key and api_secret and passphrase:
            config.update({
                "apiKey": api_key,
                "secret": api_secret,
                "password": passphrase,
            })
        if proxy:
            config["proxies"] = {"http": proxy, "https": proxy}

        self.exchange = ccxt.okx(config)
        
        # 启用 sandbox 模式（模拟盘）
        if paper_mode:
            self.exchange.set_sandbox_mode(True)
            print("\n[模式] OKX Sandbox 模拟盘模式已启用")
        else:
            print("\n[警告] 实盘模式！")

        # 获取合约面值
        self._load_contract_info()

        # 状态跟踪
        self.position = None
        self.trade_history = []
        self.signal_log = []
        self.last_fractal_bar = -1
        self.daily_trades = 0
        self.current_day = datetime.now(timezone.utc).date()
        self.peak_capital = capital
        self.max_dd = 0.0

        # 日志文件
        self.signal_log_file = Path(log_dir) / "signals.jsonl"
        self.trade_file = Path(log_dir) / "trades.jsonl"
        self.balance_log = Path(log_dir) / "balance.jsonl"

    def _load_contract_info(self):
        """加载合约信息（面值、最小下单量等）"""
        try:
            self.exchange.load_markets()
            market = self.exchange.market(self.symbol)
            self.ct_val = float(market.get("contractSize", 1))  # 每张合约面值
            self.min_contracts = float(market.get("limits", {}).get("amount", {}).get("min", 0.01))
            print(f"\n合约信息:")
            print(f"  交易对: {self.symbol}")
            print(f"  合约面值: {self.ct_val} BTC/张")
            print(f"  最小下单量: {self.min_contracts} 张")
            print(f"  杠杆倍数: {self.leverage}x")
            print(f"  保证金比例: {self.margin_pct*100}%")
        except Exception as e:
            print(f"加载合约信息失败: {e}")
            self.ct_val = 0.01  # 默认值
            self.min_contracts = 0.01

    def log_signal(self, signal: dict):
        signal["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.signal_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(signal, default=str) + "\n")

    def log_trade(self, trade: dict):
        trade["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.trade_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, default=str) + "\n")

    def log_balance(self, balance: dict):
        balance["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.balance_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(balance, default=str) + "\n")

    def fetch_data(self, tf: str, limit: int = 100) -> pd.DataFrame:
        try:
            rows = self.exchange.fetch_ohlcv(self.symbol, tf, limit=limit)
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")

            # 丢弃最后一根未收盘K线，避免分型重绘
            if len(df) > 1:
                df = df.iloc[:-1].reset_index(drop=True)

            return df
        except Exception as e:
            print(f"  [错误] 获取{tf}数据失败: {e}")
            return None
        try:
            rows = self.exchange.fetch_ohlcv(self.symbol, tf, limit=limit)
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df
        except Exception as e:
            print(f"  [错误] 获取{tf}数据失败: {e}")
            return None

    def get_balance(self) -> dict:
        """获取账户余额"""
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            return {
                "total": float(usdt.get("total", 0)),
                "free": float(usdt.get("free", 0)),
                "used": float(usdt.get("used", 0)),
            }
        except Exception as e:
            print(f"  [错误] 获取余额失败: {e}")
            return None

    def set_leverage(self, leverage: int = 100):
        """设置杠杆倍数"""
        try:
            # OKX 设置杠杆 API
            params = {
                "instId": self.symbol.replace("/", "-").replace(":", "-"),
                "lever": str(leverage),
                "mgnMode": "cross",  # 全仓
            }
            result = self.exchange.set_leverage(leverage, self.symbol, params=params)
            print(f"  杠杆设置: {leverage}x 全仓")
            return result
        except Exception as e:
            print(f"  [警告] 设置杠杆失败: {e}")
            return None

    def get_position(self) -> dict:
        """获取当前持仓"""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            for pos in positions:
                if pos.get("symbol") == self.symbol:
                    contracts = float(pos.get("contracts", 0))
                    if abs(contracts) > 0:
                        # 确保 posSide 字段存在
                        if not pos.get("posSide"):
                            pos["posSide"] = pos.get("side", "long")
                        return pos
            return None
        except Exception as e:
            print(f"  [错误] 获取持仓失败: {e}")
            return None

    def check_fractal_signal(self, m30_df: pd.DataFrame, h1_df: pd.DataFrame) -> dict:
        """
        检测最新分型+共振信号（只检查刚确认的分型，不扫描历史）
        """
        n = len(m30_df)
        if n < self.left + self.right + 2:
            return None

        m30_df = add_fractals(m30_df.copy(), self.left, self.right)
        h1_df = add_fractals(h1_df.copy(), 2, 2)

        # 只检查最新一个分型：当前K线刚刚确认的（i = n-1）
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

        # 多周期共振：取当前时间之前的1h K线
        current_ts = m30_df.loc[pivot_idx, "timestamp"]
        h1_subset = h1_df[h1_df["timestamp"] <= current_ts]
        if len(h1_subset) < 5:
            return None

        if signal == "long":
            if not h1_subset["fractal_low"].any():
                return None
        else:
            if not h1_subset["fractal_high"].any():
                return None

        # 进场价 = 分型确认后的下一根K线开盘价（即当前K线）
        entry_idx = i
        entry_price = m30_df.loc[entry_idx, "close"]
        entry_time = m30_df.loc[entry_idx, "datetime"]

        if signal == "long":
            pivot_low = m30_df.loc[pivot_idx, "low"]
            sl = pivot_low * (1 - self.sl_buffer)
            risk = entry_price - sl
            if risk <= 0:
                return None
            # 最大止损截断（点数限制）
            if hasattr(self, 'max_stop_pts') and self.max_stop_pts:
                if risk > self.max_stop_pts:
                    risk = self.max_stop_pts
                    sl = entry_price - self.max_stop_pts
            tp = entry_price + self.rr * risk
        else:
            pivot_high = m30_df.loc[pivot_idx, "high"]
            sl = pivot_high * (1 + self.sl_buffer)
            risk = sl - entry_price
            if risk <= 0:
                return None
            # 最大止损截断（点数限制）
            if hasattr(self, 'max_stop_pts') and self.max_stop_pts:
                if risk > self.max_stop_pts:
                    risk = self.max_stop_pts
                    sl = entry_price + self.max_stop_pts
            tp = entry_price - self.rr * risk

        self.last_fractal_bar = pivot_idx
        return {
            "signal": signal,
            "entry_price": round(entry_price, 2),
            "entry_time": entry_time.isoformat(),
            "stop_loss": round(sl, 2),
            "take_profit": round(tp, 2),
            "pivot_idx": int(pivot_idx),
        }

    def calculate_position_size(self, entry_price: float) -> float:
        """
        计算下单张数
        5%资金做保证金，100倍杠杆
        名义价值 = 保证金 * 杠杆 = 资金 * 5% * 100 = 资金 * 5
        张数 = 名义价值 / (entry_price * ct_val)
        """
        margin = self.capital * self.margin_pct
        notional = margin * self.leverage
        contracts = notional / (entry_price * self.ct_val)
        contracts = max(contracts, self.min_contracts)
        return round(contracts, 2)

    def close_position(self, pos: dict):
        """平仓"""
        pos_side = pos.get("posSide", pos.get("side", "long"))
        contracts = float(pos.get("contracts", 0))
        
        close_side = "sell" if pos_side == "long" else "buy"
        
        if not self.paper_mode:
            print("  [警告] 实盘平仓！")
            confirm = input("  确认平仓? (yes/no): ")
            if confirm != "yes":
                return None
        
        try:
            print(f"  [下单] 平仓: {close_side} {contracts} 张 ({pos_side})")
            order = self.exchange.create_market_order(
                symbol=self.symbol,
                side=close_side,
                amount=contracts,
                params={"tdMode": "cross", "posSide": pos_side, "reduceOnly": True}
            )
            print(f"  订单: {order.get('id', 'N/A')}")
            return order
        except Exception as e:
            print(f"  [错误] 平仓失败: {e}")
            return None

    def open_position(self, signal: dict):
        """开仓"""
        side = signal["signal"]
        entry_price = signal["entry_price"]
        pos_side = "long" if side == "long" else "short"
        
        contracts = self.calculate_position_size(entry_price)
        if contracts <= 0:
            print("  [跳过] 计算仓位为零")
            return

        order_side = "buy" if side == "long" else "sell"
        
        # 附带止损止盈参数
        params = {
            "tdMode": "cross",
            "posSide": pos_side,
            "attachAlgoOrds": [
                {
                    "tpTriggerPx": str(signal["take_profit"]),
                    "tpOrdPx": "-1",
                    "slTriggerPx": str(signal["stop_loss"]),
                    "slOrdPx": "-1",
                    "sz": str(contracts),
                    "posSide": pos_side,
                }
            ]
        }

        if not self.paper_mode:
            print("  [警告] 实盘开仓！")
            confirm = input("  确认开仓? (yes/no): ")
            if confirm != "yes":
                return None
        
        try:
            print(f"  [下单] 开仓: {order_side} {contracts} 张 ({pos_side}) @ {entry_price}")
            print(f"    SL: {signal['stop_loss']} | TP: {signal['take_profit']}")
            
            order = self.exchange.create_market_order(
                symbol=self.symbol,
                side=order_side,
                amount=contracts,
                params=params
            )
            print(f"  订单: {order.get('id', 'N/A')}")
            return order
        except Exception as e:
            print(f"  [错误] 开仓失败: {e}")
            return None

    def check_exit(self, current_price: float):
        """检查持仓是否触发止损/止盈"""
        if self.position is None:
            return

        pos = self.position
        side = pos["side"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]
        entry = pos["entry_price"]

        exited = False
        exit_price = None
        result = None

        if side == "long":
            if current_price <= sl:
                exited = True
                exit_price = sl
                result = "loss"
            elif current_price >= tp:
                exited = True
                exit_price = tp
                result = "win"
        else:
            if current_price >= sl:
                exited = True
                exit_price = sl
                result = "loss"
            elif current_price <= tp:
                exited = True
                exit_price = tp
                result = "win"

        if exited:
            # 计算收益
            if side == "long":
                gross_ret = (exit_price - entry) / entry
            else:
                gross_ret = (entry - exit_price) / entry
            net_ret = gross_ret - self.fee_rate * 2
            pnl = net_ret * entry * pos["size"]
            self.capital += pnl

            if self.capital > self.peak_capital:
                self.peak_capital = self.capital
            dd = (self.peak_capital - self.capital) / self.peak_capital
            if dd > self.max_dd:
                self.max_dd = dd

            trade = {
                "action": "CLOSE",
                "side": side,
                "entry_price": entry,
                "exit_price": exit_price,
                "result": result,
                "net_return": net_ret,
                "pnl": pnl,
                "capital_after": self.capital,
                "exit_time": datetime.now(timezone.utc).isoformat(),
            }
            self.trade_history.append(trade)
            self.log_trade(trade)
            print(f"  [平仓] {result.upper()} 收益: {net_ret*100:.4f}% 资金: {self.capital:.2f}")
            self.position = None

    def check_exit_bar(self, bar: pd.Series):
        """使用已收盘K线高低点检查是否触发止盈/止损"""
        if self.position is None:
            return

        pos = self.position
        side = pos["side"]
        sl = pos["stop_loss"]
        tp = pos["take_profit"]
        entry = pos["entry_price"]
        high = float(bar["high"])
        low = float(bar["low"])

        exited = False
        exit_price = None
        result = None

        if side == "long":
            hit_sl = low <= sl
            hit_tp = high >= tp

            # 同一根K线同时碰到止盈止损，保守按止损算
            if hit_sl and hit_tp:
                exited = True
                exit_price = sl
                result = "loss"
            elif hit_sl:
                exited = True
                exit_price = sl
                result = "loss"
            elif hit_tp:
                exited = True
                exit_price = tp
                result = "win"

        else:
            hit_sl = high >= sl
            hit_tp = low <= tp

            if hit_sl and hit_tp:
                exited = True
                exit_price = sl
                result = "loss"
            elif hit_sl:
                exited = True
                exit_price = sl
                result = "loss"
            elif hit_tp:
                exited = True
                exit_price = tp
                result = "win"

        if not exited:
            return

        if side == "long":
            gross_ret = (exit_price - entry) / entry
        else:
            gross_ret = (entry - exit_price) / entry

        net_ret = gross_ret - self.fee_rate * 2

        # 合约名义价值 = 入场价 * 合约面值 * 张数
        notional = entry * pos["size"] * self.ct_val
        pnl = net_ret * notional

        self.capital += pnl

        if self.capital > self.peak_capital:
            self.peak_capital = self.capital

        dd = (self.peak_capital - self.capital) / self.peak_capital
        if dd > self.max_dd:
            self.max_dd = dd

        trade = {
            "action": "CLOSE",
            "side": side,
            "entry_price": entry,
            "exit_price": exit_price,
            "result": result,
            "gross_return": gross_ret,
            "net_return": net_ret,
            "notional": notional,
            "pnl": pnl,
            "capital_after": self.capital,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "bar_time": str(bar["datetime"]),
        }

        self.trade_history.append(trade)
        self.log_trade(trade)

        print(f"  [本地模拟平仓] {result.upper()}")
        print(f"    入场: {entry}")
        print(f"    出场: {exit_price}")
        print(f"    名义价值: {notional:.2f} USDT")
        print(f"    PnL: {pnl:.2f} USDT")
        print(f"    资金: {self.capital:.2f}")

        self.position = None

    def run_cycle(self):
        """运行一次检测循环"""
        now = datetime.now(timezone.utc)
        print(f"\n{'='*60}")
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 检测信号")

        # 日计数重置
        if now.date() != self.current_day:
            self.current_day = now.date()
            self.daily_trades = 0

        # 获取余额（模拟盘模式下）
        if not self.paper_mode:
            balance = self.get_balance()
            if balance:
                self.capital = balance["free"]  # 使用可用余额
                print(f"  模拟盘余额: {balance['total']:.2f} USDT (可用: {balance['free']:.2f})")
                self.log_balance(balance)

        # 获取数据
        m30_df = self.fetch_data(self.timeframe, limit=100)
        h1_df = self.fetch_data(self.higher_tf, limit=50)

        if m30_df is None or h1_df is None:
            print("  [跳过] 数据获取失败")
            return

        latest_price = m30_df["close"].iloc[-1]
        print(f"  最新价格: {latest_price:.2f}")
        print(f"  30m K线: {m30_df['datetime'].iloc[-1]}")
        print(f"  1h  K线: {h1_df['datetime'].iloc[-1]}")

        # 检查持仓（非模拟模式下）
        if not self.paper_mode:
            real_pos = self.get_position()
            if real_pos:
                print(f"  模拟盘持仓: {real_pos['side']} {real_pos['contracts']} 张, 开仓价: {real_pos['entryPrice']}")
            else:
                print("  模拟盘无持仓")

        # 本地模拟持仓检查 - 使用已收盘K线
        if self.position:
            latest_bar = m30_df.iloc[-1]
            self.check_exit_bar(latest_bar)

        # 检查持仓（非模拟模式下）
        if not self.paper_mode and self.get_position():
            print("  [跳过] 已有持仓，等待平仓")
            return
        if self.position:
            print("  [跳过] 本地模拟已有持仓")
            return

        signal = self.check_fractal_signal(m30_df, h1_df)
        if signal is None:
            print("  [无信号]")
            return

        print(f"\n  [信号] {signal['signal'].upper()}")
        print(f"    进场: {signal['entry_price']}")
        print(f"    止损: {signal['stop_loss']}")
        print(f"    止盈: {signal['take_profit']}")
        self.log_signal(signal)

        # 计算仓位
        contracts = self.calculate_position_size(signal["entry_price"])
        print(f"    计算仓位: {contracts} 张 (5%保证金 * {self.leverage}x杠杆)")

        # 开仓
        order = self.open_position(signal)
        if order:
            self.position = {
                "side": signal["signal"],
                "entry_price": signal["entry_price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "size": contracts,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "order_id": order.get("id", "unknown"),
            }
            self.trade_history.append({
                "action": "OPEN",
                **self.position,
                "capital_before": self.capital,
            })
            self.log_trade(self.trade_history[-1])
            self.daily_trades += 1

        # 打印状态
        print(f"\n  [状态] 资金: {self.capital:.2f} | 收益: {(self.capital/self.initial_capital-1)*100:.2f}%")
        print(f"  [状态] 最大回撤: {self.max_dd*100:.2f}% | 交易: {len(self.trade_history)}")

    def _smart_sleep_seconds(self, failed: bool = False) -> float:
        if failed:
            return 60
        now = datetime.now(timezone.utc)
        current_min = now.minute
        next_10min = ((current_min // 10) + 1) * 10
        extra_hours = 0
        if next_10min >= 60:
            next_10min = 0
            extra_hours = 1
        target = now.replace(minute=next_10min, second=30, microsecond=0)
        if extra_hours > 0:
            target += __import__('datetime').timedelta(hours=extra_hours)
        if target <= now:
            target += __import__('datetime').timedelta(minutes=10)
        wait = (target - now).total_seconds()
        return max(wait, 5)

    def run(self, interval: int = 60):
        print(f"\n{'='*60}")
        print("OKX 模拟盘自动交易机器人")
        print(f"{'='*60}")
        print(f"模式: {'本地模拟' if self.paper_mode else 'OKX模拟盘真实下单'}")
        print(f"交易对: {self.symbol}")
        print(f"策略: 30m分型 + 1h分型共振")
        print(f"参数: left={self.left}, right={self.right}, RR={self.rr}")
        print(f"最大止损: {self.max_stop_pts} 点")
        print(f"杠杆: {self.leverage}x 全仓")
        print(f"保证金: {self.margin_pct*100}% 资金")
        print(f"初始资金: {self.initial_capital:.2f} USDT")
        print(f"调度: 每10分钟检测（00/10/20/30/40/50分 + 30秒）")
        print(f"{'='*60}\n")

        # 设置杠杆
        if not self.paper_mode:
            self.set_leverage(self.leverage)

        failed = False
        while True:
            try:
                self.run_cycle()
                failed = False
            except KeyboardInterrupt:
                print("\n\n用户中断")
                break
            except Exception as e:
                print(f"[错误] {e}")
                import traceback
                traceback.print_exc()
                failed = True
            
            wait = self._smart_sleep_seconds(failed=failed)
            td = __import__('datetime').timedelta(seconds=wait)
            next_time = datetime.now(timezone.utc) + td
            print(f"\n等待 {wait:.0f} 秒... (下次检测: {next_time.strftime('%H:%M:%S')})")
            time.sleep(wait)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ETH/USDT:USDT")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--left", type=int, default=5)
    parser.add_argument("--right", type=int, default=2)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--sl-buffer", type=float, default=0.0005)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--margin", type=float, default=0.05, help="每次保证金比例 (0.05=5%%)")
    parser.add_argument("--leverage", type=int, default=100, help="杠杆倍数")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--live", action="store_true", help="切换到OKX实盘模式(危险!)")
    args = parser.parse_args()

    ensure_dir("results/paper_trading_eth")

    trader = OKXPaperTrader(
        symbol=args.symbol,
        left=args.left,
        right=args.right,
        rr=args.rr,
        sl_buffer=args.sl_buffer,
        proxy=args.proxy,
        paper_mode=not args.live,
        capital=args.capital,
        margin_pct=args.margin,
        leverage=args.leverage,
    )

    trader.run()
