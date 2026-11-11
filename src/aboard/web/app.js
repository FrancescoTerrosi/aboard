'use strict';

const state = {
  view: 'board',
  tag: 'task',
  query: '',
  tid: null,
  streams: new Set(),
};

const $ = (sel) => document.querySelector(sel);
const detailEl = $('#detail');
const itemsEl = $('#items');
const subtitleEl = $('#subtitle');
const commandForm = $('#command-form');
const commandInput = $('#command');

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

function toast(message, level = 'info') {
  let box = $('.toasts');
  if (!box) {
    box = document.createElement('div');
    box.className = 'toasts';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function setActiveNav(view) {
  document.querySelectorAll('.views button').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === view);
  });
}

function setActiveTab(tag) {
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.tag === tag);
  });
}

function preview(value, limit = 60) {
  const text = String(value ?? '').replace(/\n/g, ' ');
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}

function fieldNames(tag) {
  return { task: ['instruction'], knowledge: ['name', 'context', 'text'],
           skill: ['name', 'context', 'text'] }[tag] || [];
}

function renderItems(data) {
  const rows = data.rows || [];
  itemsEl.innerHTML = '';
  for (const t of rows) {
    const id = t[1];
    const fields = fieldNames(data.tag);
    const first = fields.length ? preview(t[2]) : '';
    const div = document.createElement('div');
    div.className = 'item' + (id === state.tid ? ' selected' : '');
    div.innerHTML = `<div class="id">${esc(id)}</div>` +
      (first ? `<div class="preview">${esc(first)}</div>` : '');
    div.addEventListener('click', () => selectItem(data.tag, id, t));
    itemsEl.appendChild(div);
  }
  subtitleEl.textContent = `${rows.length} ${data.tag}`;
}

async function selectItem(tag, id, t) {
  state.tag = tag;
  state.tid = id;
  setActiveTab(tag);
  renderItems({ tag, rows: await api(`/api/items/${tag}?query=${encodeURIComponent(state.query)}`) });
  const fields = fieldNames(tag);
  detailEl.innerHTML = `<h2>${esc(tag)} <span style="color:var(--accent)">${esc(id)}</span></h2>` +
    fields.map((f, i) => `<p><strong>${esc(f)}:</strong> ${esc(t[2 + i] || '—')}</p>`).join('');
}

async function loadItems() {
  const data = await api(`/api/items/${state.tag}?query=${encodeURIComponent(state.query)}`);
  renderItems(data);
}

function renderTupleList(tuples, label) {
  if (!tuples || !tuples.length) return `<p class="muted">(none)</p>`;
  return `<h3>${esc(label)}</h3>` + tuples.map((t) =>
    `<div class="item">${t.map((v) => esc(v)).join(' · ')}</div>`).join('');
}

function renderProposals(data) {
  const ps = data.proposals || [];
  const reviews = data.reviews || {};
  if (!ps.length) return '<h2>Proposals</h2><p class="muted">(none pending)</p>';
  return '<h2>Proposals</h2>' + ps.map((t) => {
    const pid = t[1], kind = t[2], tag = t[3], values = t[4];
    const src = t[5] ? ` ← ${esc(t[5].tag)} ${esc(t[5].id)}` : '';
    const rv = reviews[pid] || [];
    const review = rv.length ? `<div class="muted">↳ review: ${esc(rv[rv.length - 1][2])} — ${esc(rv[rv.length - 1][3])}</div>` : '';
    return `<div class="item"><div class="id">${esc(pid)}</div>` +
      `<div>${esc(kind)} ${esc(tag)} ${esc(JSON.stringify(values))}${src}</div>${review}</div>`;
  }).join('');
}

