from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .stats import get_summary, get_daily, get_by_model, get_recent

router = APIRouter()


@router.get("/api/stats/summary")
async def api_summary(request: Request, days: int | None = None):
    return await get_summary(request.app.state.db, days)


@router.get("/api/stats/daily")
async def api_daily(request: Request, days: int = 30):
    return await get_daily(request.app.state.db, days)


@router.get("/api/stats/by-model")
async def api_by_model(request: Request, days: int | None = None):
    return await get_by_model(request.app.state.db, days)


@router.get("/api/stats/recent")
async def api_recent(request: Request, limit: int = 50):
    return await get_recent(request.app.state.db, limit)


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Usage Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0f0f0f; color: #e0e0e0;
    padding: 24px; max-width: 1200px; margin: 0 auto;
}
h1 { font-size: 1.5rem; margin-bottom: 20px; color: #fff; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
.controls { display: flex; gap: 8px; }
.controls button {
    padding: 6px 14px; border-radius: 6px; border: 1px solid #333;
    background: #1a1a1a; color: #aaa; cursor: pointer; font-size: 0.85rem;
}
.controls button.active { background: #2563eb; color: #fff; border-color: #2563eb; }
.controls button:hover { border-color: #555; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card {
    background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
    padding: 20px;
}
.card .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }
.card .value { font-size: 1.8rem; font-weight: 600; color: #fff; margin-top: 4px; }
.card .value.blue { color: #60a5fa; }
.card .value.green { color: #4ade80; }
.card .value.purple { color: #c084fc; }
.card .value.amber { color: #fbbf24; }
.charts { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }
.chart-box {
    background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
    padding: 20px;
}
.chart-box h3 { font-size: 0.95rem; margin-bottom: 12px; color: #ccc; }
.chart-box canvas { max-height: 300px; }
.table-box {
    background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
    padding: 20px; overflow-x: auto;
}
.table-box h3 { font-size: 0.95rem; margin-bottom: 12px; color: #ccc; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th { text-align: left; padding: 8px 12px; color: #888; border-bottom: 1px solid #2a2a2a; font-weight: 500; }
td { padding: 8px 12px; border-bottom: 1px solid #1f1f1f; color: #ccc; }
tr:hover td { background: #222; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
}
.badge.stream { background: #1e3a5f; color: #60a5fa; }
.badge.sync { background: #1a3a2a; color: #4ade80; }
@media (max-width: 768px) {
    .charts { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="header">
    <h1>LLM Usage Monitor</h1>
    <div class="controls">
        <button data-days="1">24h</button>
        <button data-days="7">7d</button>
        <button data-days="30" class="active">30d</button>
        <button data-days="">All</button>
    </div>
</div>

<div class="cards">
    <div class="card">
        <div class="label">Total Requests</div>
        <div class="value blue" id="stat-requests">-</div>
    </div>
    <div class="card">
        <div class="label">Input Tokens (Prefill)</div>
        <div class="value green" id="stat-input">-</div>
    </div>
    <div class="card">
        <div class="label">Output Tokens (Decode)</div>
        <div class="value purple" id="stat-output">-</div>
    </div>
    <div class="card">
        <div class="label">Cache Hit Tokens</div>
        <div class="value amber" id="stat-cache">-</div>
    </div>
</div>

<div class="charts">
    <div class="chart-box">
        <h3>Daily Usage</h3>
        <canvas id="dailyChart"></canvas>
    </div>
    <div class="chart-box">
        <h3>By Model</h3>
        <canvas id="modelChart"></canvas>
    </div>
</div>

<div class="table-box">
    <h3>Recent Requests</h3>
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Model</th>
                <th>Input</th>
                <th>Output</th>
                <th>Type</th>
                <th>Stop Reason</th>
            </tr>
        </thead>
        <tbody id="recent-body"></tbody>
    </table>
</div>

<script>
let dailyChart, modelChart;
let currentDays = 30;

function fmt(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
}

function fmtDate(iso) {
    const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
    return d.toLocaleString();
}

async function refresh() {
    const days = currentDays || '';
    const params = days ? `?days=${days}` : '';

    // Summary
    const summary = await (await fetch('/api/stats/summary' + params)).json();
    document.getElementById('stat-requests').textContent = fmt(summary.total_requests);
    document.getElementById('stat-input').textContent = fmt(summary.total_input_tokens);
    document.getElementById('stat-output').textContent = fmt(summary.total_output_tokens);
    document.getElementById('stat-cache').textContent = fmt(summary.total_cache_read);

    // Daily chart
    const daily = await (await fetch('/api/stats/daily' + (days ? `?days=${days}` : '?days=30'))).json();
    const labels = daily.map(d => d.date);

    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart(document.getElementById('dailyChart'), {
        type: 'line',
        data: {
            labels,
            datasets: [
                { label: 'Input Tokens', data: daily.map(d => d.input_tokens), borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.1)', fill: true, tension: 0.3 },
                { label: 'Output Tokens', data: daily.map(d => d.output_tokens), borderColor: '#c084fc', backgroundColor: 'rgba(192,132,252,0.1)', fill: true, tension: 0.3 },
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#aaa' } } },
            scales: {
                x: { ticks: { color: '#666', maxTicksLimit: 10 }, grid: { color: '#1f1f1f' } },
                y: { ticks: { color: '#666', callback: v => fmt(v) }, grid: { color: '#1f1f1f' } }
            }
        }
    });

    // Model chart
    const models = await (await fetch('/api/stats/by-model' + params)).json();
    const modelLabels = models.map(m => m.model.replace('claude-', '').replace(/-[0-9]{8}$/, ''));
    const modelColors = ['#60a5fa', '#4ade80', '#c084fc', '#fbbf24', '#f87171', '#fb923c'];

    if (modelChart) modelChart.destroy();
    modelChart = new Chart(document.getElementById('modelChart'), {
        type: 'doughnut',
        data: {
            labels: modelLabels,
            datasets: [{
                data: models.map(m => m.requests),
                backgroundColor: modelColors.slice(0, models.length),
                borderColor: '#1a1a1a', borderWidth: 2,
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'bottom', labels: { color: '#aaa', padding: 12 } } }
        }
    });

    // Recent table
    const recent = await (await fetch('/api/stats/recent?limit=50')).json();
    const tbody = document.getElementById('recent-body');
    tbody.innerHTML = recent.map(r => `
        <tr>
            <td>${fmtDate(r.created_at)}</td>
            <td>${r.model}</td>
            <td>${fmt(r.input_tokens)}</td>
            <td>${fmt(r.output_tokens)}</td>
            <td><span class="badge ${r.is_streaming ? 'stream' : 'sync'}">${r.is_streaming ? 'stream' : 'sync'}</span></td>
            <td>${r.stop_reason || '-'}</td>
        </tr>
    `).join('');
}

// Time range buttons
document.querySelectorAll('.controls button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentDays = btn.dataset.days ? parseInt(btn.dataset.days) : null;
        refresh();
    });
});

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""
