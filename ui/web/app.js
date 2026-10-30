'use strict';

const $ = (id) => document.getElementById(id);
const S = {
  ov: null,
  task: null,       // 选中任务 id
  sid: null,        // 选中会话 id
  tab: 'session',
  trans: null,
  consoleOff: 0,
  runKey: null,     // 用于识别"这是哪一轮"：换了轮次就重置控制台游标
  mode: 'continue', // composer 当前模式
  busy: false,
};

/* ------------------------------------------------------------ 工具 */

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function dur(sec) {
  if (sec == null || isNaN(sec)) return '';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function hhmmss(iso) {
  if (!iso) return '';
  const m = String(iso).match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(iso).slice(11, 19);
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

function renderRun() {
  const run = S.ov.run;
  const live = run.state === 'running';
  $('runDot').className = 'dot' + (live ? ' live' : '');
  $('runText').textContent = live
    ? (run.mode === 'new' ? '建工作区并跑' : '运行中')
    : '空闲';
  $('runClock').textContent = live
    ? dur(run.elapsed)
    : (run.last && run.last.rc != null
        ? `上次 exit=${run.last.rc} · ${dur(run.last.duration)}`
        : '');
  $('btnStop').disabled = !live;
  $('btnRun').disabled = live;
  $('btnRun').textContent = S.ov.work_exists ? '续跑' : '新建';

  const p = $('project');
  const proj = S.ov.project || '—';
  p.textContent = S.ov.work_exists ? `PROJECT: ${proj}` : '工作区未建';
  p.className = 'pill ' + (!S.ov.work_exists ? ''
    : proj === 'DONE' ? 'done'
    : proj === 'BLOCKED' ? 'blocked' : 'running');
}

/* ------------------------------------------------------------ 任务 */

function chipClass(status) {
  if (status === 'running' || status === 'exited' || status === 'timed_out' || status === 'interrupted') return status;
  return 'todo';
}

function renderTasks() {
  const box = $('tasks');
  const rows = S.ov.tasks || [];
  $('taskCount').textContent = rows.length ? ` ${rows.length}` : '';
  if (!rows.length) {
    box.innerHTML = '<div class="empty" style="padding:14px 12px">还没有任务</div>';
    return;
  }
  box.innerHTML = rows.map((t) => {
    const sub = [];
    if (t.attempts) sub.push(`<span>attempts <b>${t.attempts}</b></span>`);
    if (t.duration != null) sub.push(`<span>${dur(t.duration)}${t.budget ? ' / ' + dur(t.budget) : ''}</span>`);
    if (t.sid) sub.push(`<span>${esc(t.sid.slice(0, 8))}</span>`);
    if (t.summary) sub.push(`<span>summary <b>${esc(t.summary)}</b></span>`);
    return `<div class="task ${S.task === t.id ? 'sel' : ''}" data-task="${esc(t.id)}">
      <div class="name">${esc(t.id)}</div>
      <div class="chip ${chipClass(t.status)}">${esc(t.status)}</div>
      <div class="sub">${sub.join('')}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('[data-task]').forEach((el) => {
    el.onclick = () => selectTask(el.dataset.task);
  });
}

/* ------------------------------------------------------------ 会话 */

function renderSessions() {
  const box = $('sessions');
  let rows = S.ov.sessions || [];
  $('sessCount').textContent = rows.length ? ` ${rows.length}` : '';
  if (!rows.length) {
    box.innerHTML = '<div class="empty" style="padding:14px 12px">还没有会话</div>';
    return;
  }
  box.innerHTML = rows.map((s, i) => {
    const live = !String(s.event).startsWith('exit');
    return `<div class="sess ${S.sid === s.sid ? 'sel' : ''}" data-sid="${esc(s.sid)}" data-task="${esc(s.task)}">
      <div class="role ${esc(s.role)}">${esc(s.role)}</div>
      <div class="meta">${hhmmss(s.ts)} · ${esc(s.task)}</div>
      <div class="ev ${live ? 'live' : ''}">${live ? '进行中' : esc(s.event)}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('[data-sid]').forEach((el) => {
    el.onclick = () => selectSession(el.dataset.sid, el.dataset.task);
  });
}

/* ------------------------------------------------------------ 中栏 */

function tabsFor() {
  const t = [];
  t.push(['session', '会话']);
  if (S.task) {
    t.push(['task.md', 'task.md']);
    t.push(['log.md', 'log.md']);
    t.push(['summary.md', 'summary.md']);
    t.push(['.state', '.state']);
  }
  t.push(['loom.log', 'loom.log']);
  return t;
}

function renderTabs() {
  $('tabs').innerHTML = tabsFor().map(([k, label]) =>
    `<div class="tab ${S.tab === k ? 'on' : ''}" data-tab="${k}">${esc(label)}</div>`).join('');
  $('tabs').querySelectorAll('[data-tab]').forEach((el) => {
    el.onclick = () => { S.tab = el.dataset.tab; S.trans = null; renderTabs(); loadDetail(); };
  });
}

function blockHtml(b) {
  if (b.k === 'tool') {
    return `<div class="blk tool"><span class="tn">${esc(b.name)}</span>${esc(b.text)}</div>`;
  }
  if (b.k === 'result') return `<div class="blk result">${esc(b.text)}</div>`;
  if (b.k === 'think') return `<div class="blk think">${esc(b.text)}</div>`;
  return `<div class="blk">${esc(b.text)}</div>`;
}

function renderTranscript() {
  const d = S.trans;
  if (!d) { $('detail').innerHTML = '<div class="empty">加载中…</div>'; return; }
  if (d.error) {
    $('detail').innerHTML = `<div class="empty">${esc(d.error)}<br><br>
      会话 ${esc(S.sid || '')} 的转录没找到。可能它还没落盘，或者用的是别的 claude 目录。</div>`;
    return;
  }
  if (!d.records || !d.records.length) {
    $('detail').innerHTML = '<div class="empty">这个会话还没有内容</div>';
    return;
  }
  const head = `<div class="file-head">
      <span>${esc(S.sid)}</span><span>${d.records.length} 条 / 共 ${d.total}</span>
      <span>${esc(d.file || '')}</span></div>`;
  $('detail').innerHTML = head + d.records.map((r) => {
    const label = r.role === 'user' ? 'user' : 'assistant';
    const blocks = r.blocks.map(blockHtml).join('');
    return `<div class="msg ${label}">
      <div class="msg-head">${label}<span class="t">${hhmmss(r.ts)}</span></div>
      ${blocks}</div>`;
  }).join('');
  const box = $('detail');
  box.scrollTop = box.scrollHeight;
}

async function loadDetail() {
  if (S.tab === 'session') {
    if (!S.sid) { $('detail').innerHTML = '<div class="empty">左边选个会话</div>'; return; }
    $('detail').innerHTML = '<div class="empty">加载会话…</div>';
    S.trans = await api('/api/transcript?sid=' + encodeURIComponent(S.sid));
    renderTranscript();
    return;
  }
  const p = S.tab === 'loom.log' ? 'loom.log' : `tasks/${S.task}/${S.tab}`;
  const d = await api('/api/file?p=' + encodeURIComponent(p));
  if (d.error) { $('detail').innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
  if (!d.text) { $('detail').innerHTML = `<div class="empty">${esc(p)} 是空的</div>`; return; }
  $('detail').innerHTML = `<div class="file-head"><span>${esc(p)}</span><span>${d.size || 0} B</span></div>
    <div class="file-body">${esc(d.text)}</div>`;
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
  put($('humanFile'), hu, '人还没说过话');
}

/* ------------------------------------------------------------ 选择 */

function pickLatestSession(taskId) {
  const rows = S.ov.sessions || [];
  const hit = rows.find((s) => s.task === taskId);
  return hit ? hit.sid : null;
}

function selectTask(id) {
  S.task = id;
  const sid = pickLatestSession(id);
  if (sid) S.sid = sid;
  if (S.tab === 'session' && !S.sid) S.trans = null;
  S.trans = null;
  renderTasks(); renderSessions(); renderTabs(); loadDetail();
}

function selectSession(sid, task) {
  S.sid = sid;
  if (task && task !== '-') S.task = task;
  S.tab = 'session';
  S.trans = null;
  renderTasks(); renderSessions(); renderTabs(); loadDetail();
}

/* ------------------------------------------------------------ 轮询 */

async function refreshOverview() {
  const ov = await api('/api/overview');
  if (ov.error) return;
  S.ov = ov;

  // 新一轮开始 = 服务端把 console.log 换了。此时按字节偏移续读会读到错位的内容，
  // 所以清空重来。用 started 作为"这是哪一轮"的标记。
  const runKey = ov.run.state === 'running' ? ov.run.started : null;
  if (runKey !== S.runKey) {
    S.runKey = runKey;
    if (runKey) { S.consoleOff = 0; $('console').textContent = ''; }
  }

  if (!S.task) {
    const running = (ov.tasks || []).find((t) => t.status === 'running');
    if (running) S.task = running.id;
    else if ((ov.tasks || []).length) S.task = ov.tasks[0].id;
    if (S.task && !S.sid) S.sid = pickLatestSession(S.task);
    if (S.task) { renderTabs(); loadDetail(); }
  }
  renderRun(); renderTasks(); renderSessions();
  if (!S.ov.work_exists) {
    $('detail').innerHTML = '<div class="empty">这个目录里还没有 work/。<br><br>点右上角「续跑」写下目标，' +
      '就会建骨架并开始第一轮。</div>';
  }
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
}

/* ------------------------------------------------------------ 操作 */

function openComposer(mode, preset) {
  S.mode = mode;
  $('composer').hidden = false;
  const ta = $('instr');
  ta.value = preset || '';
  ta.placeholder = mode === 'new'
    ? '写下目标。会建 work/ 骨架（CLAUDE.md / OBJECTIVE.md / STATE.md），然后让 planner 拆第一轮。'
    : '一句话就够。会落进 human.log，接回上次的位置继续跑。';
  $('composerHint').textContent = mode === 'new'
    ? '等价于 loom.py -p "…"'
    : '等价于 loom.py -c -p "…"（留空则只 -c）';
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
  if (r.ok) { closeComposer(); toast(r.msg, 'ok'); }
  else toast(r.msg || '启动失败', 'err');
  refreshOverview(); pullConsole();
}

async function doStop() {
  if (S.busy) return;
  S.busy = true;
  $('btnStop').disabled = true;
  toast('正在打断，等它把 .state 和 summary 落盘…');
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
    if (S.tab === 'session' && S.sid) {
      const run = S.ov && S.ov.run;
      if (run && run.state === 'running') {
        const d = await api('/api/transcript?sid=' + encodeURIComponent(S.sid));
        if (d && d.records && (!S.trans || d.records.length !== S.trans.records.length)) {
          S.trans = d; renderTranscript();
        }
      }
    }
  }, 2500);
})();
