/* ──────────────────────────────────────────────────────────────────
   StrategyFyers Dashboard — app.js
   ────────────────────────────────────────────────────────────────── */

const API = '';   // same origin

// ── Tab routing ───────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const tab = el.dataset.tab;
    switchTab(tab);
  });
});

function switchTab(tab) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById(`nav-${tab}`).classList.add('active');
  document.getElementById(`tab-${tab}`).classList.add('active');

  if (tab === 'trades') loadTrades();
  if (tab === 'config') loadConfig();
  if (tab === 'auth')   loadTokenStatus();
}

// ── Status polling (every 3 seconds) ──────────────────────────────
let _statusInterval = null;

function startStatusPolling() {
  fetchStatus();
  _statusInterval = setInterval(fetchStatus, 3000);
}

async function fetchStatus() {
  try {
    const r = await fetch(`${API}/api/status`);
    const d = await r.json();
    applyStatus(d);
  } catch (e) {
    console.warn('Status fetch failed:', e);
  }
}

function applyStatus(d) {
  // Bot running
  const botEl = document.getElementById('status-bot');
  const modeEl = document.getElementById('status-mode');
  const card = document.getElementById('card-bot-status');

  if (d.running) {
    botEl.textContent = '🟢 RUNNING';
    botEl.className = 'card-value green';
    modeEl.textContent = d.dry_run ? 'DRY RUN mode' : '⚠️ LIVE mode';
    card.style.borderColor = 'rgba(16,185,129,0.4)';
  } else {
    botEl.textContent = '⚪ STOPPED';
    botEl.className = 'card-value';
    modeEl.textContent = '—';
    card.style.borderColor = '';
  }

  // WS
  const wsEl = document.getElementById('status-ws');
  const wsSubEl = document.getElementById('status-ws-sub');
  if (d.ws_connected) {
    wsEl.textContent = '✅ CONNECTED';
    wsEl.className = 'card-value green';
    wsSubEl.textContent = 'Data flowing';
  } else {
    wsEl.textContent = '❌ OFFLINE';
    wsEl.className = 'card-value red';
    wsSubEl.textContent = d.running ? 'Reconnecting…' : 'Bot stopped';
  }

  // Positions & trades
  document.getElementById('status-positions').textContent = d.open_positions ?? 0;
  document.getElementById('status-trades').textContent = d.trades_today ?? 0;

  // PnL
  const pnl = d.pnl_today ?? 0;
  const pnlEl = document.getElementById('status-pnl');
  const pnlCard = pnlEl.closest('.card');
  const sign = pnl >= 0 ? '+' : '';
  pnlEl.textContent = `₹${sign}${pnl.toFixed(2)}`;
  pnlEl.className = 'card-value ' + (pnl >= 0 ? 'green' : 'red');
  pnlCard.classList.toggle('positive', pnl > 0);
  pnlCard.classList.toggle('negative', pnl < 0);

  // Button states
  document.getElementById('btn-stop').disabled = !d.running;
  document.getElementById('btn-start-dry').disabled = d.running;
  document.getElementById('btn-start-live').disabled = d.running;
}

// ── Token badge (sidebar) ──────────────────────────────────────────
async function refreshSidebarToken() {
  try {
    const r = await fetch(`${API}/api/token`);
    const d = await r.json();
    const dot = document.getElementById('sidebar-token-dot');
    const label = document.getElementById('sidebar-token-label');
    if (d.valid) {
      dot.className = 'badge-dot green';
      label.textContent = 'Token valid';
    } else {
      dot.className = 'badge-dot red';
      label.textContent = 'Token expired';
    }
  } catch (e) {}
}

