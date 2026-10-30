'use strict';

const $ = (id) => document.getElementById(id);
const S = {
  ov: null,
  task: null,       // 当前选中的任务 id
  sid: null,        // 当前选中的会话 id
  tab: 'session',
  open: new Set(),  // 展开了哪些任务（任务 id，或 '@loose'）
  booted: false,
  trans: null,
  consoleOff: 0,
  runKey: null,     // 新一轮就重置控制台游标
  mode: 'continue',
  busy: false,
  foldByUser: false,
};

/* ------------------------------------------------------------ 工具 */

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const CHEV = '<svg viewBox="0 0 8 10" aria-hidden="true"><path d="M1 1l5 4-5 4" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function dur(sec) {
  if (sec == null || isNaN(sec)) return '';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${String(s).padStart(2, '0')} 秒`;
  return `${s} 秒`;
}

function shortDur(sec) {
  if (sec == null || isNaN(sec)) return '';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}`;
  if (m) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function hhmm(iso) {
  if (!iso) return '';
  const m = String(iso).match(/T(\d{2}:\d{2})/);
  return m ? m[1] : String(iso).slice(11, 16);
}

function toast(msg, kind) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast ' + (kind || '');
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return { error: t }; }
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return { ok: false, msg: t }; }
}

/* ------------------------------------------------------------ 顶栏 */

const PROJECT_LABEL = { RUNNING: '进行中', DONE: '已完成', BLOCKED: '受阻' };

function renderRun() {
  const run = S.ov.run;
  const live = run.state === 'running';

  const p = $('project');
  if (!S.ov.work_exists) {
    $('projectText').textContent = '工作区未建';
    p.className = 'badge';
    p.title = '还没有 STATE.md';
  } else {
    const proj = S.ov.project || 'RUNNING';
    $('projectText').textContent = PROJECT_LABEL[proj] || proj;
    p.className = 'badge ' + (proj === 'DONE' ? 'done' : proj === 'BLOCKED' ? 'blocked' : 'running');
    p.title = 'PROJECT: ' + proj;
  }

  $('runBox').className = 'run' + (live ? ' live' : '');
  $('runText').textContent = live
    ? (run.mode === 'new' ? '建工作区并启动' : '运行中')
    : '空闲';
  $('runClock').textContent = live
    ? shortDur(run.elapsed)
    : (run.last && run.last.rc != null
        ? `上次退出 ${run.last.rc}${run.last.duration ? ' · 用时 ' + shortDur(run.last.duration) : ''}`
        : '');

  $('btnStop').disabled = !live;
  $('btnRun').disabled = live;
  $('btnRun').textContent = S.ov.work_exists ? '继续' : '新建';
}

/* ------------------------------------------------------------ 任务树 */

// 一个任务目录名形如 task-03-final-report → 序号 03 / 名字 final report
function splitId(id) {
  const m = String(id).match(/^task-(\d+)-(.+)$/);
  if (m) return { num: m[1], name: m[2].replace(/-/g, ' ') };
  return { num: '', name: String(id) };
}

// 会话流水是「每个事件一行」且倒序。按 sid 合并成一条：
// 第一条是 start（或唯一的 start），后面的行都算它的结束。
function sessionsById() {
  const rows = (S.ov.sessions || []).slice().reverse();   // 正序
  const map = new Map();
  for (const r of rows) {
    const cur = map.get(r.sid);
    if (!cur) {
      map.set(r.sid, { sid: r.sid, task: r.task, role: r.role, start: r.ts, end: null, event: r.event });
    } else {
      cur.end = r.ts;
      cur.event = r.event;
    }
  }
  return map;
}

// 任务状态：优先看 summary 的 status（那是"活儿干得怎么样"），没有才退回 .state
function taskState(t) {
  const s = (t.summary || '').toLowerCase();
  if (s === 'done') return { label: '完成', cls: 'done' };
  if (s === 'cancelled' || s === 'canceled') return { label: '取消', cls: '' };
  if (s === 'blocked') return { label: '受阻', cls: 'warn' };
  if (s === 'partial') return { label: '部分', cls: 'warn' };
  if (t.status === 'running') return { label: '进行中', cls: 'run' };
  if (t.status === 'timed_out') return { label: '超时', cls: 'warn' };
  if (t.status === 'interrupted') return { label: '已中断', cls: 'warn' };
  if (t.status === 'exited') return { label: '已退出', cls: '' };
  return { label: s || '未开始', cls: '' };
}

