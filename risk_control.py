import pandas as pd
import numpy as np
from datetime import datetime, timezone


class RiskController:
    """
    风控模块：
    1. 单笔最大止损（占总资金比例）
    2. 日最大亏损（占日初始资金比例，触发后停止当日交易）
    3. 连续亏损暂停（连续 N 笔亏损后，暂停 M 根 K 线）
    4. 最大回撤风控（回撤超过阈值则停止交易）
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        max_loss_per_trade: float = 0.02,       # 单笔最大亏损 2%
        max_daily_loss: float = 0.05,              # 日最大亏损 5%
        max_consecutive_losses: int = 3,           # 连续亏损 N 笔暂停
        pause_after_losses: int = 5,              # 暂停 N 根 K 线
        max_drawdown_limit: float = 0.15,         # 最大回撤 15%
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.max_loss_per_trade = max_loss_per_trade
        self.max_daily_loss = max_daily_loss
        self.max_consecutive_losses = max_consecutive_losses
        self.pause_after_losses = pause_after_losses
        self.max_drawdown_limit = max_drawdown_limit

        # 状态跟踪
        self.daily_start_capital = initial_capital
        self.daily_loss = 0.0
        self.current_day = None
        self.consecutive_losses = 0
        self.pause_until_bar = 0  # 暂停直到第几根 K 线
        self.trade_count = 0
        self.peak_capital = initial_capital
        self.trade_history = []
        self.blocked_reason = None

    def reset_day(self, bar_idx: int, bar_datetime: datetime):
        """新的一天开始时重置日统计"""
        day = bar_datetime.date()
        if self.current_day != day:
            self.current_day = day
            self.daily_start_capital = self.capital
            self.daily_loss = 0.0

    def can_trade(self, bar_idx: int, bar_datetime: datetime) -> bool:
        """检查当前是否可以开仓"""
        self.reset_day(bar_idx, bar_datetime)

        # 1. 连续亏损暂停
        if bar_idx < self.pause_until_bar:
            self.blocked_reason = f"连续亏损暂停中（暂停到第{self.pause_until_bar}根K线）"
            return False

        # 2. 日最大亏损
        if self.daily_loss <= -self.max_daily_loss * self.daily_start_capital:
            self.blocked_reason = f"日最大亏损已触发（{self.daily_loss/self.daily_start_capital*100:.2f}%）"
            return False

        # 3. 最大回撤
        current_drawdown = (self.peak_capital - self.capital) / self.peak_capital
        if current_drawdown >= self.max_drawdown_limit:
            self.blocked_reason = f"最大回撤已触发（{current_drawdown*100:.2f}%）"
            return False

        self.blocked_reason = None
        return True

    def position_size(self, entry_price: float, stop_loss: float) -> float:
        """
        根据单笔最大止损计算仓位大小。
        返回：下单数量（合约张数 / 合约数量）
        """
        risk = abs(entry_price - stop_loss) / entry_price
        if risk <= 0:
            return 0

        max_risk_amount = self.capital * self.max_loss_per_trade
        position_value = max_risk_amount / risk
        # 简化：假设每张合约 = 1 USDT 面值，数量 = position_value / entry_price
        position_size = position_value / entry_price
        return max(position_size, 0)

    def record_trade(self, bar_idx: int, net_return: float, gross_pnl: float = None):
        """记录交易结果，更新风控状态"""
        self.trade_count += 1
        capital_before = self.capital
        self.capital *= (1 + net_return)
        
        # 用百分比亏损计算日亏损（更合理）
        if gross_pnl is not None:
            self.daily_loss += gross_pnl
        else:
            self.daily_loss += net_return * capital_before

        # 更新峰值和回撤
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital
        # 同时记录当前回撤
        current_dd = (self.peak_capital - self.capital) / self.peak_capital
        if current_dd > getattr(self, '_max_dd', 0):
            self._max_dd = current_dd

        # 连续亏损统计
        if net_return < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.pause_until_bar = bar_idx + self.pause_after_losses
        else:
            self.consecutive_losses = 0

        self.trade_history.append({
            "trade_no": self.trade_count,
            "bar_idx": bar_idx,
            "net_return": net_return,
            "capital_before": capital_before,
            "capital_after": self.capital,
            "daily_loss": self.daily_loss,
            "consecutive_losses": self.consecutive_losses,
            "paused": self.consecutive_losses >= self.max_consecutive_losses,
        })

    def get_stats(self) -> dict:
        """返回风控统计"""
        return {
            "initial_capital": self.initial_capital,
            "final_capital": self.capital,
            "total_return": (self.capital - self.initial_capital) / self.initial_capital,
            "max_drawdown": (self.peak_capital - min(self.capital, self.peak_capital)) / self.peak_capital,
            "trade_count": self.trade_count,
            "consecutive_losses": self.consecutive_losses,
            "pause_until_bar": self.pause_until_bar,
            "blocked_reason": self.blocked_reason,
        }


class AIFilter:
    """
    AI 分析模块（技术指标多因子过滤）
    在分型信号基础上叠加技术指标过滤，提高信号质量。
    """

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()
        self._calc_indicators()

    def _calc_indicators(self):
        """计算技术指标"""
        close = self.df["close"]
        high = self.df["high"]
        low = self.df["low"]

        # 1. PSY心理线(12) - 统计N根K线中上涨K线的比例
        # PSY = 上涨K线数 / N * 100
        up_bar = (close > close.shift(1)).astype(int)
        self.df["psy"] = up_bar.rolling(12).mean() * 100

        # 2. MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        self.df["macd"] = ema12 - ema26
        self.df["macd_signal"] = self.df["macd"].ewm(span=9, adjust=False).mean()
        self.df["macd_hist"] = self.df["macd"] - self.df["macd_signal"]

        # 3. 布林带
        self.df["bb_mid"] = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        self.df["bb_upper"] = self.df["bb_mid"] + 2 * bb_std
        self.df["bb_lower"] = self.df["bb_mid"] - 2 * bb_std
        self.df["bb_width"] = (self.df["bb_upper"] - self.df["bb_lower"]) / self.df["bb_mid"]
        self.df["bb_position"] = (close - self.df["bb_lower"]) / (self.df["bb_upper"] - self.df["bb_lower"])

        # 4. ATR(14) 波动率
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        self.df["atr"] = tr.rolling(14).mean()
        self.df["atr_pct"] = self.df["atr"] / close

        # 5. 均线趋势
        self.df["ema_20"] = close.ewm(span=20, adjust=False).mean()
        self.df["ema_50"] = close.ewm(span=50, adjust=False).mean()
        self.df["trend_up"] = self.df["ema_20"] > self.df["ema_50"]

    def filter_signal(self, idx: int, signal: str) -> tuple[bool, str]:
        """
        过滤交易信号。返回 (允许, 原因)
        """
        if idx < 50:  # 前50根K线指标不稳定
            return True, "指标未成熟"

        row = self.df.iloc[idx]

        # PSY心理线过滤
        psy = row["psy"]
        if signal == "long" and psy > 75:
            return False, f"PSY过热({psy:.1f})，情绪过于一致不宜追多"
        if signal == "short" and psy < 25:
            return False, f"PSY过冷({psy:.1f})，情绪过于一致不宜追空"

        # MACD 过滤
        macd_hist = row["macd_hist"]
        if signal == "long" and macd_hist < 0:
            return False, f"MACD负值({macd_hist:.2f})，趋势向下"
        if signal == "short" and macd_hist > 0:
            return False, f"MACD正值({macd_hist:.2f})，趋势向上"

        # 布林带过滤
        bb_pos = row["bb_position"]
        if signal == "long" and bb_pos > 0.8:
            return False, f"价格靠近布林上轨({bb_pos:.2f})，不宜追多"
        if signal == "short" and bb_pos < 0.2:
            return False, f"价格靠近布林下轨({bb_pos:.2f})，不宜追空"

        # 波动率过滤（ATR 过大时避免交易）
        atr_pct = row["atr_pct"]
        if atr_pct > 0.03:  # ATR > 3%
            return False, f"波动率过高(ATR={atr_pct*100:.2f}%)，风险过大"

        return True, "通过AI过滤"

    def get_df(self) -> pd.DataFrame:
        return self.df
