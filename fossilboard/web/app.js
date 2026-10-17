/* FossilBoard — фронтенд. Ванильный JS, без сборки и без фреймворков.
 * Пользовательский ввод никогда не попадает в innerHTML без экранирования:
 * функция esc() вызывается на каждом куске текста, который идёт в разметку.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const api = (path, opts = {}) => fetch(path, {
  headers: opts.body ? { 'Content-Type': 'application/json' } : {},
  ...opts,
}).then(async (r) => {
  const ctype = r.headers.get('Content-Type') || '';
  const data = ctype.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
});

const state = {
  projects: [],
  current: '',
  board: null,
  detail: null,
  filters: { search: '', module: '', session: '', parents: false },
};

/* ---------------------------------------------------------------- utils */
const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const icon = (name, size = 14) =>
  `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const numId = (id) => parseInt(String(id).split('-').pop(), 10) || 0;
const sortTasks = (list) => list.slice().sort((a, b) =>
  (a.parent_id || '').localeCompare(b.parent_id || '') || numId(a.id) - numId(b.id));

const collapseKey = () => `fossilboard:collapsed:${state.current}`;
const getCollapsed = () => {
  try { return new Set(JSON.parse(localStorage.getItem(collapseKey()) || '[]')); }
  catch { return new Set(); }
};
const setCollapsed = (set) => localStorage.setItem(collapseKey(), JSON.stringify([...set]));

function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('err', isErr);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3500);
}

function promptFor(title, value = '') {
  return new Promise((resolve) => {
    const dlg = $('#prompt-dialog');
    $('#prompt-title').textContent = title;
    const input = $('#prompt-input');
    input.value = value;
    dlg.returnValue = 'cancel';
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok' ? input.value.trim() : null), { once: true });
    dlg.showModal();
    input.focus();
    input.select();
  });
}

async function copyText(text, what = 'скопировано') {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what}: ${text}`);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    toast(`${what}: ${text}`);
  }
}

/* ------------------------------------------------------------- markdown */
function inlineMd(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, text, url) => {
      // внешние схемы не пропускаем: безопаснее текстом
      if (!/^(https?:|mailto:|\/|#)/i.test(url)) return `${text} (${url})`;
      const href = esc(url);
      return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    });
}

/** Минимальный markdown: заголовки, списки, код, цитаты, ссылки. Всё экранировано. */
function renderMarkdown(src) {
  const lines = String(src || '').split('\n');
  const out = [];
  let inCode = false, code = [], list = null;

  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of lines) {
    if (/^```/.test(raw)) {
      if (inCode) {
        out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
        code = []; inCode = false;
      } else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(raw); continue; }

    const heading = raw.match(/^(#{1,6})\s+(.*)$/);
    const bullet = raw.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = raw.match(/^\s*\d+[.)]\s+(.*)$/);
    const quote = raw.match(/^>\s?(.*)$/);

    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inlineMd(esc(heading[2]))}</h${level}>`);
    } else if (bullet || numbered) {
      const want = bullet ? 'ul' : 'ol';
      if (list !== want) { closeList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inlineMd(esc((bullet || numbered)[1]))}</li>`);
    } else if (quote) {
      closeList();
      out.push(`<blockquote>${inlineMd(esc(quote[1]))}</blockquote>`);
    } else if (raw.trim() === '') {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inlineMd(esc(raw))}</p>`);
    }
  }
  if (inCode && code.length) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  closeList();
  return out.join('\n');
}

/* ---------------------------------------------------------------- загрузка */
const boardPath = () => `/api/board?project=${encodeURIComponent(state.current)}`;

async function loadProjects() {
  const data = await api('/api/state');
  state.projects = data.projects;
  const sel = $('#project-select');
  sel.innerHTML = state.projects
    .map((p) => `<option value="${esc(p.id)}">${esc(p.id)} · ${esc(p.name)}</option>`).join('');
  const pick = data.current && state.projects.some((p) => p.repo_path === data.current)
    ? (state.projects.find((p) => p.repo_path === data.current) || {}).id
    : (state.projects[0] || {}).id;
  state.current = pick;
  if (pick) sel.value = pick;
  $('#version').textContent = data.version ? `v${data.version}` : '';
}

