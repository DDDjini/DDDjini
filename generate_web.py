"""
生成 RR=1 交互式回测网页
"""
import json
from pathlib import Path


def generate_html():
    # 加载 RR=1 回测数据
    with open("results/rr1_BTC_web_data.json", "r", encoding="utf-8") as f:
        btc_data = json.load(f)
    with open("results/rr1_ETH_web_data.json", "r", encoding="utf-8") as f:
        eth_data = json.load(f)

    # 压缩确保精度
    for data in [btc_data, eth_data]:
        for t in data["trades"]:
            t["entry_price"] = round(t["entry_price"], 2)
            t["exit_price"] = round(t["exit_price"], 2)
            t["stop_loss"] = round(t["stop_loss"], 2)
            t["take_profit"] = round(t["take_profit"], 2)
            t["net_return"] = round(t["net_return"], 6)
            t["account_return"] = round(t["account_return"], 6)
        for k in data["kline"]:
            k["open"] = round(k["open"], 2)
            k["high"] = round(k["high"], 2)
            k["low"] = round(k["low"], 2)
            k["close"] = round(k["close"], 2)

    btc_json = json.dumps(btc_data, ensure_ascii=False)
    eth_json = json.dumps(eth_data, ensure_ascii=False)

    html_template = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>币圈策略回测系统 RR=1 - 分型+共振+冷却</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f6fa;color:#2d3436}
