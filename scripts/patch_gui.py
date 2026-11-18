"""One-shot patcher: adds the AI Agent tab to the dashboard HTML."""

import io

path = "nse_bridge/gui/static/index.html"
src = io.open(path, encoding="utf-8").read()
assert 'data-page="aiagent"' not in src, "already patched"

NAV_ITEM = """    <div class="navitem" data-page="aiagent" style="margin-top:8px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="7" width="16" height="12" rx="3"/><circle cx="9" cy="13" r="1.6" fill="currentColor" stroke="none"/><circle cx="15" cy="13" r="1.6" fill="currentColor" stroke="none"/><path d="M12 7V4M8 4h8"/></svg>
      AI Agent <span class="pill" id="pill-agent" style="background:#2a1d4a;color:#c4b5fd">NEW</span></div>
  </nav>"""

old_nav_tail = """      Settings</div>
  </nav>"""
assert old_nav_tail in src
src = src.replace(old_nav_tail, "      Settings</div>\n" + NAV_ITEM, 1)

PAGE = """    <!-- ================= AI AGENT ================= -->
    <section class="page" id="page-aiagent">
      <div class="tophint">
        The <b>AI Agent</b> runs 5 engines (trend, momentum, volatility, news sentiment, risk math)
        through <b>9 decision gates</b> and the <b>COACH</b> discipline system. The aggression dial
        maps to risk-per-trade, required confidence and stop geometry — the math always sizes the trade,
        the LLM (optional) only reviews. <b>Observe</b> = analysis only · <b>Advisory</b> = propose, you confirm ·
        <b>Auto</b> = dispatch approved proposals automatically.
      </div>
      <div class="grid" style="margin-bottom:14px">
        <div class="card">
          <h2>Aggression Dial</h2>
          <input type="range" id="aggrSlider" min="0" max="100" value="45" style="width:100%;accent-color:#7c3aed">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--dim)">
            <span>0 · Sniper</span><span id="aggrLabel" style="color:var(--violet);font-weight:700;font-size:14px">45 · Balanced</span><span>100 · Degen</span>
          </div>
          <div class="kv" style="margin-top:10px"><span class="k">Risk / trade</span><span class="v" id="aggrRisk">—</span></div>
          <div class="kv"><span class="k">Required confidence</span><span class="v" id="aggrConf">—</span></div>
          <div class="kv"><span class="k">Stop (ATR mult)</span><span class="v" id="aggrSl">—</span></div>
          <div class="kv"><span class="k">Target (ATR mult)</span><span class="v" id="aggrTp">—</span></div>
        </div>
        <div class="card">
          <h2>Agent Control</h2>
          <div class="field"><label>Mode</label>
            <select id="agentMode"><option value="observe">Observe — analysis only</option>
              <option value="advisory">Advisory — propose, confirm manually</option>
              <option value="auto">Auto — dispatch approved proposals</option></select></div>
          <div class="field"><label>Symbol</label><input id="agentSymbol" placeholder="XAUUSD"></div>
          <div class="field"><label>Auto-execute consent <small>(required with mode=auto)</small></label>
            <select id="agentAutoExec"><option value="false">Off</option><option value="true">I consent to automated order dispatch</option></select></div>
          <div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap">
            <button class="btn" id="runAnalyze">🧠 Analyze now</button>
            <button class="btn ghost" id="agentExec">⚡ Execute proposal</button>
            <span id="agentMsg" style="font-size:12.5px"></span>
          </div>
        </div>
      </div>
      <div class="card" id="decisionCard" style="margin-bottom:14px;display:none">
        <h2>Decision
          <span style="display:flex;gap:10px;align-items:center">
            <span id="decisionBadge" style="font-weight:800;font-size:13px"></span>
            <span id="decisionConf" style="color:var(--dim);font-size:12.5px"></span>
          </span>
        </h2>
        <div id="gates" style="display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin-bottom:12px"></div>
        <div id="proposalBox"></div>
        <div id="llmBox"></div>
      </div>
      <div class="card">
        <h2>COACH — Discipline System <span id="coachBadge" style="font-size:12px"></span></h2>
        <div id="coachMsgs" class="empty">Run an analysis to see the coach verdict</div>
        <div id="coachStats" style="margin-top:8px;font-size:12px;color:var(--dim)"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h2>LLM Reviewer (optional — OpenAI-compatible)</h2>
        <div class="row">
          <div class="field"><label>Base URL</label><input id="llmUrl" placeholder="https://api.openai.com"></div>
          <div class="field"><label>API key</label><input id="llmKey" type="password" placeholder="sk-…"></div>
          <div class="field" style="max-width:180px"><label>Model</label><input id="llmModel" placeholder="gpt-4o-mini"></div>
        </div>
        <button class="btn small ghost" id="saveLlm">Save LLM settings</button>
        <span id="llmMsg" style="font-size:12.5px;color:var(--dim)"></span>
      </div>
    </section>

    <!-- ================= SETTINGS ================= -->"""