async function loadBoard() {
  if (!state.current) {
    $('#board').innerHTML = '<p class="col-empty">В реестре нет проектов. Создай: board.py init --id FB --name "FossilBoard" --repo .</p>';
    return;
  }
  state.board = await api(boardPath());
  const proj = state.board.project;
  $('#repo-path').textContent = proj.repo_path;
  $('#repo-path').title = proj.repo_path;
  $('#board-meta').textContent =
    `${proj.name} · задач ${state.board.tasks.length} · git ${state.board.git_head || '—'}`;
  fillModuleFilter();
  fillColumnSelect();
  render();
}

function moduleById(id) {
  return (state.board?.modules || []).find((m) => m.id === id) || null;
}

function fillModuleFilter() {
  const sel = $('#module-filter');
  const cur = sel.value;
  const mods = state.board.modules || [];
  sel.innerHTML = '<option value="">все</option>' +
    mods.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  sel.value = mods.some((m) => m.id === cur) ? cur : '';

  const sessions = [...new Set(state.board.tasks.map((t) => t.session_label).filter(Boolean))].sort();
  const ssel = $('#session-filter');
  const scur = ssel.value;
  ssel.innerHTML = '<option value="">любая</option>' +
    sessions.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  ssel.value = sessions.includes(scur) ? scur : '';
}

function fillColumnSelect() {
  const sel = $('#d-column');
  const cur = sel.value;
  sel.innerHTML = (state.board?.columns || [])
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  if (cur) sel.value = cur;
}

/* ---------------------------------------------------------------- фильтры */
function visibleTasks() {
  const b = state.board;
  if (!b) return [];
  const { search, module, session, parents } = state.filters;
  const needle = search.toLowerCase().trim();
  return b.tasks.filter((t) => {
    if (needle && !(`${t.title} ${t.description || ''}`.toLowerCase().includes(needle))) return false;
    if (module && t.module_id !== module) return false;
    if (session && t.session_label !== session) return false;
    if (parents && !b.tasks.some((c) => c.parent_id === t.id)) return false;
    return true;
  });
}

/* ---------------------------------------------------------------- рендер доски */
function render() {
  const b = state.board;
  if (!b) return;
  const shown = new Set(visibleTasks().map((t) => t.id));
  const hasFilter = state.filters.search || state.filters.module || state.filters.session || state.filters.parents;
  const collapsed = getCollapsed();
  const html = b.columns.map((col) => {
    const wip = b.wip[col.id] || { count: 0, wip_limit: null, over: false };
    const items = sortTasks(b.tasks.filter((t) => t.column_id === col.id));
    const cards = items.map((t) => {
      if (t.parent_id && !shown.has(t.id)) return '';
      if (hasFilter && !shown.has(t.id)) return '';
      return renderCard(t, b, collapsed);
    }).join('');
    return `<section class="col ${wip.over ? 'over-limit' : ''}" data-column="${esc(col.id)}">
      <header class="col-head">
        <span class="name">${esc(col.name)}</span>
        <span class="count">${items.length}</span>
        ${wip.wip_limit != null
          ? `<span class="wip-badge">WIP ${items.length}/${wip.wip_limit}</span>` : ''}
      </header>
      <div class="col-body" data-column="${esc(col.id)}">
        ${cards || '<p class="col-empty">пусто</p>'}
      </div>
    </section>`;
  }).join('');
  $('#board').innerHTML = html;
}

function subtaskProgress(id) {
  const kids = (state.board.tasks || []).filter((t) => t.parent_id === id);
  return { done: kids.filter((k) => k.column_id === 'done').length, total: kids.length };
}