function renderStatus(data) {
  const rows = [
    `<h2>Status</h2>`,
    `<p><strong>tasks pending:</strong> ${esc(data.tasks)}</p>`,
    `<p><strong>proposals pending:</strong> ${esc(data.proposals)}</p>`,
  ];
  if (data.agent_defs && data.agent_defs.length) {
    rows.push('<h3>agents</h3>');
    for (const d of data.agent_defs) {
      const running = data.workers && data.workers.some((w) => w.name === d[1]);
      rows.push(`<p>${esc(d[1])} (${esc(d[2])}) — ${running ? 'running' : 'idle'}</p>`);
    }
  } else rows.push('<p class="muted">agents: none</p>');
  if (data.workers && data.workers.length) {
    rows.push('<h3>workers</h3>');
    for (const w of data.workers) rows.push(`<p>${esc(w.name)}: ${esc(w.tasks_done)} done, ${esc(w.tokens_used)} tokens</p>`);
  }
  if (data.generic_agents && data.generic_agents.length) {
    rows.push('<h3>generic agents</h3>');
    for (const a of data.generic_agents) rows.push(`<p>${esc(a.name)} (${esc(a.role)}): ${esc(a.turns)} turn(s), ${esc(a.tokens_used)} tokens</p>`);
  }
  if (data.architect) {
    rows.push('<h3>architect</h3>');
    rows.push(`<p>${esc(data.architect.name)} (${esc(data.architect.role)}): ${esc(data.architect.turns)} turn(s), ${esc(data.architect.tokens_used)} tokens</p>`);
  }
  rows.push(`<p><strong>chat mode:</strong> ${esc(data.chat_mode)}</p>`);
  return rows.join('');
}

function renderChat(data) {
  const turns = data.turns || [];
  let html = '<h2>Chat</h2>';
  for (const t of turns.slice(-20)) {
    html += `<div class="item"><strong>${esc(t[1])}:</strong> ${esc(t[2])}</div>`;
  }
  if (data.pending) html += `<div class="item"><strong>you:</strong> ${esc(data.pending)}</div>`;
  if (data.thinking) html += '<div class="muted">…thinking</div>';
  return html;
}

function renderArchitect(data) {
  const turns = data.turns || [];
  let html = `<h2>Architect${data.name ? ' · ' + esc(data.name) : ''}</h2>`;
  for (const t of turns.slice(-20)) {
    html += `<div class="item"><strong>${esc(t[2])}:</strong> ${esc(t[3])}</div>`;
  }
  if (data.pending) html += `<div class="item"><strong>you:</strong> ${esc(data.pending)}</div>`;
  if (data.thinking) html += '<div class="muted">…thinking</div>';
  if (data.error) html += `<div class="error">architect failed: ${esc(data.error)}</div>`;
  return html;
}

function renderFiles(data) {
  let html = '<h2>Files</h2>';
  html += renderTupleList(data.projects, 'projects');
  html += renderTupleList(data.files, 'created');
  html += renderTupleList(data.errors, 'errors');
  return html;
}

function renderTuples(data) {
  if (data.tag) {
    return `<h2>Tuples: ${esc(data.tag)}</h2>` + renderTupleList(data.tuples, data.tag);
  }
  const groups = data.groups || {};
  let html = `<h2>Tuples (${esc(data.total || 0)})</h2>`;
  for (const name of Object.keys(groups).sort()) {
    html += renderTupleList(groups[name], name);
  }
  return html;
}

function renderSearch(data) {
  const rows = data.rows || [];
  let html = `<h2>Search: ${esc(data.tag)}</h2>`;
  if (!rows.length) return html + `<p class="muted">no exact match for ${esc(data.query)}</p>`;
  for (const t of rows) html += `<div class="item"><span class="id">${esc(t[1])}</span> ${esc(t[2])}</div>`;
  return html;
}

function renderStock(data) {
  if (!data.mounted) return '<h2>Stock</h2><p class="muted">(none mounted)</p>';
  return `<h2>Stock</h2><p><strong>path:</strong> ${esc(data.path)}</p>` +
    `<p><strong>items:</strong> ${esc(data.count)}</p>`;
}

function renderAgentDefs(data) {
  const defs = data.defs || [];
  const running = data.running || [];
  if (!defs.length) return '<h2>Agents</h2><p class="muted">(none defined)</p>';
  let html = '<h2>Agents</h2>';
  for (const d of defs) {
    const isRunning = running.includes(d[1]);
    html += `<div class="item"><span class="id">${esc(d[1])}</span> ${esc(d[2])} — ${isRunning ? 'running' : 'idle'}</div>`;
  }
  return html;
}

