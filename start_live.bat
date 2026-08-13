@echo off
chcp 65001 >nul
echo ===============================================================
echo   OKX 实盘交易机器人
echo   策略: 分型(5,2) + 1h共振 + RR=1:1 + 100x杠杆
echo ===============================================================
echo.

cd /d "%~dp0"

REM 检查 .env 文件
if not exist ".env" (
    echo ❌ 缺少 .env 文件！请先配置 API 密钥。
    pause
    exit /b 1
)

REM 激活虚拟环境
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    echo ✅ 虚拟环境已激活
) else (
    echo ⚠️ 未找到虚拟环境，使用系统 Python
)

echo.
echo 🚀 启动交易机器人...
echo 💡 持续运行中，按 Ctrl+C 停止
echo.

python live_trader.py

pause