function renderCard(task, b, collapsed) {
  const mod = moduleById(task.module_id);
  const kids = (b.tasks || []).filter((t) => t.parent_id === task.id);
  const prog = subtaskProgress(task.id);
  const isCollapsed = collapsed.has(task.id);
  const att = (task.attachments || []).length;
  const chips = [];
  if (mod) chips.push(`<span class="chip"><i class="dot" style="background:${esc(mod.color)}"></i>${esc(mod.name)}</span>`);
  if (task.estimate) chips.push(`<span class="chip">~${esc(task.estimate)} ч</span>`);
  if (task.session_label) chips.push(`<span class="chip">${esc(task.session_label)}</span>`);
  if (att) chips.push(`<span class="chip">📎 ${att}</span>`);
  if (task.parent_id) chips.push(`<span class="chip">${icon('child', 11)} ${esc(task.parent_id)}</span>`);

  const subBlock = kids.length ? `
    <div class="sub-row">
      <button class="sub-toggle" data-toggle="${esc(task.id)}" title="свернуть саб-задачи">${isCollapsed ? '▸' : '▾'}</button>
      <div class="bar"><i style="width:${prog.total ? Math.round(100 * prog.done / prog.total) : 0}%"></i></div>
      <span class="pill">${prog.done} / ${prog.total}</span>
    </div>
    ${isCollapsed ? '' : `<ul class="sub-list">${kids.map((k) => renderCard(k, b, collapsed)).join('')}</ul>`}
  ` : '';

  return `<article class="card ${state.detail?.task?.id === task.id ? 'selected' : ''}"
      draggable="true" data-id="${esc(task.id)}" style="border-left-color:${mod ? esc(mod.color) : '#565656'}">
    <div class="cid">${esc(task.id)}</div>
    <div class="ctitle">${esc(task.title)}</div>
    <div class="chips">${chips.join('')}</div>
    ${subBlock}
    <div class="nudge">
      <button data-nudge="-1" data-id="${esc(task.id)}" title="← левее">←</button>
      <button data-nudge="1" data-id="${esc(task.id)}" title="→ правее">→</button>
    </div>
  </article>`;
}

/* ---------------------------------------------------------------- drag&drop */
let dragId = null;

document.addEventListener('dragstart', (e) => {
  const card = e.target.closest('.card');
  if (!card) return;
  dragId = card.dataset.id;
  card.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
});

document.addEventListener('dragend', (e) => {
  const card = e.target.closest('.card');
  if (card) card.classList.remove('dragging');
  document.querySelectorAll('.col.dragover').forEach((c) => c.classList.remove('dragover'));
  dragId = null;
});

document.addEventListener('dragover', (e) => {
  const col = e.target.closest('.col');
  if (!col || !dragId) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  col.classList.add('dragover');
});

document.addEventListener('dragleave', (e) => {
  const col = e.target.closest('.col');
  if (col && !col.contains(e.relatedTarget)) col.classList.remove('dragover');
});

document.addEventListener('drop', async (e) => {
  const col = e.target.closest('.col');
  if (!col || !dragId) return;
  e.preventDefault();
  document.querySelectorAll('.col.dragover').forEach((c) => c.classList.remove('dragover'));
  await moveTask(dragId, col.dataset.column);
  dragId = null;
});

/* клики по доске: открыть карточку, свернуть саб-задачи, сдвинуть */
document.addEventListener('click', async (e) => {
  const toggle = e.target.closest('[data-toggle]');
  if (toggle) {
    const set = getCollapsed();
    set.has(toggle.dataset.toggle) ? set.delete(toggle.dataset.toggle) : set.add(toggle.dataset.toggle);
    setCollapsed(set);
    render();
    return;
  }
  const nudge = e.target.closest('[data-nudge]');
  if (nudge) {
    e.stopPropagation();
    const delta = Number(nudge.dataset.nudge);
    const task = state.board.tasks.find((t) => t.id === nudge.dataset.id);
    const cols = state.board.columns;
    const idx = cols.findIndex((c) => c.id === task.column_id);
    const next = cols[Math.max(0, Math.min(cols.length - 1, idx + delta))];
    if (next) await moveTask(task.id, next.id);
    return;
  }
  const card = e.target.closest('.card');
  if (card) openDetail(card.dataset.id);
});