.header{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);color:#fff;padding:18px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.header h1{font-size:20px;font-weight:600}
.header .subtitle{font-size:12px;opacity:.85;line-height:1.6}
.header .badge{display:inline-block;background:rgba(255,255,255,0.15);padding:3px 10px;border-radius:12px;font-size:12px;margin-right:6px}
.tab-bar{display:flex;gap:6px;padding:12px 24px;background:#fff;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}
.tab-btn{padding:8px 20px;border:1px solid #ddd;border-radius:8px;background:#fff;cursor:pointer;font-size:14px;font-weight:500;transition:all .2s}
.tab-btn:hover{border-color:#0f3460;color:#0f3460}
.tab-btn.active{background:#0f3460;color:#fff;border-color:#0f3460}
.highlight-bar{background:linear-gradient(135deg,#fff5f5,#fff0e6);padding:10px 24px;display:flex;gap:20px;font-size:13px;color:#666;flex-wrap:wrap;align-items:center;border-bottom:1px solid #f0e0e0}
.highlight-bar strong{color:#e74c3c;font-size:15px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;padding:16px 24px;background:#fff;border-bottom:1px solid #e0e0e0}
.stat-card{background:#f8f9fa;border-radius:10px;padding:14px 16px;text-align:center;border:1px solid #eee;transition:transform .15s}
.stat-card:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,0.08)}
.stat-card .label{font-size:12px;color:#888;margin-bottom:4px}
.stat-card .value{font-size:22px;font-weight:700}
.stat-card .value.positive{color:#e74c3c}
.stat-card .value.negative{color:#27ae60}
.stat-card .sub{font-size:11px;color:#aaa;margin-top:2px}
.chart-container{width:100%;height:580px;background:#fff;margin:0;border-bottom:1px solid #e0e0e0;position:relative}
.chart-container.equity-chart{height:340px}
.chart-section-title{padding:12px 24px 0;font-size:14px;font-weight:600;color:#333;background:#fff}
.legend-bar{display:flex;gap:16px;padding:8px 24px;background:#fff;font-size:12px;color:#666;align-items:center;flex-wrap:wrap;border-bottom:1px solid #e0e0e0}
.legend-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}
.trade-table-wrap{padding:16px 24px;background:#fff;max-height:500px;overflow-y:auto;border-bottom:1px solid #e0e0e0}
.trade-table{width:100%;border-collapse:collapse;font-size:12px}
.trade-table th{position:sticky;top:0;background:#1a1a2e;color:#fff;padding:9px 6px;text-align:center;font-weight:500;z-index:1;font-size:12px}
.trade-table td{padding:6px 6px;text-align:center;border-bottom:1px solid #f0f0f0;font-size:12px}
.trade-table tr:hover{background:#f0f4ff !important}
tr.win-row{background:#fff5f5}
tr.loss-row{background:#f0fff4}
.badge-win{background:#e74c3c;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px}
.badge-loss{background:#27ae60;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px}
.badge-long{background:#e74c3c20;color:#e74c3c;padding:2px 6px;border-radius:4px;font-size:11px;border:1px solid #e74c3c40}
.badge-short{background:#27ae6020;color:#27ae60;padding:2px 6px;border-radius:4px;font-size:11px;border:1px solid #27ae6040}
.month-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;padding:16px 24px;background:#fff;border-bottom:1px solid #e0e0e0}
.month-card{padding:10px 12px;border-radius:8px;text-align:center;font-size:12px}
.month-card.win-month{background:#fff5f5;border:1px solid #fdd}
.month-card.loss-month{background:#f0fff4;border:1px solid #cfc}
.month-card .m-label{font-weight:600;color:#333}
.month-card .m-trades{color:#888;font-size:11px}
.month-card .m-ret{font-size:16px;font-weight:700;margin-top:2px}
.footer{text-align:center;padding:20px;color:#888;font-size:12px}
.search-box{padding:10px 24px;background:#fff;display:flex;gap:10px;align-items:center}
.search-box input{flex:1;max-width:300px;padding:6px 12px;border:1px solid #ddd;border-radius:6px;font-size:13px}
.search-box .filter-btn{padding:6px 14px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;font-size:12px}
.search-box .filter-btn.active{background:#0f3460;color:#fff;border-color:#0f3460}
@media(max-width:768px){.stats-grid{grid-template-columns:repeat(3,1fr)}.chart-container{height:400px}}
</style>
</head>
<body>

<div class="header">
<div>
  <h1>币圈分型策略回测系统 <span class="badge">RR=1:1</span><span class="badge">100x杠杆</span></h1>
  <div class="subtitle">策略：分型(5,2) + 1h多周期共振 + 冷却3根 + 止损截断 | 100x杠杆 5%保证金(5x敞口) | 2025.08.12 - 2026.08.12</div>
</div>
</div>

<div class="tab-bar">
  <button class="tab-btn active" onclick="switchSymbol('BTC')">BTC/USDT</button>
  <button class="tab-btn" onclick="switchSymbol('ETH')">ETH/USDT</button>
</div>

<div id="stats-panel" class="stats-grid"></div>

<div class="highlight-bar" id="highlight-bar"></div>

<div id="month-panel" class="month-grid"></div>

<div class="chart-section-title">K线图 & 买卖点</div>
<div class="chart-container" id="main-chart"><div class="loading" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#888">加载中...</div></div>
<div class="legend-bar" id="legend-bar"></div>

<div class="chart-section-title">资金曲线 & 收益率</div>
<div class="chart-container equity-chart" id="equity-chart"><div class="loading" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#888">加载中...</div></div>

<div class="search-box">
  <span style="font-size:13px;color:#666;white-space:nowrap">完整交易明细</span>
  <span style="color:#888;font-size:11px">(共<span id="trade-total">0</span>笔)</span>
  <input type="text" id="trade-search" placeholder="搜索时间/价格..." oninput="filterTrades()">
  <button class="filter-btn active" onclick="filterByResult('all',this)">全部</button>
  <button class="filter-btn" onclick="filterByResult('win',this)">盈利</button>
  <button class="filter-btn" onclick="filterByResult('loss',this)">亏损</button>
  <button class="filter-btn" onclick="filterByResult('long',this)">做多</button>
  <button class="filter-btn" onclick="filterByResult('short',this)">做空</button>
</div>
<div class="trade-table-wrap" id="trade-table"></div>
<div class="footer">策略回测系统 RR=1 · 基于真实历史数据 · 仅供研究参考，不构成投资建议</div>

<script>
var ALL_DATA = { "BTC": __BTC_DATA__, "ETH": __ETH_DATA__ };
var currentSymbol = 'BTC';
var currentFilter = 'all';
var mainChart = null;
var equityChart = null;

function init() {
  mainChart = echarts.init(document.getElementById('main-chart'));
  equityChart = echarts.init(document.getElementById('equity-chart'));
  switchSymbol('BTC');
  window.addEventListener('resize', function() { mainChart && mainChart.resize(); equityChart && equityChart.resize(); });
}

function switchSymbol(sym) {
  currentSymbol = sym;
  var btns = document.querySelectorAll('.tab-btn');
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
  event.target.classList.add('active');
  currentFilter = 'all';
  document.querySelectorAll('.filter-btn').forEach(function(b,i){ b.classList.toggle('active', i===0); });
  renderAll(ALL_DATA[sym]);
}

function renderAll(data) {
  renderStats(data);
  renderHighlight(data);
  renderMonthGrid(data);
  renderMainChart(data);
  renderEquityChart(data);
  renderTradeTable(data);
  document.getElementById('trade-total').textContent = data.trades.length;
  document.getElementById('legend-bar').innerHTML =
    '<span><span class="legend-dot" style="background:#e74c3c"></span> 做多入场(向上箭头)</span>' +
    '<span><span class="legend-dot" style="background:#27ae60"></span> 做空入场(向下箭头)</span>' +
    '<span><span class="legend-dot" style="background:#f39c12"></span> 止盈(菱形)</span>' +
    '<span><span class="legend-dot" style="background:#888"></span> 止损(三角)</span>' +
    '<span style="margin-left:12px"><span class="legend-dot" style="background:#e74c3c;opacity:.5;width:20px;height:2px;border-radius:2px"></span> 盈利连线</span>' +
    '<span><span class="legend-dot" style="background:#27ae60;opacity:.5;width:20px;height:2px;border-radius:2px"></span> 亏损连线</span>' +
    '<span style="margin-left:12px;color:#888">滚轮缩放 | 拖拽平移 | 点击箭头定位交易</span>';
}

function renderStats(data) {
  var s = data.stats;
  var retClass = s.total_return >= 0 ? 'positive' : 'negative';
  var pf = s.profit_factor >= 999 ? '∞' : s.profit_factor.toFixed(3);

  document.getElementById('stats-panel').innerHTML =
    '<div class="stat-card"><div class="label">交易总次数</div><div class="value">' + s.trades + '</div><div class="sub">多' + s.long_trades + ' / 空' + s.short_trades + '</div></div>' +
    '<div class="stat-card"><div class="label">胜率</div><div class="value positive">' + (s.win_rate*100).toFixed(1) + '%</div><div class="sub">盈' + s.wins + ' / 亏' + s.losses + '</div></div>' +
    '<div class="stat-card"><div class="label">累计收益(杠杆)</div><div class="value ' + retClass + '">' + fmtLargeNum(s.total_return*100) + '%</div><div class="sub">最终 ' + fmtLargeNum(s.final_capital) + ' USDT</div></div>' +
    '<div class="stat-card"><div class="label">盈亏因子</div><div class="value positive">' + pf + '</div><div class="sub">Profit Factor</div></div>' +
    '<div class="stat-card"><div class="label">最大回撤</div><div class="value negative">' + (s.max_drawdown*100).toFixed(2) + '%</div><div class="sub">Max Drawdown</div></div>' +
    '<div class="stat-card"><div class="label">平均单笔(杠杆)</div><div class="value ' + (s.avg_return>=0?'positive':'negative') + '">' + (s.avg_return * s.notional_mult * 100).toFixed(2) + '%</div><div class="sub">盈' + (s.avg_win_return * s.notional_mult * 100).toFixed(2) + '% / 亏' + (s.avg_loss_return * s.notional_mult * 100).toFixed(2) + '%</div></div>' +
    '<div class="stat-card"><div class="label">最大连续亏损</div><div class="value">' + s.max_consecutive_losses + '</div><div class="sub">笔</div></div>' +
    '<div class="stat-card"><div class="label">平均持仓K线</div><div class="value">' + s.avg_hold_bars.toFixed(1) + '</div><div class="sub">根 (30min)</div></div>';
}

function fmtLargeNum(n) {
  if (Math.abs(n) >= 1e12) return (n/1e12).toFixed(2) + '万亿';
  if (Math.abs(n) >= 1e8) return (n/1e8).toFixed(2) + '亿';
  if (Math.abs(n) >= 1e4) return (n/1e4).toFixed(2) + '万';
  return n.toFixed(2);
}

function renderHighlight(data) {
  var s = data.stats;
  document.getElementById('highlight-bar').innerHTML =
    '<span>做多胜率: <strong>' + (s.long_win_rate*100).toFixed(1) + '%</strong></span>' +
    '<span>做空胜率: <strong>' + (s.short_win_rate*100).toFixed(1) + '%</strong></span>' +
    '<span>策略: 分型(5,2)+1h共振+冷却3根 | RR=1:1 | ' + s.leverage + 'x杠杆</span>';
}

function renderMonthGrid(data) {
  var trades = data.trades;
  var byMonth = {};
  for (var i = 0; i < trades.length; i++) {
    var m = trades[i].entry_time.slice(0,7);
    if (!byMonth[m]) byMonth[m] = { trades: 0, wins: 0, ret: 1 };
    byMonth[m].trades++;
    if (trades[i].result === 'win') byMonth[m].wins++;
    byMonth[m].ret *= (1 + trades[i].account_return);
  }
  var months = Object.keys(byMonth).sort();
  var html = '';
  for (var j = 0; j < months.length; j++) {
    var m = months[j];
    var d = byMonth[m];
    var mRet = (d.ret - 1) * 100;
    var cls = mRet >= 0 ? 'win-month' : 'loss-month';
    var color = mRet >= 0 ? '#e74c3c' : '#27ae60';
    html += '<div class="month-card ' + cls + '">' +
      '<div class="m-label">' + m + '</div>' +
      '<div class="m-trades">' + d.trades + '笔 盈' + d.wins + '</div>' +
      '<div class="m-ret" style="color:' + color + '">' + (mRet>=0?'+':'') + mRet.toFixed(1) + '%</div>' +
      '</div>';
  }
  document.getElementById('month-panel').innerHTML = html;
}

function renderMainChart(data) {
  var klines = data.kline;
  var dates = [];
  var ohlc = [];
  for (var i = 0; i < klines.length; i++) {
    var k = klines[i];
    dates.push(k.time);
    ohlc.push([k.open, k.close, k.low, k.high]);
  }

  var buyMarks = [], sellMarks = [], tpMarks = [], slMarks = [];
  var winLines = [], lossLines = [];

  for (var i = 0; i < data.trades.length; i++) {
    var t = data.trades[i];
    // Skip trades outside kline range for lines
    var mark = {
      name: '#' + (i+1),
      coord: [t.entry_time, t.entry_price],
      value: (t.side==='long'?'多':'空') + '#' + (i+1) + (t.result==='win'?' ✓':' ✗'),
      symbol: 'arrow',
      symbolSize: t.result==='win' ? 14 : 10,
      symbolRotate: t.side === 'long' ? 180 : 0,
      itemStyle: { color: t.result==='win' ? (t.side==='long'?'#e74c3c':'#27ae60') : '#999' },
      label: { show: false }
    };
    if (t.side === 'long') buyMarks.push(mark);
    else sellMarks.push(mark);

    var exitMark = {
      name: '#' + (i+1),
      coord: [t.exit_time, t.exit_price],
      symbol: t.result==='win' ? 'diamond' : 'triangle',
      symbolSize: 8,
      symbolRotate: t.result==='win' ? 0 : 180,
      itemStyle: { color: t.result==='win' ? '#f39c12' : '#888' },
      label: { show: false }
    };
    if (t.result === 'win') tpMarks.push(exitMark);
    else slMarks.push(exitMark);

    if (dates.indexOf(t.entry_time) >= 0 && dates.indexOf(t.exit_time) >= 0) {
      var lineData = { coords: [[t.entry_time, t.entry_price], [t.exit_time, t.exit_price]] };
      if (t.result === 'win') winLines.push(lineData);
      else lossLines.push(lineData);
    }
  }

  var option = {
    backgroundColor: '#fff',
    animation: true,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: function(params) {
        if (!params || params.length === 0) return '';
        var result = '';
        for (var j = 0; j < params.length; j++) {
          var p = params[j];
          if (p.seriesName === 'K线' && p.data) {
            var d = p.data;
            var color = d[2] > d[1] ? '#e74c3c' : '#27ae60';
            result += '时间: ' + p.axisValue + '<br/>开: ' + d[1] + ' 高: ' + d[3] + '<br/>低: ' + d[2] + ' 收: <span style="color:' + color + ';font-weight:bold">' + d[2] + '</span><br/>';
          }
        }
        for (var k = 0; k < params.length; k++) {
          var pp = params[k];
          if (pp.seriesName !== 'K线' && pp.seriesName !== '成交量' && pp.seriesName !== '盈利连线' && pp.seriesName !== '亏损连线' && pp.value) {
            result += pp.marker + ' ' + pp.value + '<br/>';
          }
        }
        return result;
      }
    },
    grid: [
      { left: '3%', right: '3%', top: '5%', height: '68%' },
      { left: '3%', right: '3%', top: '78%', height: '16%' }
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLine: { lineStyle: { color: '#ccc' } }, axisLabel: { show: true, fontSize: 10, formatter: function(v) { return v.slice(5,16); } }, axisTick: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false }, axisTick: { show: false } }
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, scale: true, splitLine: { lineStyle: { color: '#f0f0f0' } }, axisLabel: { fontSize: 10 } },
      { type: 'value', gridIndex: 1, scale: true, axisLabel: { show: false }, splitLine: { show: false } }
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0,1], start: 0, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
      { type: 'slider', xAxisIndex: [0,1], start: 0, end: 100, height: 20, bottom: 5, borderColor: '#ddd', fillerColor: 'rgba(15,52,96,0.2)', handleStyle: { color: '#0f3460' } }
    ],
    series: [
      { name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0, itemStyle: { color: '#e74c3c', color0: '#27ae60', borderColor: '#e74c3c', borderColor0: '#27ae60' }, z: 1 },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: klines.map(function(k, i) { var prev = i>0?klines[i-1]:k; return { value: k.volume, itemStyle: { color: k.close>=prev.close?'rgba(231,76,60,0.4)':'rgba(39,174,96,0.4)' } }; }), z: 1 },
      { name: '做多入场', type: 'scatter', data: buyMarks, xAxisIndex: 0, yAxisIndex: 0, symbolSize: 14, z: 10 },
      { name: '做空入场', type: 'scatter', data: sellMarks, xAxisIndex: 0, yAxisIndex: 0, symbolSize: 14, z: 10 },
      { name: '止盈出场', type: 'scatter', data: tpMarks, xAxisIndex: 0, yAxisIndex: 0, symbolSize: 9, z: 10 },
      { name: '止损出场', type: 'scatter', data: slMarks, xAxisIndex: 0, yAxisIndex: 0, symbolSize: 9, z: 10 },
      { name: '盈利连线', type: 'lines', coordinateSystem: 'cartesian2d', xAxisIndex: 0, yAxisIndex: 0, polyline: false, data: winLines, lineStyle: { color: '#e74c3c', width: 1, opacity: 0.3, type: 'dashed' }, effect: { show: false }, z: 2 },
      { name: '亏损连线', type: 'lines', coordinateSystem: 'cartesian2d', xAxisIndex: 0, yAxisIndex: 0, polyline: false, data: lossLines, lineStyle: { color: '#27ae60', width: 1, opacity: 0.25, type: 'dashed' }, effect: { show: false }, z: 2 }
    ]
  };

  mainChart.setOption(option, true);
  mainChart.off('click');
  mainChart.on('click', function(params) {
    if (params.componentType === 'series' && (params.seriesName === '做多入场' || params.seriesName === '做空入场')) {
      var match = params.name.match(/#(\d+)/);
      if (match) {
        var row = document.getElementById('trade-row-' + match[1]);
        if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); row.style.background = '#ffeb3b'; setTimeout(function() { row.style.background = ''; }, 1500); }
      }
    }
  });
}

function renderEquityChart(data) {
  var equity = data.equity;
  if (!equity || equity.length === 0) {
    equityChart.setOption({ title: { text: '无交易数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } } }, true);
    return;
  }

  var capData = equity.map(function(e) { return +e.capital.toFixed(2); });
  var labels = equity.map(function(e, i) { return '#' + e.trade_no; });
  var step = Math.max(1, Math.floor(equity.length / 15));
  var returns = equity.map(function(e) { return +((e.capital - 10000) / 10000 * 100).toFixed(2); });

  var option = {
    backgroundColor: '#fff',
    tooltip: { trigger: 'axis', formatter: function(p) { return p[0].axisValue + '<br/>资金: ' + p[0].value.toFixed(2) + ' USDT<br/>收益率: ' + (p[1] ? p[1].value.toFixed(2) : '0') + '%'; } },
    legend: { data: ['资金曲线','收益率%'], bottom: 0, textStyle: { fontSize: 11 } },
    grid: { left: '3%', right: '4%', top: '8%', bottom: '12%' },
    xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 9, interval: step } },
    yAxis: [
      { type: 'value', name: 'USDT', axisLabel: { fontSize: 10, formatter: function(v) { return fmtLargeNum(v); } }, splitLine: { lineStyle: { color: '#f0f0f0' } } },
      { type: 'value', name: '%', axisLabel: { fontSize: 10 }, splitLine: { show: false } }
    ],
    series: [
      { name: '资金曲线', type: 'line', data: capData, yAxisIndex: 0, smooth: true, lineStyle: { color: '#0f3460', width: 2 }, areaStyle: { color: new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'rgba(15,52,96,0.25)'},{offset:1,color:'rgba(15,52,96,0.02)'}]) }, itemStyle: { color: '#0f3460' }, symbol: 'none', markLine: { silent: true, data: [{yAxis: 10000, label: { formatter:'初始' }, lineStyle: { color:'#aaa', type:'dashed' }}] } },
      { name: '收益率%', type: 'line', data: returns, yAxisIndex: 1, smooth: true, lineStyle: { color: '#e74c3c', width: 1.5, type: 'dotted' }, itemStyle: { color: '#e74c3c' }, symbol: 'none' }
    ]
  };
  equityChart.setOption(option, true);
}

function renderTradeTable(data) {
  var trades = data.trades;
  var html = '<table class="trade-table"><thead><tr>' +
    '<th>#</th><th>方向</th><th>入场时间</th><th>入场价</th>' +
    '<th>出场时间</th><th>出场价</th><th>止损价</th><th>止盈价</th>' +
    '<th>结果</th><th>收益率(杆)</th><th>RR</th>' +
    '</tr></thead><tbody>';

  for (var i = 0; i < trades.length; i++) {
    var t = trades[i];
    if (currentFilter === 'win' && t.result !== 'win') continue;
    if (currentFilter === 'loss' && t.result !== 'loss') continue;
    if (currentFilter === 'long' && t.side !== 'long') continue;
    if (currentFilter === 'short' && t.side !== 'short') continue;

    var searchTerm = document.getElementById('trade-search').value.toLowerCase();
    if (searchTerm && t.entry_time.toLowerCase().indexOf(searchTerm) < 0 && t.exit_time.toLowerCase().indexOf(searchTerm) < 0 && String(t.entry_price).indexOf(searchTerm) < 0) continue;

    var rowClass = t.result === 'win' ? 'win-row' : 'loss-row';
    var badgeClass = t.result === 'win' ? 'badge-win' : 'badge-loss';
    var badgeText = t.result === 'win' ? '盈利' : '亏损';
    var sideBadge = t.side === 'long' ? 'badge-long' : 'badge-short';
    var sideText = t.side === 'long' ? '多' : '空';
    var accRet = t.account_return * 100;
    var retColor = accRet >= 0 ? 'color:#e74c3c;font-weight:600' : 'color:#27ae60;font-weight:600';

    html += '<tr id="trade-row-' + (i+1) + '" class="' + rowClass + '">' +
      '<td>' + (i+1) + '</td>' +
      '<td><span class="' + sideBadge + '">' + sideText + '</span></td>' +
      '<td>' + t.entry_time.slice(0,16) + '</td>' +
      '<td>' + t.entry_price.toFixed(1) + '</td>' +
      '<td>' + t.exit_time.slice(0,16) + '</td>' +
      '<td>' + t.exit_price.toFixed(1) + '</td>' +
      '<td>' + t.stop_loss.toFixed(1) + '</td>' +
      '<td>' + t.take_profit.toFixed(1) + '</td>' +
      '<td><span class="' + badgeClass + '">' + badgeText + '</span></td>' +
      '<td style="' + retColor + '">' + (accRet>=0?'+':'') + accRet.toFixed(2) + '%</td>' +
      '<td>' + t.rr.toFixed(1) + '</td>' +
      '</tr>';
  }
  html += '</tbody></table>';
  document.getElementById('trade-table').innerHTML = html;
}

function filterByResult(type, btn) {
  currentFilter = type;
  document.querySelectorAll('.filter-btn').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
  renderTradeTable(ALL_DATA[currentSymbol]);
}

function filterTrades() {
  renderTradeTable(ALL_DATA[currentSymbol]);
}

document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>'''

    html = html_template.replace('__BTC_DATA__', btc_json).replace('__ETH_DATA__', eth_json)

    output_path = "backtest_dashboard.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    file_size = Path(output_path).stat().st_size / 1024 / 1024
    print(f"网页已生成: {output_path} ({file_size:.1f} MB)")
    return output_path


if __name__ == "__main__":
    generate_html()