function taskMeta(t) {
  const bits = [];
  if (t.duration != null) bits.push('用时 ' + shortDur(t.duration));
  if (t.budget) bits.push('预算 ' + shortDur(t.budget));
  if (t.attempts > 1) bits.push(`${t.attempts} 次尝试`);
  return bits.join(' · ');
}

function groups() {
  const sess = sessionsById();
  const buckets = new Map();
  const loose = [];
  for (const s of sess.values()) {
    if (!s.task || s.task === '-') { loose.push(s); continue; }
    if (!buckets.has(s.task)) buckets.set(s.task, []);
    buckets.get(s.task).push(s);
  }
  const byStart = (a, b) => String(a.start).localeCompare(String(b.start));
  const tasks = (S.ov.tasks || []).map((t) => ({ ...t, sessions: (buckets.get(t.id) || []).sort(byStart) }));
  loose.sort(byStart);
  return { tasks, loose };
}

function sessRow(s) {
  const live = !s.end;
  const role = s.role || 'session';
  return `<div class="sess ${S.sid === s.sid ? 'sel' : ''}" data-sid="${esc(s.sid)}" data-task="${esc(s.task || '-')}"
      title="${esc(s.sid)}">
    <i class="s-dot ${esc(role)}"></i>
    <span class="s-role">${esc(role)}</span>
    ${live ? '<span class="s-live">进行中</span>' : ''}
    <span class="s-time">${hhmm(s.start)}</span>
  </div>`;
}

function renderTree() {
  const box = $('tree');
  const { tasks, loose } = groups();
  $('treeCount').textContent = tasks.length ? `${tasks.length} 个` : '';

  if (!tasks.length && !loose.length) {
    box.innerHTML = '<div class="placeholder">还没有任务。点右上角「新建」写下目标，就会建骨架并开始第一轮。</div>';
    return;
  }

  const html = [];
  for (const t of tasks) {
    const { num, name } = splitId(t.id);
    const st = taskState(t);
    const open = S.open.has(t.id);
    const meta = taskMeta(t);
    html.push(`<div class="node ${S.task === t.id ? 'sel' : ''}" data-task-row="${esc(t.id)}">
      <button class="twisty ${open ? 'open' : ''} ${t.sessions.length ? '' : 'empty'}"
              data-twist="${esc(t.id)}" tabindex="-1">${CHEV}</button>
      <span class="num">${esc(num || '·')}</span>
      <span class="t-name" title="${esc(t.id)}">${esc(name)}</span>
      <span class="t-state ${st.cls}">${esc(st.label)}</span>
    </div>`);
    if (open) {
      html.push('<div class="group-body">');
      html.push(t.sessions.length
        ? t.sessions.map(sessRow).join('')
        : '<div class="group-empty">还没有会话</div>');
      html.push('</div>');
    }
    if (meta && open) html.push(`<div class="t-meta">${esc(meta)}</div>`);
  }

  if (loose.length) {
    const open = S.open.has('@loose');
    html.push(`<div class="sect-title">编排</div>`);
    html.push(`<div class="node" data-loose-row>
      <button class="twisty ${open ? 'open' : ''}" data-twist="@loose" tabindex="-1">${CHEV}</button>
      <span class="t-name">每轮编排（planner）</span>
      <span class="t-state">${loose.length}</span>
    </div>`);
    if (open) html.push('<div class="group-body">' + loose.map(sessRow).join('') + '</div>');
  }

  box.innerHTML = html.join('');

  box.querySelectorAll('[data-twist]').forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); toggleOpen(el.dataset.twist); };
  });
  box.querySelectorAll('[data-task-row]').forEach((el) => {
    el.onclick = () => selectTask(el.dataset.taskRow);
  });
  box.querySelectorAll('[data-loose-row]').forEach((el) => {
    el.onclick = () => toggleOpen('@loose');
  });
  box.querySelectorAll('[data-sid]').forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); selectSession(el.dataset.sid, el.dataset.task); };
  });
}