// ── Bot control ────────────────────────────────────────────────────
async function startBot(dryRun) {
  const label = dryRun ? 'DRY RUN' : 'LIVE';
  if (!dryRun) {
    if (!confirm(`⚠️ You are about to start the bot in LIVE mode.\nReal orders will be placed. Continue?`)) return;
  }
  try {
    const r = await fetch(`${API}/api/bot/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: dryRun }),
    });
    const d = await r.json();
    showToast(d.success ? `✅ ${d.message}` : `❌ ${d.message}`, d.success ? 'success' : 'error');
    fetchStatus();
  } catch (e) {
    showToast('❌ Failed to start bot', 'error');
  }
}

async function stopBot() {
  if (!confirm('Stop the trading bot? Any open positions will not be auto-closed.')) return;
  try {
    const r = await fetch(`${API}/api/bot/stop`, { method: 'POST' });
    const d = await r.json();
    showToast(d.success ? `✅ ${d.message}` : `❌ ${d.message}`, d.success ? 'success' : 'error');
    fetchStatus();
  } catch (e) {
    showToast('❌ Failed to stop bot', 'error');
  }
}

// ── Log streaming (SSE) ────────────────────────────────────────────
let _sse = null;

function startLogStream() {
  if (_sse) { _sse.close(); }
  const logBody = document.getElementById('log-body');
  _sse = new EventSource(`${API}/api/logs/stream?lines=120`);

  _sse.onmessage = (e) => {
    const line = JSON.parse(e.data);
    appendLog(line);
  };

  _sse.onerror = () => {
    appendLog('[Connection to log stream lost — retrying…]', 'error');
  };
}

function appendLog(line, forceClass) {
  const logBody = document.getElementById('log-body');
  const div = document.createElement('div');
  div.textContent = line;

  // Colorize
  if (forceClass) {
    div.className = `log-line-${forceClass}`;
  } else if (line.includes('ERROR') || line.includes('CRITICAL')) {
    div.className = 'log-line-error';
  } else if (line.includes('WARNING') || line.includes('No tick')) {
    div.className = 'log-line-warning';
  } else if (line.includes('Signal') || line.includes('SMC') || line.includes('signal')) {
    div.className = 'log-line-signal';
  } else if (line.includes('DRY RUN') || line.includes('TRADE') || line.includes('Candle close')) {
    div.className = 'log-line-trade';
  } else if (line.includes('💓') || line.includes('IST | Market')) {
    div.className = 'log-line-heart';
  } else {
    div.className = 'log-line-info';
  }

  logBody.appendChild(div);

  const autoScroll = document.getElementById('log-autoscroll');
  if (autoScroll && autoScroll.checked) {
    logBody.scrollTop = logBody.scrollHeight;
  }

  // Keep max 500 lines
  while (logBody.children.length > 500) {
    logBody.removeChild(logBody.firstChild);
  }
}

function clearLog() {
  document.getElementById('log-body').innerHTML = '';
}

// ── Backtest ───────────────────────────────────────────────────────
async function runBacktest() {
  const symbol = document.getElementById('bt-symbol').value;
  const date = document.getElementById('bt-date').value;
  const resolution = parseInt(document.getElementById('bt-resolution').value);

  document.getElementById('btn-run-bt').disabled = true;
  document.getElementById('bt-results').classList.add('hidden');
  document.getElementById('bt-raw-output').classList.add('hidden');
  document.getElementById('bt-loading').classList.remove('hidden');

  try {
    const r = await fetch(`${API}/api/backtest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, date, resolution }),
    });
    const d = await r.json();

    document.getElementById('bt-loading').classList.add('hidden');
    document.getElementById('bt-results').classList.remove('hidden');
    document.getElementById('bt-raw-output').classList.remove('hidden');

    // Fill results
    const pnl = d.net_pnl ?? 0;
    const pnlEl = document.getElementById('bt-pnl');
    pnlEl.textContent = `₹${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
    pnlEl.className = 'result-value ' + (pnl >= 0 ? 'green' : 'red');

    document.getElementById('bt-trades').textContent = d.trades;
    document.getElementById('bt-winrate').textContent = `${d.win_rate?.toFixed(1)}%`;
    document.getElementById('bt-winners').textContent = d.winners;
    document.getElementById('bt-losers').textContent = d.losers;
    document.getElementById('bt-candles').textContent = d.candles_loaded;

    // Raw output
    const raw = document.getElementById('bt-raw-output');
    raw.textContent = d.raw_output || '';
    raw.scrollTop = raw.scrollHeight;

  } catch (e) {
    document.getElementById('bt-loading').classList.add('hidden');
    showToast('❌ Backtest failed: ' + e.message, 'error');
  } finally {
    document.getElementById('btn-run-bt').disabled = false;
  }
}

// ── Trades table ───────────────────────────────────────────────────
async function loadTrades() {
  const status = document.getElementById('trades-filter').value;
  const tbody = document.getElementById('trades-body');

  try {
    const r = await fetch(`${API}/api/trades?limit=200&status=${status}`);
    const d = await r.json();

    if (!d.trades || d.trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="13" class="empty-msg">No trades found</td></tr>';
      return;
    }

    tbody.innerHTML = d.trades.map(t => {
      const pnl = t.pnl ?? 0;
      const pnlClass = pnl > 0 ? 'green' : pnl < 0 ? 'red' : '';
      const sign = pnl > 0 ? '+' : '';
      return `<tr>
        <td>${t.id}</td>
        <td>${t.timestamp?.slice(0,19) ?? '—'}</td>
        <td title="${t.symbol}">${shortSym(t.symbol)}</td>
        <td>${t.strategy ?? '—'}</td>
        <td><span class="badge badge-${t.direction?.toLowerCase()}">${t.direction ?? '—'}</span></td>
        <td>${fmt(t.entry_price)}</td>
        <td>${fmt(t.sl)}</td>
        <td>${fmt(t.target)}</td>
        <td>${t.exit_price ? fmt(t.exit_price) : '—'}</td>
        <td>${t.qty ?? '—'}</td>
        <td class="${pnlClass}">${t.pnl != null ? `₹${sign}${pnl.toFixed(2)}` : '—'}</td>
        <td>${t.exit_reason ?? '—'}</td>
        <td><span class="badge badge-${(t.status ?? '').toLowerCase()}">${t.status ?? '—'}</span></td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="13" class="empty-msg">Error loading trades</td></tr>`;
  }
}

function shortSym(sym) {
  return (sym ?? '').replace('NSE:', '').replace('-INDEX', '');
}
function fmt(v) {
  return v != null ? parseFloat(v).toFixed(2) : '—';
}

// ── Config ────────────────────────────────────────────────────────
let _configData = {};

async function loadConfig() {
  try {
    const r = await fetch(`${API}/api/config`);
    const d = await r.json();
    _configData = d.config || {};
    renderConfig(_configData);
  } catch (e) {
    console.error('Config load failed:', e);
  }
}

const CONFIG_LABELS = {
  DRY_RUN: 'DRY RUN Mode',
  CAPITAL: 'Capital (₹)',
  RISK_PER_TRADE_PCT: 'Risk per Trade (%)',
  MAX_DAILY_LOSS_INR: 'Max Daily Loss (₹)',
  MAX_TRADES_PER_DAY: 'Max Trades per Day',
  OPTION_SL_PCT: 'Option SL (%)',
  OPTION_TARGET_PCT: 'Option Target (%)',
  TRAIL_ACTIVATION_PCT: 'Trail Activation (%)',
  TRAIL_DISTANCE_PCT: 'Trail Distance (%)',
  SIGNAL_COOLDOWN_MINUTES: 'Signal Cooldown (min)',
  MAX_POSITIONS_PER_SYMBOL: 'Max Positions/Symbol',
  ENTRY_CUTOFF: 'Entry Cutoff Time',
  CANDLE_TIMEFRAME_MINUTES: 'Candle Timeframe (min)',
};

function renderConfig(cfg) {
  const grid = document.getElementById('config-grid');
  grid.innerHTML = '';

  for (const [key, val] of Object.entries(cfg)) {
    const label = CONFIG_LABELS[key] || key;
    const isBool = typeof val === 'boolean';

    const item = document.createElement('div');
    item.className = 'config-item';

    if (isBool) {
      item.innerHTML = `
        <div class="form-group">
          <label class="form-label">${label}</label>
          <select class="form-control" data-key="${key}" id="cfg-${key}">
            <option value="true"  ${val ? 'selected' : ''}>Enabled (True)</option>
            <option value="false" ${!val ? 'selected' : ''}>Disabled (False)</option>
          </select>
        </div>`;
    } else {
      item.innerHTML = `
        <div class="form-group">
          <label class="form-label">${label}</label>
          <input type="text" class="form-control" data-key="${key}" id="cfg-${key}" value="${val}" />
        </div>`;
    }

    grid.appendChild(item);
  }
}

async function saveConfig() {
  const inputs = document.querySelectorAll('#config-grid [data-key]');
  const payload = {};

  inputs.forEach(el => {
    const key = el.dataset.key;
    let val = el.value;
    // Coerce bool
    if (val === 'true') val = true;
    else if (val === 'false') val = false;
    payload[key] = val;
  });

  try {
    const r = await fetch(`${API}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    const msgEl = document.getElementById('config-saved-msg');
    msgEl.style.display = 'block';
    setTimeout(() => { msgEl.style.display = 'none'; }, 4000);
  } catch (e) {
    showToast('❌ Failed to save config', 'error');
  }
}

