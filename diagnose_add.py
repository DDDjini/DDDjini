"""
加仓问题诊断脚本：检查实盘账户状态，定位"显示已加仓但仓位没变"的原因
"""
import ccxt
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET = os.getenv("OKX_API_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
PROXY = os.getenv("OKX_PROXY", "")

cfg = {
    "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
    "enableRateLimit": True, "timeout": 30000,
    "options": {"defaultType": "swap"},
}
if PROXY:
    cfg["proxies"] = {"http": PROXY, "https": PROXY}

ex = ccxt.okx(cfg)
ex.set_sandbox_mode(False)
ex.load_markets()

print("=" * 60)
print("1. 账户配置（持仓模式）")
print("=" * 60)
try:
    acct = ex.private_get_account_config()
    data = acct.get("data", [])
    if data:
        d = data[0]
        print(f"  持仓模式 posMode: {d.get('posMode')}  (long_short_mode=双向 / net_mode=单向)")
        print(f"  账户层级 acctLv: {d.get('acctLv')}")
        print(f"  合约模式: {d.get('uid')}")
except Exception as e:
    print(f"  查询失败: {e}")

print("\n" + "=" * 60)
print("2. 当前持仓")
print("=" * 60)
for inst, sym in [("BTC-USDT-SWAP", "BTC/USDT:USDT"), ("ETH-USDT-SWAP", "ETH/USDT:USDT")]:
    try:
        positions = ex.fetch_positions([sym])
        found = False
        for p in positions:
            contracts = float(p.get("contracts", 0) or 0)
            if abs(contracts) > 0:
                found = True
                print(f"  {inst}:")
                print(f"    张数 contracts = {contracts}")
                print(f"    方向 side = {p.get('side')}")
                print(f"    posSide = {p.get('posSide')}")
                print(f"    入场价 entryPrice = {p.get('entryPrice')}")
                print(f"    未实现盈亏 = {p.get('unrealizedPnl')}")
        if not found:
            print(f"  {inst}: 无持仓")
    except Exception as e:
        print(f"  {inst} 查询失败: {e}")

print("\n" + "=" * 60)
print("3. 挂单（止盈止损 algo 单）")
print("=" * 60)
for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
    try:
        algos = ex.private_get_trade_orders_algo_pending({"instId": inst})
        data = algos.get("data", []) if isinstance(algos, dict) else []
        print(f"  {inst}: {len(data)} 个挂单")
        for a in data:
            print(f"    algoId={a.get('algoId')} 类型={a.get('ordType')} "
                  f"方向={a.get('side')} posSide={a.get('posSide')} "
                  f"数量={a.get('sz')} TP={a.get('tpTriggerPx')} SL={a.get('slTriggerPx')}")
    except Exception as e:
        print(f"  {inst} 查询失败: {e}")

print("\n" + "=" * 60)
print("4. 最近成交记录（看加仓订单有没有成交）")
print("=" * 60)
for inst, sym in [("BTC", "BTC/USDT:USDT"), ("ETH", "ETH/USDT:USDT")]:
    try:
        trades = ex.fetch_my_trades(sym, limit=15)
        print(f"  {inst} 最近 {len(trades)} 笔成交:")
        for t in trades:
            side_cn = "买入" if t["side"] == "buy" else "卖出"
            print(f"    {t['datetime']} {side_cn} {t['amount']} @ {t['price']} 订单号={t.get('order')}")
    except Exception as e:
        print(f"  {inst} 成交查询失败: {e}")

print("\n" + "=" * 60)
print("诊断完成")
print("=" * 60)