async function moveTask(id, columnId) {
  const task = state.board.tasks.find((t) => t.id === id);
  if (!task || task.column_id === columnId) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/move?project=${encodeURIComponent(state.current)}`, {
      method: 'POST',
      body: JSON.stringify({ column_id: columnId }),
    });
    await loadBoard();
    if (state.detail?.task?.id === id) await openDetail(id);
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------------------------------------------------------------- карточка */
async function openDetail(id) {
  const data = await api(`/api/task?project=${encodeURIComponent(state.current)}&id=${encodeURIComponent(id)}`);
  state.detail = data;
  const t = data.task;
  $('#detail').hidden = false;
  $('#d-id').textContent = t.id;
  $('#d-title').value = t.title;
  $('#d-desc').value = t.description || '';
  $('#d-estimate').value = t.estimate ?? '';
  $('#d-session').value = t.session_label || '';
  $('#d-column').value = t.column_id;
  const msel = $('#d-module');
  msel.innerHTML = '<option value="">—</option>' +
    state.board.modules.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  msel.value = t.module_id || '';

  renderPreview();
  renderSubtasks(data);
  renderAttachments(data);
  renderCommits(data);
  renderTimeline(data);
  render();
}

function closeDetail() {
  $('#detail').hidden = true;
  state.detail = null;
  render();
}

function renderPreview() {
  const box = $('#d-preview');
  const text = $('#d-desc').value;
  box.innerHTML = renderMarkdown(text);
  box.hidden = !$('#d-preview-toggle').dataset.on;
}

function renderSubtasks(data) {
  const list = $('#d-subtasks');
  $('#d-progress').textContent = data.progress.total
    ? `${data.progress.done} / ${data.progress.total} готово` : '';
  list.innerHTML = data.subtasks.length ? data.subtasks.map((k) => {
    const col = state.board.columns.find((c) => c.id === k.column_id) || { name: k.column_id };
    return `<li><div class="row">
      <span><code>${esc(k.id)}</code> ${esc(k.title)}</span>
      <span class="chip ${k.column_id === 'done' ? 'ok' : ''}">${esc(col.name)}</span>
      <button class="icon-btn" data-open="${esc(k.id)}" title="открыть">${icon('move', 13)}</button>
    </div></li>`;
  }).join('') : '<li class="att-size">саб-задач нет</li>';
}

function renderAttachments(data) {
  const list = $('#d-attachments');
  const items = data.task.attachments || [];
  $('#d-attcount').textContent = items.length ? `${items.length}` : '';
  list.innerHTML = items.map((a) => {
    const isImage = /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(a.name);
    const url = `/api/attachments/${encodeURIComponent(data.task.id)}/${encodeURIComponent(a.name)}?project=${encodeURIComponent(state.current)}`;
    const size = a.size > 1024 ? `${(a.size / 1024).toFixed(1)} KB` : `${a.size} B`;
    return `<li><div class="row">
      <span><span class="att-name">${esc(a.name)}</span><br>
        <span class="att-size">.board/${esc(a.path)} · ${size}</span></span>
      <span class="row">
        <button class="btn" data-copy-path="${esc(a.path)}" title="скопировать путь">${icon('copy', 13)}</button>
        <a class="btn" href="${url}" ${isImage ? 'target="_blank"' : 'download'} title="${isImage ? 'открыть' : 'скачать'}">${icon('attach', 13)}</a>
        <button class="btn danger" data-detach="${esc(a.name)}" title="удалить вложение">${icon('trash', 13)}</button>
      </span>
    </div>
    ${isImage ? `<img class="thumb" src="${url}" alt="${esc(a.name)}" loading="lazy" data-zoom="${url}">` : ''}
    </li>`;
  }).join('');
}

function renderCommits(data) {
  const list = $('#d-commits');
  $('#d-commitcount').textContent = `${data.commits.length}`;
  if (!data.commits.length) {
    list.innerHTML = `<li class="att-size">${esc(data.commit_error
      ? `git недоступен: ${data.commit_error}`
      : 'коммитов нет — привяжи карточку хэштегом в сообщении коммита')}</li>
      <li><code class="att-size">${esc(data.suggested_commit)}</code>
      <button class="btn" data-copy-cmd="${esc(data.suggested_commit)}">${icon('copy', 13)} скопировать</button></li>`;
    return;
  }
  list.innerHTML = data.commits.map((c) => `<li><div class="row">
    <span><span class="hash">${esc(c.short)}</span>
      <span class="subject">${esc(c.subject)}</span><br>
      <span class="meta">${esc(String(c.date).slice(0, 19).replace('T', ' '))} · ${esc(c.author)}</span></span>
    <button class="btn" data-diff="${esc(c.hash)}">${icon('diff', 13)} Diff</button>
  </div></li>`).join('');
}

function renderTimeline(data) {
  const items = [];
  for (const ev of data.task.events || []) {
    items.push({ at: ev.at, kind: ev.kind, text: ev.payload, src: 'событие' });
  }
  for (const c of data.commits || []) {
    items.push({ at: c.date, kind: 'commit', text: c.subject, src: c.short });
  }
  items.sort((a, b) => String(a.at).localeCompare(String(b.at)));
  $('#d-timeline').innerHTML = items.map((i) => `<li>
    <span class="when">${esc(String(i.at).slice(0, 19).replace('T', ' '))}</span>
    <b>${esc(i.kind)}</b> ${esc(i.text || '')}
    ${i.src !== 'событие' ? `<span class="when"> · ${esc(i.src)}</span>` : ''}
  </li>`).join('') || '<li class="att-size">пусто</li>';
}

/* ---------------------------------------------------------------- действия */
$('#d-save').addEventListener('click', async () => {
  const t = state.detail.task;
  const body = {
    title: $('#d-title').value,
    description: $('#d-desc').value,
    module_id: $('#d-module').value || null,
    estimate: $('#d-estimate').value === '' ? null : Number($('#d-estimate').value),
    session_label: $('#d-session').value || null,
  };
  try {
    await api(`/api/tasks/${encodeURIComponent(t.id)}?project=${encodeURIComponent(state.current)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    });
    await loadBoard();
    await openDetail(t.id);
    toast(`${t.id} сохранена`);
  } catch (err) { toast(err.message, true); }
});