function renderLlmAgents(data) {
  const agents = data.agents || [];
  if (!agents.length) return '<h2>Generic agents</h2><p class="muted">(none running)</p>';
  let html = '<h2>Generic agents</h2>';
  for (const a of agents) {
    html += `<div class="item"><span class="id">${esc(a.name)}</span> ${esc(a.role)} · ${esc(a.tokens_used)} tokens · ${esc(a.turns)} turn(s)</div>`;
    for (const text of a.recent) html += `<div class="muted">- ${esc(text)}</div>`;
  }
  return html;
}

function renderHelp(data) {
  return `<h2>Help</h2>` +
    `<p>Use the command bar below. Examples: <code>/add task …</code>, <code>/chat hello</code>, <code>/architect start …</code>, <code>/status</code>.</p>` +
    `<p><strong>chat mode:</strong> ${esc(data.chat_mode)}</p>` +
    `<p><strong>stock:</strong> ${data.stock ? 'mounted' : 'none'}</p>` +
    `<p><strong>proposals pending:</strong> ${esc(data.proposals)}</p>`;
}

function renderView(view) {
  const data = view.data || {};
  const renderers = {
    proposals: renderProposals,
    status: renderStatus,
    chat: renderChat,
    architect: renderArchitect,
    files: renderFiles,
    tuples: renderTuples,
    search: renderSearch,
    stock: renderStock,
    agent_defs: renderAgentDefs,
    llm_agents: renderLlmAgents,
    help: renderHelp,
  };
  const fn = renderers[view.name];
  if (fn) {
    state.view = view.name;
    setActiveNav(view.name);
    detailEl.innerHTML = fn(data);
  }
}

async function refreshView() {
  if (state.view === 'board') return loadItems();
  const map = {
    proposals: '/api/proposals',
    status: '/api/status',
    chat: '/api/chat',
    architect: '/api/architect',
    files: '/api/files',
    tuples: '/api/tuples',
  };
  if (map[state.view]) {
    const data = await api(map[state.view]);
    renderView({ name: state.view, data });
  }
}

function applyResult(res) {
  for (const m of res.messages || []) toast(m.message, m.level);
  if (res.table) {
    state.tag = res.table.tag;
    state.tid = null;
    state.query = '';
    $('#filter').value = '';
    setActiveTab(state.tag);
    loadItems();
  }
  if (res.view) renderView(res.view);
  for (const jid of res.jobs || []) openJobStream(jid);
}

function openJobStream(jid) {
  if (state.streams.has(jid)) return;
  const es = new EventSource(`/api/jobs/${jid}/events`);
  state.streams.add(jid);
  es.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'message') toast(payload.message, payload.level);
    if (payload.type === 'view') renderView(payload.state);
    if (payload.type === 'table' && payload.state) {
      state.tag = payload.state.tag;
      state.tid = null;
      setActiveTab(state.tag);
      loadItems();
    }
    if (payload.type === 'state') refreshView();
    if (payload.type === 'done') {
      es.close();
      state.streams.delete(jid);
      refreshView();
    }
  };
  es.onerror = () => {
    es.close();
    state.streams.delete(jid);
  };
}

async function submitCommand(event) {
  event.preventDefault();
  const line = commandInput.value.trim();
  if (!line) return;
  commandInput.value = '';
  try {
    const res = await api('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        line,
        tag: state.tag,
        tid: state.tid,
        proposal_pid: state.tid,
      }),
    });
    applyResult(res);
  } catch (err) {
    toast(err.message, 'error');
  }
}

function wireNav() {
  document.querySelectorAll('.views button').forEach((b) => {
    b.addEventListener('click', async () => {
      const view = b.dataset.view;
      state.view = view;
      setActiveNav(view);
      if (view === 'board') return loadItems();
      const map = {
        proposals: '/api/proposals',
        status: '/api/status',
        chat: '/api/chat',
        architect: '/api/architect',
        files: '/api/files',
        tuples: '/api/tuples',
      };
      if (map[view]) {
        const data = await api(map[view]);
        renderView({ name: view, data });
      }
    });
  });

  document.querySelectorAll('.tabs button').forEach((b) => {
    b.addEventListener('click', () => {
      state.tag = b.dataset.tag;
      state.tid = null;
      setActiveTab(state.tag);
      loadItems();
    });
  });
}

function wireFilter() {
  const filter = $('#filter');
  filter.addEventListener('input', () => {
    state.query = filter.value;
    loadItems();
  });
}

commandForm.addEventListener('submit', submitCommand);
wireNav();
wireFilter();
loadItems();
