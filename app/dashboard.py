"""Web dashboard — a token-protected page the client opens in a browser to see
live status, the last signal, open positions, today's trades, the hot cache, and
the active config. Data comes from build_state(); the page polls /api/dashboard.
"""
from __future__ import annotations

import time

from .schedule import entries_allowed


def build_state(settings, store, cache, account_id: str, monitor, started_at: float, watcher=None) -> dict:
    risk_on = settings.risk.enabled
    entries_ok, entries_why = entries_allowed(settings)
    # hide internal/test strategy_ids (e.g. the "loop_" self-test trades) from the client view
    hide = settings.execution.hide_strategy_prefix
    def _shown(sid):
        return not (hide and (sid or "").startswith(hide))
    # open positions, enriched with the current cached price
    open_pos = []
    for p in store.all_open():
        if not _shown(p.get("strategy_id")):
            continue
        q = cache.quote_for_symbol(p.get("option_symbol")) if cache else None
        # conservative realizable value: min(bid, last) when both present (avoids showing a
        # phantom high print on thin 0DTE quotes); else fall back to whichever exists.
        _b, _l = (q or {}).get("bid") or 0, (q or {}).get("last") or 0
        cur = (min(_b, _l) if (_b and _l) else (_l or _b or (q or {}).get("mid")))
        row = {
            "symbol": p.get("symbol"), "strategy_id": p.get("strategy_id"),
            "side": p.get("side"), "option_symbol": p.get("option_symbol"),
            "quantity": p.get("quantity"), "entry_price": p.get("entry_price"),
            "current_price": cur, "opened_at": p.get("opened_at"),
            "unrealized_pct": (round((cur - p["entry_price"]) / p["entry_price"], 4)
                               if cur and p.get("entry_price") else None),
            "unrealized_usd": (round((cur - p["entry_price"]) * p["quantity"] * 100, 2)
                               if cur and p.get("entry_price") else None),
        }
        open_pos.append(row)

    events = store.recent_events(80)
    last_signal = next((e for e in events if e.get("kind") == "signal"), None)
    trades = [t for t in store.recent_trades(120) if _shown(t.get("strategy_id"))][:60]

    # REAL TradeStation account P&L (from balances) — authoritative, not bot-calculated.
    ts = cache.pnl() if cache else {}
    ts_realized, ts_unrealized = ts.get("realized"), ts.get("unrealized")
    if ts_realized is not None or ts_unrealized is not None:
        account_pnl = round((ts_realized or 0) + (ts_unrealized or 0), 2)
        pnl_source = "TradeStation"
    else:   # broker P&L not available yet -> fall back to the bot-calculated figure
        account_pnl = store.trades_today_pnl(exclude_prefix=hide or None)
        pnl_source = "calculated"

    # effective daily-loss limit: dashboard runtime override if set, else config
    _dlo = store.get_meta("daily_max_loss_override")
    try:
        daily_loss = float(_dlo) if _dlo not in (None, "") else float(settings.risk.daily_max_loss_usd)
    except (TypeError, ValueError):
        daily_loss = float(settings.risk.daily_max_loss_usd)
    # daily trailing profit-protect stop ($ off the day's peak P&L; 0 = off)
    _dto = store.get_meta("daily_trailing_override")
    try:
        daily_trailing = float(_dto) if _dto not in (None, "") else float(getattr(settings.risk, "daily_trailing_stop_usd", 0) or 0)
    except (TypeError, ValueError):
        daily_trailing = 0.0
    try:
        day_peak_pnl = float(store.get_meta("day_peak_pnl") or 0.0)
    except (TypeError, ValueError):
        day_peak_pnl = 0.0

    cache_rows = []
    for sym in settings.cache.symbols:
        hot = cache.get(sym.upper()) if cache else None
        if hot:
            cache_rows.append({
                "symbol": sym, "underlying": hot.underlying_price,
                "call": hot.call_symbol, "call_ask": hot.call_quote.get("ask"),
                "put": hot.put_symbol, "put_ask": hot.put_quote.get("ask"),
                "age": round(time.monotonic() - hot.updated, 1),
            })
        else:
            cache_rows.append({"symbol": sym, "underlying": None, "age": None})

    return {
        "now": time.time(),
        "status": {
            "environment": settings.environment,
            "live_trading": settings.is_live,
            "risk_enabled": risk_on,
            "halted_today": (monitor.is_halted() if risk_on else False),
            "account": account_id,
            "equity": cache.equity() if cache else 0,
            "uptime_sec": int(time.time() - started_at) if started_at else None,
            "open_count": len(open_pos),
            "entries_open": entries_ok,
            "entries_note": entries_why,
            "watcher_ok": (watcher.connection_ok if watcher else True),
            "watcher_issues": (len(watcher.last_findings) if watcher else 0),
        },
        "config": {
            "symbols": settings.cache.symbols,
            "exit_mode": {
                "hybrid": f"hybrid — broker {settings.execution.bracket_stop_type} backstop + bot trail/trim/BE/TP",
                "bracket": f"broker OCO TP/SL ({settings.execution.bracket_stop_type}) — no bot trail/trim",
                "monitor": "bot monitor only (stop/trim/trail/BE/TP)",
            }.get(settings.execution.exit_mode, settings.execution.exit_mode),
            "entry_fill": settings.defaults.entry_fill,
            "exit_fill": settings.defaults.exit_fill,
            "expiration": settings.defaults.expiration,
            "strike": f"{settings.defaults.strike_selection}+{settings.defaults.strike_offset}",
            "sizing": (f"${settings.sizing.dollars}" if settings.sizing.mode == "dollars"
                       else f"{settings.sizing.percent}% equity") + f" (max {settings.sizing.max_contracts})",
            "stop_loss_pct": settings.risk.stop_loss_pct,
            "trims": [f"+{int(t.at_profit_pct*100)}%→sell {int(t.close_pct*100)}%" for t in settings.risk.trims],
            "trailing": (f"trail {int(settings.risk.trailing.trail_pct*100)}% after +{int(settings.risk.trailing.activate_at_pct*100)}%"
                         if settings.risk.trailing.enabled else "off"),
            "breakeven": (f"stop→entry once +{int(settings.risk.breakeven.trigger_pct*100)}%"
                          if settings.risk.breakeven.enabled else "off"),
            "max_exposure": (f"{int(settings.sizing.max_exposure_pct*100)}% of equity"
                             if settings.sizing.max_exposure_pct else "off"),
            "retry": (f"{settings.retry.max_attempts}x/{settings.retry.interval_sec}s chase ({settings.retry.applies_to})"
                      + (" +mkt" if settings.retry.market_fallback else "") if settings.retry.enabled else "off"),
            "take_profit": (f"+{int(settings.risk.take_profit_pct*100)}% full exit" if settings.risk.take_profit_pct else "off (trims)"),
            "schedule": (f"{settings.schedule.entry_start}-{settings.schedule.entry_end} "
                         f"{settings.schedule.timezone} ({','.join(settings.schedule.days)})"
                         if settings.schedule.enabled else "always (no window)"),
            "daily_max_loss_usd": daily_loss,
            "daily_trailing_usd": daily_trailing,
            "day_peak_pnl": round(day_peak_pnl, 2),
            "max_losing_trades": settings.risk.max_losing_trades or 0,
        },
        "groups": [
            {"name": g.name, "symbols": ", ".join(g.symbols),
             "stop": (f"{int(g.stop_loss_pct*100)}%" if g.stop_loss_pct is not None else "—"),
             "tp": (f"{int(g.take_profit_pct*100)}%" if g.take_profit_pct is not None else "—"),
             "size": (f"${g.dollars}" if g.dollars is not None else (f"{g.percent}%" if g.percent is not None else "—"))}
            for g in settings.groups
        ],
        "last_signal": last_signal,
        "events": events,
        "open_positions": open_pos,
        "trades": trades,
        "day_pnl": account_pnl,
        "ts_realized": ts_realized,
        "ts_unrealized": ts_unrealized,
        "pnl_source": pnl_source,
        "cache": cache_rows,
        "discrepancies": (watcher.last_findings if watcher else []),
    }


DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeStation Bot — Live</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;--grn:#3fb950;--red:#f85149;--amb:#d29922;--blu:#58a6ff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1200px;margin:0 auto;padding:18px}
  h1{font-size:18px;margin:0 0 2px} .sub{color:var(--mut);font-size:12px;margin-bottom:14px}
  .badges{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
  .badge{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;font-size:12px}
  .badge b{display:block;font-size:15px;margin-top:2px}
  .ok{color:var(--grn)} .bad{color:var(--red)} .warn{color:var(--amb)} .info{color:var(--blu)}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:14px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:12.5px} th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd)}
  th{color:var(--mut);font-weight:600} td.num{text-align:right;font-variant-numeric:tabular-nums}
  .pill{padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600}
  .pill.call{background:rgba(63,185,80,.15);color:var(--grn)} .pill.put{background:rgba(248,81,73,.15);color:var(--red)}
  .empty{color:var(--mut);padding:8px} .foot{color:var(--mut);font-size:11px;text-align:center;margin-top:8px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px} @media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
  <a href="/logout" style="float:right;font-size:12px;color:var(--blu);text-decoration:none;border:1px solid var(--bd);border-radius:6px;padding:5px 12px">Log out</a>
  <h1>TradeStation 0DTE Options Bot</h1>
  <div class="sub" id="sub">connecting…</div>
  <div class="badges" id="badges"></div>
  <div class="card"><h2>Last signal</h2><div id="last">—</div></div>
  <div class="card"><h2>Open positions</h2><div id="positions"></div></div>
  <div class="card"><h2>Watcher — bot vs broker reconciliation</h2><div id="watcher"></div></div>
  <div class="grid">
    <div class="card"><h2>Recent signals &amp; events</h2><div id="events"></div></div>
    <div class="card"><h2>Closed trades (P&amp;L) <a id="dlcsv" href="#" style="float:right;font-size:11px;color:var(--blu);text-decoration:none">⬇ export CSV</a></h2><div id="trades"></div></div>
  </div>
  <div class="card"><h2>Live cache (ATM call/put per symbol)</h2><div id="cache"></div></div>
  <div class="card"><h2>Trading mode — SIM / LIVE</h2>
    <div id="mode" class="empty">loading…</div>
    <div class="sub" style="margin-top:6px">Switching requires ALL positions closed. LIVE places real-money orders on your brokerage account the moment a TradingView alert arrives. Applies immediately and survives restarts.</div>
  </div>
  <div class="card"><h2>Risk controls</h2>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span>Daily loss limit&nbsp;$</span>
      <input id="dl_input" type="number" min="1" step="100"
        style="width:120px;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:5px 8px;font-size:13px">
      <button id="dl_save" onclick="saveDL()"
        style="background:var(--blu);color:#0d1117;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer">Save</button>
      <span id="dl_msg" style="color:var(--mut);font-size:12px"></span>
    </div>
    <div class="sub" style="margin-top:6px">Flatten everything &amp; stop for the day once the account is down this much. Applies immediately.</div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px;border-top:1px solid var(--bd);padding-top:12px">
      <span>Daily trailing stop&nbsp;$</span>
      <input id="tr_input" type="number" min="0" step="50"
        style="width:120px;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:5px 8px;font-size:13px">
      <button id="tr_save" onclick="saveTrail()"
        style="background:var(--blu);color:#0d1117;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer">Save</button>
      <span id="tr_msg" style="color:var(--mut);font-size:12px"></span>
      <span id="tr_peak" style="color:var(--mut);font-size:12px"></span>
    </div>
    <div class="sub" style="margin-top:6px">Locks in the day's profit: tracks your peak P&amp;L for the day, and if P&amp;L falls this many $ from that peak, it flattens &amp; stops for the day. <b>0 = off.</b> Example: profit peaks at +$300, trailing $150 → stops if P&amp;L drops to +$150.</div>
  </div>
  <div class="card"><h2>Baskets — per-basket risk config</h2>
    <div class="sub" style="margin-bottom:10px">Add stock symbols to a basket; the bot applies that basket's stop / take-profit / trim / break-even / size to every symbol in it, at entry. Your TradingView alert only needs <code>secret</code>, <code>action</code> (buy/sell), and <code>symbol</code>. Set a % field to <b>0</b> to turn it off.</div>
    <div id="baskets"></div>
    <div style="margin-top:10px">
      <button id="basket_save" onclick="saveBaskets()"
        style="background:var(--grn);color:#0d1117;border:0;border-radius:6px;padding:7px 14px;font-weight:600;cursor:pointer">Save baskets</button>
      <span id="basket_msg" style="margin-left:10px;font-size:12px;color:var(--mut)"></span>
    </div>
  </div>
  <div class="card"><h2>Trading window — applies to all baskets</h2>
    <div class="sub" style="margin-bottom:10px">The one shared entry window for the whole bot. Exits, stops, trims and the daily-loss kill-switch are always allowed regardless. Everything else (exit mode, fills, expiration/strike, trailing, break-even, max exposure, max losing trades, retry) is configured <b>per basket</b> below.</div>
    <div id="settings"></div>
    <div style="margin-top:10px">
      <button id="set_save" onclick="saveSettings()"
        style="background:var(--grn);color:#0d1117;border:0;border-radius:6px;padding:7px 14px;font-weight:600;cursor:pointer">Save window</button>
      <span id="set_msg" style="margin-left:10px;font-size:12px;color:var(--mut)"></span>
    </div>
  </div>
  <div class="card"><h2>Active configuration</h2><div id="config"></div></div>
  <div class="foot" id="foot"></div>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = id => document.getElementById(id);