$('#d-column').addEventListener('change', async (e) => {
  const t = state.detail.task;
  await moveTask(t.id, e.target.value);
});

$('#d-preview-toggle').addEventListener('click', (e) => {
  const btn = e.currentTarget;
  btn.dataset.on = btn.dataset.on ? '' : '1';
  btn.textContent = btn.dataset.on ? 'правка' : 'предпросмотр';
  renderPreview();
});

$('#d-delete').addEventListener('click', async () => {
  const t = state.detail.task;
  const kids = state.board.tasks.filter((k) => k.parent_id === t.id).length;
  const ok = await promptFor(`Удалить ${t.id} «${t.title}»${kids ? ` и ${kids} саб-задач` : ''}? Напишите id для подтверждения:`, t.id);
  if (!ok || ok !== t.id) { toast('удаление отменено', true); return; }
  await api(`/api/tasks/${encodeURIComponent(t.id)}?project=${encodeURIComponent(state.current)}`, { method: 'DELETE' });
  closeDetail();
  await loadBoard();
  toast(`${t.id} удалена`);
});

$('#d-addsub').addEventListener('click', async () => {
  const title = await promptFor('Название саб-задачи:');
  if (!title) return;
  try {
    await api(`/api/tasks?project=${encodeURIComponent(state.current)}`, {
      method: 'POST',
      body: JSON.stringify({ title, parent_id: state.detail.task.id }),
    });
    await loadBoard();
    await openDetail(state.detail.task.id);
    toast('саб-задача создана');
  } catch (err) { toast(err.message, true); }
});