old_settings_head = """    <!-- ================= SETTINGS ================= -->"""
assert old_settings_head in src
src = src.replace(old_settings_head, PAGE, 1)

src = src.replace(
    "const titles = {overview:'Overview', positions:'Open Positions', orders:'Pending Orders', logs:'Bridge Logs', checks:'Connection Checks', settings:'Settings'};",
    "const titles = {overview:'Overview', positions:'Open Positions', orders:'Pending Orders', logs:'Bridge Logs', checks:'Connection Checks', settings:'Settings', aiagent:'AI Agent'};",
    1,
)
src = src.replace(
    """  if (el.dataset.page === 'checks') runChecks();
  if (el.dataset.page === 'settings') loadSettings();""",
    """  if (el.dataset.page === 'checks') runChecks();
  if (el.dataset.page === 'settings') loadSettings();
  if (el.dataset.page === 'aiagent') { loadAgentStatus(); }""",
    1,
)

AGENT_JS = r"""
/* ---------- AI AGENT ---------- */
const AGGR_LABELS = v => v<=20?'Sniper':v<=40?'Conservative':v<=60?'Balanced':v<=80?'Aggressive':'Degen';
function paintAggr(v){
  $('aggrLabel').textContent = v + ' · ' + AGGR_LABELS(v);
  $('aggrRisk').textContent = (0.25 + v*0.045).toFixed(3) + ' %';
  $('aggrConf').textContent = (95 - v*0.55).toFixed(1) + ' %';
  $('aggrSl').textContent = (2.2 - v*0.012).toFixed(2) + '\u00d7 ATR';
  $('aggrTp').textContent = (2.6 - v*0.01).toFixed(2) + '\u00d7 ATR';
}
$('aggrSlider').oninput = e => paintAggr(+e.target.value);
paintAggr(45);

async function loadAgentStatus(){
  try {
    const s = await api('agent/status', undefined, 'GET');
    $('agentMode').value = s.mode;
    $('agentSymbol').value = s.symbol || 'XAUUSD';
    $('aggrSlider').value = s.aggression;
    paintAggr(s.aggression);
    $('agentAutoExec').value = String(!!s.auto_execute);
    const pill = $('pill-agent');
    pill.textContent = s.mode.toUpperCase();
    pill.style.background = s.mode === 'auto' ? '#3a0e0e' : '#2a1d4a';
  } catch(e){}
}
$('saveLlm').onclick = async () => {
  try {
    await api('agent/config', {
      llm_base_url: $('llmUrl').value.trim(), llm_model: $('llmModel').value.trim(),
      llm_api_key: $('llmKey').value.trim() || undefined,
    }, 'PUT');
    $('llmMsg').textContent = 'saved';
  } catch(e){ $('llmMsg').textContent = 'failed: ' + (e.message||e); }
};
$('runAnalyze').onclick = async () => {
  const msg = $('agentMsg');
  msg.textContent = 'running engines…'; msg.className = '';
  try {
    await api('agent/config', {
      mode: $('agentMode').value, aggression: +$('aggrSlider').value,
      symbol: $('agentSymbol').value.trim(),
      auto_execute: $('agentAutoExec').value === 'true',
    }, 'PUT');
    const r = await api('agent/analyze', {}, 'POST');
    msg.textContent = '';
    paintDecision(r);
  } catch(e){ msg.textContent = 'failed: ' + (e.message||e); msg.className='down'; }
};
$('agentExec').onclick = async () => {
  const msg = $('agentMsg');
  try {
    const r = await api('agent/execute', {}, 'POST');
    msg.textContent = 'executed: ' + r.side + ' ' + r.volume + ' lots @ ' + r.price + ' (retcode ' + r.retcode + ')';
    msg.className = 'up';
  } catch(e){ msg.textContent = 'refused: ' + (e.message||e); msg.className='down'; }
};
function paintDecision(d){
  const card = $('decisionCard'); card.style.display = '';
  const badge = $('decisionBadge');
  const dir = d.direction !== 'none' ? d.direction.toUpperCase() : '\u2014';
  badge.textContent = d.decision + ' \u00b7 ' + dir + ' @ ' + d.confidence + '/100';
  badge.style.color = d.decision === 'TRADE' ? 'var(--green)' : 'var(--red)';
  $('decisionConf').textContent = 'required: ' + d.min_confidence + ' \u00b7 mode: ' + d.mode + ' \u00b7 aggression: ' + d.aggression;
  $('gates').innerHTML = d.gates.map(g => {
    const col = g.verdict==='pass'?'var(--green)':g.verdict==='warn'?'var(--amber)':'var(--red)';
    return '<div style="background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 11px">' +
      '<div style="display:flex;justify-content:space-between;font-size:12px">' +
      '<span style="color:var(--dim);text-transform:uppercase;letter-spacing:1px">' + g.name + '</span>' +
      '<span style="color:' + col + ';font-weight:700">' + g.score.toFixed(0) + '</span></div>' +
      '<div style="height:5px;background:#0d1521;border-radius:4px;margin:6px 0">' +
      '<div style="width:' + g.score + '%;height:100%;background:' + col + ';border-radius:4px"></div></div>' +
      '<div style="font-size:11px;color:var(--dim)">' + g.detail + '</div></div>';
  }).join('');
  const pr = d.proposal;
  $('proposalBox').innerHTML = pr ? (
    '<div style="background:#0f1b2c;border:1px solid #1c3049;border-radius:10px;padding:12px 14px;margin-top:6px">' +
    '<b style="color:var(--cyan)">Proposal</b> \u2014 <span class="badge ' + (pr.side==='buy'?'buy':'sell') + '">' + pr.side + '</span>' +
    ' entry <b>' + pr.entry + '</b> \u00b7 SL <b class="down">' + pr.sl + '</b> \u00b7 TP <b class="up">' + pr.tp + '</b>' +
    ' \u00b7 risk <b>' + pr.risk_pct + '%</b>' +
    '<div style="font-size:11px;color:var(--dim);margin-top:4px">' + pr.note + '</div></div>'
  ) : '<div class="empty">No proposal \u2014 the router said NO_TRADE.</div>';
  $('llmBox').innerHTML = d.llm ? (
    '<div style="margin-top:8px;font-size:12.5px"><b style="color:var(--violet)">LLM review:</b> ' +
    d.llm.critique + (d.llm.risks.length ? ' \u00b7 risks: ' + d.llm.risks.join(', ') : '') + '</div>'
  ) : '';
  const cb = $('coachBadge');
  cb.textContent = d.coach.verdict.toUpperCase() + ' (' + d.coach.score + ')';
  cb.style.color = d.coach.verdict==='pass'?'var(--green)':d.coach.verdict==='warn'?'var(--amber)':'var(--red)';
  $('coachMsgs').innerHTML = d.coach.messages.length
    ? d.coach.messages.map(m => '<div style="padding:4px 0">\ud83d\udca1 ' + m + '</div>').join('')
    : '<div style="color:var(--dim)">Discipline clean \u2014 keep it up.</div>';
  $('coachStats').textContent = JSON.stringify(d.coach.stats);
}
"""

src = src.replace("/* ---------- boot ---------- */", AGENT_JS + "\n/* ---------- boot ---------- */", 1)

io.open(path, "w", encoding="utf-8", newline="\n").write(src)
print("AI Agent tab added —", src.count('aiagent'), "occurrences")