function toggleOpen(key) {
  if (S.open.has(key)) S.open.delete(key); else S.open.add(key);
  renderTree();
}

/* ------------------------------------------------------------ 中栏 */

function tabsFor() {
  const t = [['session', '会话']];
  if (S.task) {
    t.push(['task.md', '任务契约'], ['log.md', '过程'], ['summary.md', '总结'], ['.state', '机器状态']);
  }
  t.push(['loom.log', '运行日志']);
  return t;
}

const TAB_TITLE = {
  'session': '', 'task.md': 'task.md', 'log.md': 'log.md',
  'summary.md': 'summary.md', '.state': '.state', 'loom.log': 'loom.log',
};

function renderTabs() {
  const tabs = tabsFor();
  if (!tabs.some(([k]) => k === S.tab)) S.tab = 'session';
  $('tabs').innerHTML = tabs.map(([k, label]) =>
    `<div class="tab ${S.tab === k ? 'on' : ''}" data-tab="${k}" title="${esc(TAB_TITLE[k])}">${esc(label)}</div>`
  ).join('');
  $('tabs').querySelectorAll('[data-tab]').forEach((el) => {
    el.onclick = () => { S.tab = el.dataset.tab; renderTabs(); loadDetail(); };
  });
}

// 只认最常用的两种行内标记：**粗体** 和 `代码`。
// 会话里满屏的星号比"渲染得不完美"更难看，但也不值得为此引一个 markdown 库。
function md(s) {
  return esc(s)
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>');
}

function blockHtml(b) {
  if (b.k === 'tool') {
    return `<div class="blk tool"><span class="tn">${esc(b.name)}</span>${esc(b.text)}</div>`;
  }
  if (b.k === 'result') return `<div class="blk result">${esc(b.text)}</div>`;
  if (b.k === 'think') return `<div class="blk think">${md(b.text)}</div>`;
  return `<div class="blk">${md(b.text)}</div>`;
}

function renderTranscript() {
  const d = S.trans;
  if (!d) { $('detail').innerHTML = '<div class="placeholder">正在读会话…</div>'; return; }
  if (d.error) {
    $('detail').innerHTML = `<div class="placeholder">这个会话的原文没找到。可能它还没落盘，
      或者用的是别的 claude 目录。<br><br><b>${esc(S.sid || '')}</b></div>`;
    return;
  }
  if (!d.records || !d.records.length) {
    $('detail').innerHTML = '<div class="placeholder">这个会话还没有内容</div>';
    return;
  }
  const box = $('detail');
  const prev = box.scrollTop;
  const stick = box.scrollHeight - prev - box.clientHeight < 80;   // 人在底部才自动跟新
  const head = `<div class="pane-head">
      <span class="title">${esc(S.sid.slice(0, 8))}</span>
      <span>${d.records.length} 条，共 ${d.total} 条</span>
      <span class="mono">${esc(shortPath(d.file))}</span>
    </div>`;
  box.innerHTML = head + d.records.map((r) => {
    const who = r.role === 'user' ? 'user' : 'assistant';
    return `<div class="msg ${who}">
      <div class="msg-head"><i class="dot"></i>${who}<span class="t">${hhmm(r.ts)}</span></div>
      ${r.blocks.map(blockHtml).join('')}
    </div>`;
  }).join('');
  box.scrollTop = stick ? box.scrollHeight : prev;
}

function shortPath(p) {
  if (!p) return '';
  const parts = String(p).split(/[\\/]/);
  return parts.slice(-2).join('/');
}