const t = ts => ts ? new Date(ts*1000).toLocaleTimeString() : '—';
// always render in US Eastern (market) time, regardless of the viewer's timezone
const tet = ts => ts ? new Date(ts*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false})+' ET' : '—';
const n = (v,d=2) => (v===null||v===undefined) ? '—' : Number(v).toFixed(d);
const pct = v => (v===null||v===undefined) ? '—' : (v>=0?'+':'')+(v*100).toFixed(1)+'%';
const cls = v => v>0?'ok':v<0?'bad':'';
function badge(label,val,k){return `<div class="badge">${label}<b class="${k||''}">${val}</b></div>`;}
function rows(arr,cols,empty){if(!arr||!arr.length)return `<div class="empty">${empty}</div>`;
  let h='<table><tr>'+cols.map(c=>`<th>${c[0]}</th>`).join('')+'</tr>';
  for(const r of arr){h+='<tr>'+cols.map(c=>`<td class="${c[2]||''}">${c[1](r)}</td>`).join('')+'</tr>';}
  return h+'</table>';}
function side(s){return s?`<span class="pill ${s}">${s.toUpperCase()}</span>`:'—';}
async function tick(){
  try{
    const r = await fetch('/api/dashboard?token='+encodeURIComponent(TOKEN));
    if(r.status===401){$('sub').innerHTML='<span class="bad">Unauthorized — check the token in the URL.</span>';return;}
    const d = await r.json(); const s=d.status;
    $('sub').textContent = `account ${s.account} · uptime ${Math.floor((s.uptime_sec||0)/60)}m · updated ${t(d.now)}`;
    $('badges').innerHTML =
      badge('Environment', s.environment.toUpperCase(), s.live_trading?'bad':'info')+
      badge('Live trading', s.live_trading?'LIVE $$':'sim (paper)', s.live_trading?'bad':'ok')+
      badge('Risk controls', s.risk_enabled?'ON':'OFF', s.risk_enabled?'ok':'warn')+
      badge('Halted today', s.halted_today?'YES':'no', s.halted_today?'bad':'ok')+
      badge('Entries', s.entries_open?'OPEN':'closed', s.entries_open?'ok':'warn')+
      badge('Open positions', s.open_count, 'info')+
      badge('Watcher', s.watcher_ok?(s.watcher_issues?(s.watcher_issues+' issue'):'OK'):'CONN LOST', s.watcher_ok?(s.watcher_issues?'warn':'ok'):'bad')+
      badge('Account equity', '$'+Number(s.equity||0).toLocaleString(), '')+
      badge('P&L · '+(d.pnl_source||''), '$'+n(d.day_pnl), cls(d.day_pnl))+
      (d.ts_unrealized!=null ? badge('Open P&L', '$'+n(d.ts_unrealized), cls(d.ts_unrealized)) : '');
    const ls=d.last_signal;
    $('last').innerHTML = ls ? `<b class="${ls.ok?'ok':'bad'}">${(ls.action||'').toUpperCase()} ${ls.symbol||''}</b>
      &nbsp; ${ls.option_symbol||''} &nbsp; <span class="info">${t(ls.ts)}</span>
      ${ls.latency_ms?` · ⚡${Math.round(ls.latency_ms)}ms`:''} ${ls.order_id?` · order ${ls.order_id}`:''}
      <div class="sub">${ls.detail||''}</div>` : '<div class="empty">No signals yet.</div>';
    $('positions').innerHTML = rows(d.open_positions,[
      ['Symbol',r=>r.symbol],['Dir',r=>side(r.side)],['Contract',r=>r.option_symbol],
      ['Qty',r=>r.quantity,'num'],['Entry',r=>n(r.entry_price),'num'],['Now',r=>n(r.current_price),'num'],
      ['Unrl%',r=>`<span class="${cls(r.unrealized_pct)}">${pct(r.unrealized_pct)}</span>`,'num'],
      ['Unrl$',r=>`<span class="${cls(r.unrealized_usd)}">${n(r.unrealized_usd)}</span>`,'num'],
    ],'No open positions.');
    $('events').innerHTML = rows(d.events.slice(0,18),[
      ['Time',r=>t(r.ts)],['Kind',r=>r.kind],['Act',r=>r.action||''],['Sym',r=>r.symbol||''],
      ['OK',r=>r.ok?'<span class="ok">✓</span>':'<span class="bad">✗</span>'],
      ['ms',r=>r.latency_ms?Math.round(r.latency_ms):'','num'],
    ],'No events yet.');
    $('trades').innerHTML = rows(d.trades.slice(0,18),[
      ['Entry (ET)',r=>tet(r.fill_time||r.opened_at)],['Exit (ET)',r=>tet(r.closed_at)],
      ['Sym',r=>r.symbol],['Dir',r=>side(r.side)],['Qty',r=>r.quantity,'num'],
      ['Entry $',r=>n(r.entry_price),'num'],['Exit $',r=>n(r.exit_price),'num'],
      ['P&L$',r=>`<span class="${cls(r.pnl)}">${n(r.pnl)}</span>`,'num'],
      ['P&L%',r=>`<span class="${cls(r.pnl_pct)}">${pct(r.pnl_pct)}</span>`,'num'],
      ['MaxDD',r=>`<span class="bad">${pct(r.max_drawdown_pct)}</span>`,'num'],
      ['Why',r=>r.exit_reason||''],['Via',r=>r.exit_method||''],
    ],'No closed trades yet. <span style="color:var(--mut)">Full §3.18 audit fields in the CSV export ↑</span>');
    $('dlcsv').href = '/export/trades?token='+encodeURIComponent(TOKEN);
    $('watcher').innerHTML = (!d.discrepancies||!d.discrepancies.length)
      ? '<div class="empty">✓ all clear — bot state matches the broker'+(s.watcher_ok?'':' <span class=bad>(connection issue)</span>')+'</div>'
      : rows(d.discrepancies,[['Type',r=>`<span class="${r.type=='unmanaged'?'bad':'warn'}">${r.type}</span>`],['Symbol',r=>r.symbol],['Detail',r=>r.detail]],'');
    $('cache').innerHTML = rows(d.cache,[
      ['Symbol',r=>r.symbol],['Underlying',r=>n(r.underlying),'num'],
      ['ATM Call',r=>r.call||'—'],['Call ask',r=>n(r.call_ask),'num'],
      ['ATM Put',r=>r.put||'—'],['Put ask',r=>n(r.put_ask),'num'],['Age',r=>r.age!=null?r.age+'s':'cold','num'],
    ],'Cache warming…');
    const c=d.config;
    $('config').innerHTML = `<table>
      <tr><th>Symbols</th><td>${c.symbols.join(', ')}</td></tr>
      <tr><th>Exit mode</th><td>${c.exit_mode||'—'}</td></tr>
      <tr><th>Entry / Exit fill</th><td>${c.entry_fill} / ${c.exit_fill}</td></tr>
      <tr><th>Expiration / Strike</th><td>${c.expiration} / ${c.strike}</td></tr>
      <tr><th>Sizing</th><td>${c.sizing}</td></tr>
      <tr><th>Stop loss</th><td>${(c.stop_loss_pct*100).toFixed(0)}%</td></tr>
      <tr><th>Take-profit</th><td>${c.take_profit}</td></tr>
      <tr><th>Trims</th><td>${c.trims.join(', ')||'—'}</td></tr>
      <tr><th>Trailing</th><td>${c.trailing}</td></tr>
      <tr><th>Break-even</th><td>${c.breakeven}</td></tr>
      <tr><th>Max exposure</th><td>${c.max_exposure}</td></tr>
      <tr><th>Order retry/chase</th><td>${c.retry}</td></tr>
      <tr><th>Daily max loss</th><td>$${c.daily_max_loss_usd}</td></tr>
      <tr><th>Daily trailing stop</th><td>${c.daily_trailing_usd?('$'+c.daily_trailing_usd+' (peak P&L $'+(c.day_peak_pnl||0)+')'):'off'}</td></tr>
      <tr><th>Max losing trades</th><td>${c.max_losing_trades?c.max_losing_trades+'/day':'off'}</td></tr>
      <tr><th>Trading window</th><td>${c.schedule}</td></tr></table>`;
    $('groups').innerHTML = (!d.groups||!d.groups.length)
      ? '<div class="empty">no groups — all symbols use the global config</div>'
      : rows(d.groups,[['Group',g=>g.name],['Symbols',g=>g.symbols],['Stop',g=>g.stop],['Take-profit',g=>g.tp],['Size',g=>g.size]],'');
    const dlIn=$('dl_input'); if(dlIn && document.activeElement!==dlIn && d.config) dlIn.value=d.config.daily_max_loss_usd;
    const trIn=$('tr_input'); if(trIn && document.activeElement!==trIn && d.config) trIn.value=d.config.daily_trailing_usd||0;
    if($('tr_peak')&&d.config) $('tr_peak').textContent = d.config.daily_trailing_usd?('today peak P&L $'+(d.config.day_peak_pnl||0)):'';
    $('foot').textContent = 'auto-refreshing every 4s';
  }catch(e){ $('foot').textContent='connection error — retrying…'; }
}
async function saveDL(){
  const v=$('dl_input').value, b=$('dl_save'); b.disabled=true; $('dl_msg').textContent='saving…';
  try{
    const r=await fetch('/api/set-daily-loss?token='+encodeURIComponent(TOKEN)+'&value='+encodeURIComponent(v),{method:'POST'});
    const d=await r.json();
    $('dl_msg').innerHTML = d.ok ? '<span class="ok">✓ saved $'+Number(d.daily_max_loss_usd).toLocaleString()+'</span>'
                                 : '<span class="bad">✗ '+(d.error||'failed')+'</span>';
  }catch(e){ $('dl_msg').innerHTML='<span class="bad">✗ error</span>'; }
  b.disabled=false;
}
async function saveTrail(){
  const v=$('tr_input').value, b=$('tr_save'); b.disabled=true; $('tr_msg').textContent='saving…';
  try{
    const r=await fetch('/api/set-daily-trailing?token='+encodeURIComponent(TOKEN)+'&value='+encodeURIComponent(v),{method:'POST'});
    const d=await r.json();
    $('tr_msg').innerHTML = d.ok ? '<span class="ok">✓ '+(Number(d.daily_trailing_stop_usd)?('saved $'+Number(d.daily_trailing_stop_usd).toLocaleString()):'off')+'</span>'
                                 : '<span class="bad">✗ '+(d.error||'failed')+'</span>';
  }catch(e){ $('tr_msg').innerHTML='<span class="bad">✗ error</span>'; }
  b.disabled=false;
}
// ── baskets (loaded once; not on the 4s refresh, so edits aren't clobbered) ──
const DEF_BASKETS=[
 {name:"Basket 1",symbols:[],stop_loss_pct:0.20,take_profit_pct:0,trim_pct:0,trim_at:0,be_at:0,fix_trail_pct:0,
  exit_mode:"hybrid",entry_fill:"market",exit_fill:"market",expiration:"0DTE",strike_selection:"ATM",strike_offset:0,
  max_exposure_pct:0,trailing_enabled:false,trail_activate_pct:0.30,trail_pct:0.20,
  max_losing_trades:0,retry_max_attempts:20,retry_interval_sec:2,sizing_mode:"dollars",dollars:500,percent:5},
 {name:"Basket 2",symbols:[],stop_loss_pct:0.20,take_profit_pct:0,trim_pct:0.50,trim_at:0.30,be_at:0.30,fix_trail_pct:0,
  exit_mode:"hybrid",entry_fill:"market",exit_fill:"market",expiration:"0DTE",strike_selection:"ATM",strike_offset:0,
  max_exposure_pct:0,trailing_enabled:false,trail_activate_pct:0.30,trail_pct:0.20,
  max_losing_trades:0,retry_max_attempts:20,retry_interval_sec:2,sizing_mode:"dollars",dollars:300,percent:5}
];
function binp(id,val,w){return `<input id="${id}" value="${val}" style="width:${w||60}px;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:5px;padding:4px 6px;font-size:12.5px">`;}
function pv(f){return f?Math.round(f*1000)/10:0;}
const bl=t=>`<span style="color:var(--mut);font-size:11.5px">${t}</span>`;
function renderBaskets(list){
  let h='';
  for(let i=0;i<2;i++){
    const b={...DEF_BASKETS[i],...(list[i]||{})};
    const rowStyle='display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:8px;font-size:12.5px;color:var(--fg)';
    h+=`<div style="border:1px solid var(--bd);border-radius:8px;padding:12px;margin-bottom:12px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${binp('b'+i+'_name',b.name||('Basket '+(i+1)),110)}
        ${bl('symbols:')} ${binp('b'+i+'_symbols',(b.symbols||[]).join(', '),320)}</div>
      <div style="${rowStyle}">
        ${bl('Contract →')}
        ${bl('Exit mode')} ${ssel('b'+i+'_exit',b.exit_mode,SEL.exit)}
        ${bl('Entry')} ${ssel('b'+i+'_ef',b.entry_fill,SEL.fill)}
        ${bl('Exit')} ${ssel('b'+i+'_xf',b.exit_fill,SEL.fill)}
        ${bl('Exp')} ${ssel('b'+i+'_exp',b.expiration,SEL.exp)}
        ${bl('Strike')} ${ssel('b'+i+'_ss',b.strike_selection,SEL.strike)} ${bl('+off')} ${binp('b'+i+'_off',b.strike_offset||0,44)}</div>
      <div style="${rowStyle}">
        ${bl('Exits →')}
        ${bl('Stop %')} ${binp('b'+i+'_sl',pv(b.stop_loss_pct))}
        ${bl('Take-profit %')} ${binp('b'+i+'_tp',pv(b.take_profit_pct))}
        ${bl('Trim %')} ${binp('b'+i+'_trimpct',pv(b.trim_pct))} ${bl('at %')} ${binp('b'+i+'_trimat',pv(b.trim_at))}
        ${bl('Break-even at %')} ${binp('b'+i+'_be',pv(b.be_at))}
        ${bl('Fixed trail %')} ${binp('b'+i+'_ftr',pv(b.fix_trail_pct))}
        ${bl('1-lot: full exit @trim')} ${schk('b'+i+'_olx',b.onelot_trim_exit)}</div>
      <div style="${rowStyle}">
        ${bl('Advanced →')}
        ${bl('Trailing')} ${schk('b'+i+'_tren',b.trailing_enabled)} ${bl('act %')} ${binp('b'+i+'_tract',pv(b.trail_activate_pct))} ${bl('trail %')} ${binp('b'+i+'_trpct',pv(b.trail_pct))}
        ${bl('Max exposure %')} ${binp('b'+i+'_maxexp',pv(b.max_exposure_pct))} ${bl('(0=off)')}
        ${bl('Max losing')} ${binp('b'+i+'_mlt',b.max_losing_trades||0,44)}
        ${bl('Max cons')} ${binp('b'+i+'_mc',b.max_contracts||10,50)}
        ${bl('Retry')} ${binp('b'+i+'_ra',b.retry_max_attempts||20,44)} ${bl('x')} ${binp('b'+i+'_ri',b.retry_interval_sec||2,44)} ${bl('s')}
        ${bl('Size')} <select id="b${i}_mode" style="background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:5px;padding:4px"><option value="dollars"${b.sizing_mode!=='percent'?' selected':''}>$</option><option value="percent"${b.sizing_mode==='percent'?' selected':''}>% eq</option></select> ${binp('b'+i+'_sizeval',b.sizing_mode==='percent'?(b.percent||5):(b.dollars||500),70)}</div>
    </div>`;
  }
  $('baskets').innerHTML=h;
}
async function loadBaskets(){
  try{ const r=await fetch('/api/baskets?token='+encodeURIComponent(TOKEN)); const d=await r.json();
    renderBaskets((d.baskets&&d.baskets.length)?d.baskets:DEF_BASKETS);
  }catch(e){ renderBaskets(DEF_BASKETS); }
}
async function saveBaskets(){
  const list=[];
  for(let i=0;i<2;i++){
    const mode=$('b'+i+'_mode').value, sv=parseFloat($('b'+i+'_sizeval').value)||0;
    const f=id=>{const v=parseFloat($('b'+i+'_'+id).value); return isNaN(v)?0:v/100;};   // % -> fraction
    const g=id=>{const v=parseFloat($('b'+i+'_'+id).value); return isNaN(v)?0:v;};        // raw number
    list.push({name:$('b'+i+'_name').value||('Basket '+(i+1)),
      symbols:$('b'+i+'_symbols').value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean),
      stop_loss_pct:f('sl'),take_profit_pct:f('tp'),trim_pct:f('trimpct'),trim_at:f('trimat'),be_at:f('be'),
      fix_trail_pct:f('ftr'),onelot_trim_exit:$('b'+i+'_olx').checked,
      exit_mode:$('b'+i+'_exit').value,entry_fill:$('b'+i+'_ef').value,exit_fill:$('b'+i+'_xf').value,
      expiration:$('b'+i+'_exp').value,strike_selection:$('b'+i+'_ss').value,strike_offset:Math.round(g('off')),
      max_exposure_pct:f('maxexp'),trailing_enabled:$('b'+i+'_tren').checked,
      trail_activate_pct:f('tract'),trail_pct:f('trpct'),
      max_losing_trades:Math.round(g('mlt')),max_contracts:Math.round(g('mc'))||10,
      retry_max_attempts:Math.round(g('ra'))||20,retry_interval_sec:g('ri')||2,
      sizing_mode:mode,dollars:mode==='dollars'?sv:500,percent:mode==='percent'?sv:5});
  }
  const b=$('basket_save'); b.disabled=true; $('basket_msg').textContent='saving…';
  try{ const r=await fetch('/api/baskets?token='+encodeURIComponent(TOKEN),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({baskets:list})});
    const d=await r.json();
    $('basket_msg').innerHTML=d.ok?'<span class="ok">✓ saved '+d.baskets.reduce((a,x)=>a+x.symbols.length,0)+' symbols</span>':'<span class="bad">✗ '+(d.error||'failed')+'</span>';
    if(d.ok) renderBaskets(d.baskets);
  }catch(e){ $('basket_msg').innerHTML='<span class="bad">✗ error</span>'; }
  b.disabled=false;
}
// ── strategy settings (loaded once; not on the 4s refresh, so edits aren't clobbered) ──
const SEL={
 'exit':['monitor','bracket','hybrid'],'fill':['market','mid','ask','bid'],
 'exp':['0DTE','NEXT'],'strike':['ATM','OTM','ITM']};
