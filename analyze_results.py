import pandas as pd
import numpy as np

def analyze(trades_df, name):
    if trades_df.empty:
        return None
    trades = trades_df.copy()
    trades['equity'] = (1 + trades['net_return']).cumprod()
    total = len(trades)
    wins = len(trades[trades['net_return'] > 0])
    losses = total - wins
    win_rate = wins / total
    total_return = trades['equity'].iloc[-1] - 1
    avg_return = trades['net_return'].mean()
    gross_profit = trades.loc[trades['net_return'] > 0, 'net_return'].sum()
    gross_loss = abs(trades.loc[trades['net_return'] < 0, 'net_return'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    rolling_max = trades['equity'].cummax()
    drawdown = trades['equity'] / rolling_max - 1
    max_drawdown = drawdown.min()
    
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    trades['exit_time'] = pd.to_datetime(trades['exit_time'])
    avg_duration = (trades['exit_time'] - trades['entry_time']).mean()
    
    longs = trades[trades['side'] == 'long']
    shorts = trades[trades['side'] == 'short']
    
    return {
        'name': name,
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'total_return': total_return,
        'avg_return': avg_return,
        'profit_factor': profit_factor,
        'max_drawdown': max_drawdown,
        'avg_duration': avg_duration,
        'long_trades': len(longs),
        'long_winrate': len(longs[longs['net_return'] > 0]) / len(longs) if len(longs) > 0 else 0,
        'short_trades': len(shorts),
        'short_winrate': len(shorts[shorts['net_return'] > 0]) / len(shorts) if len(shorts) > 0 else 0,
    }

results = []
for tf in ['30m', '1h', '4h', '1d']:
    df = pd.read_csv(f'D:/okx_ai_agent/results/trades_{tf}.csv')
    stats = analyze(df, f'基础分型-{tf}')
    if stats:
        results.append(stats)

df_all = pd.read_csv('D:/okx_ai_agent/results/trades_all_timeframes.csv')
stats = analyze(df_all, '基础分型-全周期合并')
if stats:
    results.append(stats)

df_main = pd.read_csv('D:/okx_ai_agent/results/main_strategy_trades.csv')
stats = analyze(df_main, '主策略(分型+共振+AI+风控)')
if stats:
    results.append(stats)

print('=' * 90)
print(f"{'策略':<30} {'交易数':>6} {'胜率':>8} {'累计收益':>10} {'盈亏比':>8} {'最大回撤':>10}")
print('=' * 90)
for r in results:
    print(f"{r['name']:<30} {r['trades']:>6} {r['win_rate']*100:>7.2f}% {r['total_return']*100:>9.2f}% {r['profit_factor']:>8.3f} {r['max_drawdown']*100:>9.2f}%")

print()
print('=' * 90)
print('详细多空分析')
print('=' * 90)
for r in results:
    print(f"\n{r['name']}:")
    print(f"  多头: {r['long_trades']}笔, 胜率 {r['long_winrate']*100:.2f}%")
    print(f"  空头: {r['short_trades']}笔, 胜率 {r['short_winrate']*100:.2f}%")
    print(f"  平均持仓: {r['avg_duration']}")