async function loadDetail() {
  if (S.tab === 'session') {
    if (!S.sid) {
      $('detail').innerHTML = S.task
        ? '<div class="placeholder">这个任务还没有会话。<br><br>点右上角「继续」，loom 会给它开一个 worker 会话。</div>'
        : '<div class="placeholder">从左边选一个会话</div>';
      return;
    }
    $('detail').innerHTML = '<div class="placeholder">正在读会话…</div>';
    S.trans = await api('/api/transcript?sid=' + encodeURIComponent(S.sid));
    renderTranscript();
    return;
  }
  const p = S.tab === 'loom.log' ? 'loom.log' : `tasks/${S.task}/${S.tab}`;
  const d = await api('/api/file?p=' + encodeURIComponent(p));
  if (d.error) {
    $('detail').innerHTML = `<div class="placeholder">${esc(d.error)}</div>`;
    return;
  }
  if (!d.text) {
    $('detail').innerHTML = `<div class="placeholder"><b>${esc(p)}</b> 还是空的</div>`;
    return;
  }
  $('detail').innerHTML = `<div class="pane-head">
      <span class="title">${esc(p.split('/').pop())}</span>
      <span class="mono">${esc(p)}</span>
      <span>${d.size || 0} 字节</span>
    </div><div class="file-body">${esc(d.text)}</div>`;
}

/* ------------------------------------------------------------ 右栏 */

async function renderFiles() {
  const [st, hu] = await Promise.all([
    api('/api/file?p=STATE.md'),
    api('/api/file?p=human.log'),
  ]);
  const put = (el, d, empty) => {
    const v = d && d.text ? d.text : empty;
    if (el.textContent !== v) {
      const keep = el.scrollTop;
      el.textContent = v;
      el.scrollTop = keep;
    }
  };
  put($('stateFile'), st, '还没有 STATE.md');
  put($('humanFile'), hu, '还没有人插过话。');
}

/* ------------------------------------------------------------ 选择 */

function latestSession() {
  const all = [...sessionsById().values()];
  if (!all.length) return null;
  return all.sort((a, b) => String(a.start).localeCompare(String(b.start))).pop();
}

function latestSessionOf(taskId) {
  const { tasks } = groups();
  const t = tasks.find((x) => x.id === taskId);
  return t && t.sessions.length ? t.sessions[t.sessions.length - 1] : null;
}

function setHash(h) {
  if (location.hash.slice(1) !== h) history.replaceState(null, '', '#' + h);
}

function selectTask(id) {
  S.task = id;
  S.open.add(id);
  const last = latestSessionOf(id);
  S.sid = last ? last.sid : null;
  S.trans = null;
  if (S.tab === 'session' && !S.sid) S.tab = 'task.md';
  setHash('task:' + id);
  renderTree(); renderTabs(); loadDetail();
}

function selectSession(sid, task) {
  S.sid = sid;
  if (task && task !== '-') { S.task = task; S.open.add(task); }
  S.tab = 'session';
  S.trans = null;
  setHash(task && task !== '-' && task !== '@loose' ? `task:${task}:${sid}` : `s:${sid}`);
  renderTree(); renderTabs(); loadDetail();
}

// 支持 #task:<id>[:<sid>] 与 #s:<sid>，刷新和分享都能落回同一个位置
function applyHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) return false;
  const [kind, a, b] = h.split(':');
  if (kind === 'task' && a) {
    const last = latestSessionOf(a);
    if (b) { S.task = a; S.open.add(a); selectSession(b, a); }
    else selectTask(a);
    return last !== null || !!b;
  }
  if (kind === 's' && a) { selectSession(a, ''); return true; }
  return false;
}

/* ------------------------------------------------------------ 轮询 */

async function refreshOverview() {
  const ov = await api('/api/overview');
  if (ov.error) return;
  S.ov = ov;

  // 新一轮 = 服务端把 console.log 换了，按旧偏移续读会错位
  const runKey = ov.run.state === 'running' ? ov.run.started : null;
  if (runKey !== S.runKey) {
    S.runKey = runKey;
    if (runKey) { S.consoleOff = 0; $('console').textContent = ''; }
  }

  if (!S.booted) {
    S.booted = true;
    if (applyHash()) { renderRun(); renderTree(); return; }
    const last = latestSession();
    if (last) {
      S.sid = last.sid;
      if (last.task && last.task !== '-') { S.task = last.task; S.open.add(last.task); }
      else S.open.add('@loose');
      S.tab = 'session';
    } else if ((ov.tasks || []).length) {
      S.task = ov.tasks[0].id;
      S.open.add(S.task);
      S.tab = 'task.md';
    }
    renderTabs(); loadDetail();
  }

  renderRun(); renderTree();
}