$('#d-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!$('#detail').hidden) closeDetail();
    else if (!$('#modules-panel').hidden) $('#modules-panel').hidden = true;
  }
});

/* клики внутри панели карточки */
$('#detail').addEventListener('click', async (e) => {
  const open = e.target.closest('[data-open]');
  if (open) return openDetail(open.dataset.open);

  const copyPath = e.target.closest('[data-copy-path]');
  if (copyPath) return copyText(copyPath.dataset.copyPath, 'путь скопирован');

  const copyCmd = e.target.closest('[data-copy-cmd]');
  if (copyCmd) return copyText(copyCmd.dataset.copyCmd, 'команда скопирована');

  const detach = e.target.closest('[data-detach]');
  if (detach) {
    const t = state.detail.task;
    const ok = await promptFor(`Удалить вложение ${detach.dataset.detach}? Напишите имя файла:`, detach.dataset.detach);
    if (ok !== detach.dataset.detach) return;
    await api(`/api/attachments?project=${encodeURIComponent(state.current)}&id=${encodeURIComponent(t.id)}&name=${encodeURIComponent(detach.dataset.detach)}`, { method: 'DELETE' });
    await loadBoard(); await openDetail(t.id);
    return;
  }

  const diff = e.target.closest('[data-diff]');
  if (diff) {
    const box = $('#d-commits');
    const old = box.querySelector('.diff-box');
    if (old) { old.remove(); return; }
    try {
      const d = await api(`/api/diff?project=${encodeURIComponent(state.current)}&rev=${encodeURIComponent(diff.dataset.diff)}`);
      const wrap = document.createElement('div');
      wrap.className = 'diff-box';
      // строки патча экранируются целиком, теги добавляются по построчно
      const lines = d.text.split('\n').map((line) => {
        const cls = line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') ? 'meta'
          : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '';
        return cls ? `<span class="${cls}">${esc(line)}</span>` : esc(line);
      });
      wrap.innerHTML = `${d.truncated ? `<p class="truncated">⚠ вывод обрезан: ${d.total_lines} строк, показано ${d.text.split('\n').length}. Показать целиком: git show ${esc(d.rev)}</p>` : ''}<pre class="diff">${lines.join('\n')}</pre>`;
      diff.closest('li').appendChild(wrap);
    } catch (err) { toast(err.message, true); }
  }
});

$('#d-preview').addEventListener('click', (e) => {
  const img = e.target.closest('[data-zoom]');
  if (img) window.open(img.dataset.zoom, '_blank', 'noopener');
});

/* ---------------------------------------------------------------- вложения */
const dz = $('#dropzone');
const fileInput = $('#d-file');
$('#d-pick').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) uploadFiles([...fileInput.files]);
  fileInput.value = '';
});
['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault(); dz.classList.add('hot');
}));
['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault(); dz.classList.remove('hot');
}));
dz.addEventListener('drop', (e) => {
  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) uploadFiles(files);
});