const DAYS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function ssel(id,val,opts){return `<select id="${id}" style="background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:5px;padding:4px 6px;font-size:12.5px">`+
  opts.map(o=>`<option${o===val?' selected':''}>${o}</option>`).join('')+`</select>`;}
function schk(id,val){return `<input type="checkbox" id="${id}"${val?' checked':''} style="vertical-align:middle">`;}
function renderSettings(s){
  const day=DAYS.map(d=>`<label style="margin-right:8px;color:var(--fg)">${schk('st_d_'+d,(s['schedule.days']||[]).includes(d))} ${d}</label>`).join('');
  $('settings').innerHTML=`<table>
    <tr><th>Window on</th><td>${schk('st_sc_en',s['schedule.enabled'])} &nbsp;·&nbsp; ${binp('st_sc_s',s['schedule.entry_start'],70)} – ${binp('st_sc_e',s['schedule.entry_end'],70)} ${(s['schedule.timezone']||'ET')}</td></tr>
    <tr><th>Days</th><td>${day}</td></tr>
    <tr><th>End-of-day flatten</th><td>${schk('st_fl_en',s['schedule.flatten_enabled'])} on &nbsp;·&nbsp; force-close all at ${binp('st_fl_at',s['schedule.flatten_at'],70)} ET &nbsp;<span style="color:var(--mut);font-size:11px">(no 0DTE into expiry)</span></td></tr>
  </table>`;
}
async function loadSettings(){
  try{ const r=await fetch('/api/settings?token='+encodeURIComponent(TOKEN)); const d=await r.json();
    if(d.settings) renderSettings(d.settings);
  }catch(e){}
}
async function saveSettings(){
  const body={
    'schedule.enabled':$('st_sc_en').checked,
    'schedule.entry_start':$('st_sc_s').value,'schedule.entry_end':$('st_sc_e').value,
    'schedule.days':DAYS.filter(d=>$('st_d_'+d).checked),
    'schedule.flatten_enabled':$('st_fl_en').checked,'schedule.flatten_at':$('st_fl_at').value};
  const b=$('set_save'); b.disabled=true; $('set_msg').textContent='saving…';
  try{ const r=await fetch('/api/settings?token='+encodeURIComponent(TOKEN),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    const nerr=d.errors?Object.keys(d.errors).length:0;
    $('set_msg').innerHTML=d.ok?'<span class="ok">✓ saved</span>'
      :'<span class="'+(nerr?'bad':'ok')+'">'+(nerr?('✗ invalid: '+Object.keys(d.errors).join(', ')):'saved')+'</span>';
    if(d.settings) renderSettings(d.settings);
  }catch(e){ $('set_msg').innerHTML='<span class="bad">✗ error</span>'; }
  b.disabled=false;
}
// ── trading mode (sim/live) ──
async function loadMode(){
  try{
    const r=await fetch('/api/mode?token='+encodeURIComponent(TOKEN)); const d=await r.json();
    const live=d.mode==='live';
    $('mode').className='';
    $('mode').innerHTML=`<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span style="font-size:16px;font-weight:700;padding:6px 16px;border-radius:8px;
        background:${live?'rgba(248,81,73,.15)':'rgba(63,185,80,.15)'};color:${live?'var(--red)':'var(--grn)'}">
        ${live?'🔴 LIVE — real money':'🟢 SIM (paper)'}</span>
      <span style="color:var(--mut);font-size:12.5px">account <b style="color:var(--fg)">${d.account}</b>
        ${d.live_account?` · live acct ${d.live_account}`:' · no live account configured'}</span>
      ${d.live_account?`<button onclick="switchMode('${live?'sim':'live'}')"
        style="background:${live?'var(--grn)':'var(--red)'};color:#fff;border:0;border-radius:6px;padding:7px 14px;font-weight:700;cursor:pointer">
        ${live?'Switch to SIM (paper)':'Switch to LIVE'}</button>`:''}
      <span id="mode_msg" style="font-size:12px;color:var(--mut)"></span></div>`;
  }catch(e){ $('mode').textContent='mode unavailable'; }
}
async function switchMode(m){
  if(m==='live'){
    const t=prompt('⚠️ REAL MONEY.\\nEvery TradingView alert will place LIVE orders on your brokerage account.\\n\\nType LIVE to confirm:');
    if(t!=='LIVE'){ $('mode_msg').textContent='cancelled'; return; }
  } else if(!confirm('Switch back to SIM (paper) trading?')) return;
  $('mode_msg').textContent='switching…';
  try{
    const r=await fetch('/api/set-mode?token='+encodeURIComponent(TOKEN)+'&mode='+m+(m==='live'?'&confirm=LIVE':''),{method:'POST'});
    const d=await r.json();
    $('mode_msg').innerHTML=d.ok?'<span class="ok">✓ switched</span>':'<span class="bad">✗ '+(d.error||'failed')+'</span>';
  }catch(e){ $('mode_msg').innerHTML='<span class="bad">✗ error</span>'; }
  loadMode();
}
loadMode();
loadSettings();
loadBaskets();
tick(); setInterval(tick, 4000);
</script></body></html>"""