async function pullConsole() {
  const d = await api('/api/console?off=' + S.consoleOff);
  if (d.error) return;
  if (d.text) {
    const el = $('console');
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    el.textContent += d.text;
    if (el.textContent.length > 300000) el.textContent = el.textContent.slice(-200000);
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
  S.consoleOff = d.offset || 0;
  $('consoleMeta').textContent = d.size ? `${(d.size / 1024).toFixed(1)} KB` : '';

  // 没有内容时自动收起，免得底下挂着一条空白的槽；人手动折过就不再替他决定
  if (!S.foldByUser) {
    const empty = !d.size;
    $('consoleWrap').classList.toggle('folded', empty);
    $('btnFold').textContent = empty ? '展开' : '收起';
  }
}

/* ------------------------------------------------------------ 操作 */

function openComposer(mode, preset) {
  S.mode = mode;
  $('composer').hidden = false;
  const ta = $('instr');
  ta.value = preset || '';

  $('composerLabel').textContent = mode === 'new' ? '这个工作区要做什么？' : '跟它说一句';
  ta.placeholder = mode === 'new'
    ? '写下目标。会建 work/ 骨架（CLAUDE.md / OBJECTIVE.md / STATE.md），再让 planner 拆第一轮。'
    : '一句话就够。会写进 human.log，接回上次的位置继续跑。';
  $('composerHint').textContent = mode === 'new'
    ? '相当于 loom.py -p "…"'
    : '相当于 loom.py -c -p "…"（留空就是只 -c）';
  ta.focus();
}

function closeComposer() {
  $('composer').hidden = true;
  $('instr').value = '';
}

async function sendComposer() {
  if (S.busy) return;
  const text = $('instr').value.trim();
  if (S.mode === 'new' && !text) { toast('新建得先写目标', 'err'); return; }
  S.busy = true;
  $('btnSend').disabled = true;
  const r = await post('/api/run', { mode: S.mode, text });
  S.busy = false;
  $('btnSend').disabled = false;
  if (r.ok) { closeComposer(); toast(r.msg); }
  else toast(r.msg || '没启动起来', 'err');
  refreshOverview(); pullConsole();
}

async function doStop() {
  if (S.busy) return;
  S.busy = true;
  $('btnStop').disabled = true;
  toast('正在打断，等它把状态和总结落盘…');
  const r = await post('/api/stop', {});
  S.busy = false;
  toast(r.msg || (r.ok ? '已停止' : '没停下来'), r.ok ? 'ok' : 'err');
  await refreshOverview();
  openComposer('continue');
}

/* ------------------------------------------------------------ 事件 */

$('btnRun').onclick = () => openComposer(S.ov && S.ov.work_exists ? 'continue' : 'new');
$('btnStop').onclick = doStop;
$('btnCancel').onclick = closeComposer;
$('btnSend').onclick = sendComposer;
$('instr').onkeydown = (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendComposer(); }
  if (e.key === 'Escape') closeComposer();
};
$('btnBottom').onclick = () => { const el = $('console'); el.scrollTop = el.scrollHeight; };
$('btnFold').onclick = () => {
  const w = $('consoleWrap');
  w.classList.toggle('folded');
  S.foldByUser = true;
  $('btnFold').textContent = w.classList.contains('folded') ? '展开' : '收起';
};

/* ------------------------------------------------------------ 启动 */

(async function boot() {
  renderTabs();
  await refreshOverview();
  await renderFiles();
  await pullConsole();
  setInterval(refreshOverview, 2000);
  setInterval(pullConsole, 1200);
  setInterval(renderFiles, 3000);
  setInterval(async () => {
    if (S.tab !== 'session' || !S.sid) return;
    const run = S.ov && S.ov.run;
    if (!run || run.state !== 'running') return;
    const d = await api('/api/transcript?sid=' + encodeURIComponent(S.sid));
    if (d && d.records && (!S.trans || d.records.length !== S.trans.records.length)) {
      S.trans = d; renderTranscript();
    }
  }, 2500);
})();