async function uploadFiles(files) {
  const t = state.detail.task;
  for (const file of files) {
    try {
      const raw = await file.arrayBuffer();
      await api(`/api/tasks/${encodeURIComponent(t.id)}/attachments?project=${encodeURIComponent(state.current)}&name=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: raw,
      });
      toast(`${file.name} прикреплён`);
    } catch (err) { toast(`${file.name}: ${err.message}`, true); }
  }
  await loadBoard();
  await openDetail(t.id);
}

/* ---------------------------------------------------------------- модули/колонки */
$('#settings').addEventListener('click', async () => {
  $('#modules-panel').hidden = false;
  await loadProjects();
  await loadBoard();
  renderModules();
});
$('#m-close').addEventListener('click', () => { $('#modules-panel').hidden = true; });

function renderModules() {
  $('#m-list').innerHTML = (state.board?.modules || []).map((m) => {
    const n = state.board.tasks.filter((t) => t.module_id === m.id).length;
    return `<li><div class="row">
      <i class="dot" style="width:12px;height:12px;border-radius:50%;background:${esc(m.color)}"></i>
      <span>${esc(m.name)} <span class="att-size">· ${n}</span></span>
      <span class="row">
        <button class="btn" data-mrename="${esc(m.id)}" data-name="${esc(m.name)}">${icon('edit', 12)}</button>
        <button class="btn danger" data-mrm="${esc(m.id)}" data-name="${esc(m.name)}">${icon('trash', 12)}</button>
      </span>
    </div></li>`;
  }).join('') || '<li class="att-size">модулей нет</li>';

  $('#c-list').innerHTML = (state.board?.columns || []).map((c) => {
    const wip = state.board.wip[c.id] || {};
    return `<li><div class="row">
      <span>${esc(c.name)} <code class="att-size">${esc(c.id)}</code></span>
      <span class="att-size">${wip.count} карточек · WIP ${wip.wip_limit ?? '—'}</span>
    </div></li>`;
  }).join('');
}

$('#m-add').addEventListener('click', async () => {
  const name = $('#m-name').value.trim();
  if (!name) return;
  await api(`/api/modules?project=${encodeURIComponent(state.current)}`, {
    method: 'POST', body: JSON.stringify({ name, color: $('#m-color').value }),
  });
  $('#m-name').value = '';
  await loadProjects(); await loadBoard(); renderModules();
});

$('#m-list').addEventListener('click', async (e) => {
  const rename = e.target.closest('[data-mrename]');
  if (rename) {
    const name = await promptFor('Новое имя модуля:', rename.dataset.name);
    if (!name) return;
    await api(`/api/modules/${rename.dataset.mrename}?project=${encodeURIComponent(state.current)}`, {
      method: 'PATCH', body: JSON.stringify({ name }),
    });
    await loadBoard(); renderModules(); return;
  }
  const rm = e.target.closest('[data-mrm]');
  if (rm) {
    const ok = await promptFor(`Удалить модуль «${rm.dataset.name}»? Карточки останутся без модуля.`, 'удалить');
    if (ok !== 'удалить') return;
    await api(`/api/modules/${rm.dataset.mrm}?project=${encodeURIComponent(state.current)}`, { method: 'DELETE' });
    await loadBoard(); renderModules();
  }
});

/* ---------------------------------------------------------------- верхняя панель */
$('#new-task').addEventListener('click', async () => {
  const title = await promptFor('Название новой карточки:');
  if (!title) return;
  try {
    const task = await api(`/api/tasks?project=${encodeURIComponent(state.current)}`, {
      method: 'POST', body: JSON.stringify({ title, column_id: 'backlog' }),
    });
    await loadBoard();
    await openDetail(task.task.id);
  } catch (err) { toast(err.message, true); }
});

$('#refresh').addEventListener('click', async () => {
  await loadProjects();
  await loadBoard();
  if (state.detail?.task?.id) await openDetail(state.detail.task.id);
  toast('обновлено');
});

$('#project-select').addEventListener('change', async (e) => {
  state.current = e.target.value;
  closeDetail();
  await loadBoard();
});

$('#search').addEventListener('input', (e) => { state.filters.search = e.target.value; render(); });
$('#module-filter').addEventListener('change', (e) => { state.filters.module = e.target.value; render(); });
$('#session-filter').addEventListener('change', (e) => { state.filters.session = e.target.value; render(); });
$('#parents-filter').addEventListener('change', (e) => { state.filters.parents = e.target.checked; render(); });

/* ---------------------------------------------------------------- старт */
(async function boot() {
  try {
    await loadProjects();
    await loadBoard();
  } catch (err) {
    toast(err.message, true);
    $('#board').innerHTML = `<p class="col-empty">Не удалось загрузить доску: ${esc(err.message)}</p>`;
  }
})();