// ── Auth ──────────────────────────────────────────────────────────
async function loadTokenStatus() {
  try {
    const r = await fetch(`${API}/api/token`);
    const d = await r.json();

    document.getElementById('auth-valid').textContent = d.valid ? '✅ Valid' : '❌ Expired';
    document.getElementById('auth-valid').className = 'token-val ' + (d.valid ? 'green' : 'red');
    document.getElementById('auth-date').textContent = d.date || '—';
    document.getElementById('auth-user').textContent = d.user || '—';
    document.getElementById('auth-today').textContent = d.today || '—';
  } catch (e) {}
}

async function getAuthUrl() {
  try {
    const r = await fetch(`${API}/api/auth/url`);
    const d = await r.json();
    const block = document.getElementById('auth-url-block');
    const urlEl = document.getElementById('auth-url-text');
    block.classList.remove('hidden');
    urlEl.textContent = d.url;
    urlEl.onclick = () => { window.open(d.url, '_blank'); };
  } catch (e) {
    showToast('❌ Could not get auth URL', 'error');
  }
}

async function submitAuthCode() {
  const code = document.getElementById('auth-code-input').value.trim();
  if (!code) { showToast('Please paste the auth code first', 'error'); return; }

  const msgEl = document.getElementById('auth-result-msg');
  msgEl.style.display = 'none';

  try {
    const r = await fetch(`${API}/api/auth`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auth_code: code }),
    });
    const d = await r.json();

    msgEl.style.display = 'block';
    if (d.success) {
      msgEl.className = 'alert alert-success';
      msgEl.textContent = `✅ Token generated successfully! Logged in as ${d.user}`;
      refreshSidebarToken();
      loadTokenStatus();
    } else {
      msgEl.className = 'alert alert-error';
      msgEl.textContent = `❌ Failed: ${d.detail || 'Unknown error'}`;
    }
  } catch (e) {
    msgEl.style.display = 'block';
    msgEl.className = 'alert alert-error';
    msgEl.textContent = `❌ Error: ${e.message}`;
  }
}

// ── Toast notifications ───────────────────────────────────────────
function showToast(msg, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `alert alert-${type === 'success' ? 'success' : type === 'error' ? 'error' : 'info'}`;
  toast.style.cssText = `
    position:fixed; bottom:1.5rem; right:1.5rem; z-index:9999;
    min-width:300px; max-width:480px;
    animation:fadeIn 0.2s ease;
    box-shadow: 0 4px 24px rgba(0,0,0,0.4);
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Set default backtest date to today
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById('bt-date').value = today;

  startStatusPolling();
  startLogStream();
  refreshSidebarToken();
  setInterval(refreshSidebarToken, 60000);
});
