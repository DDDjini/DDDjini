import ccxt
import time

print("正在连接 OKX...")
try:
    exchange = ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "timeout": 15000,
    })
    print("加载 markets...")
    exchange.load_markets()
    print("OKX 连接成功")
    
    print("尝试获取 1 根 K 线...")
    rows = exchange.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=1)
    print(f"成功获取: {len(rows)} 行")
    print(rows[0])
except Exception as e:
    print(f"错误: {e}")
