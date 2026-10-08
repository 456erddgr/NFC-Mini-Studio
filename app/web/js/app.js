/* ============================================================
   NFC Mini Studio · 前端逻辑 v2.2.0
   · 三通道连接（蓝牙 BLE / USB 串口 / USB 直连厂商通道）
   · 寻卡自动识卡（IC / ID / UID）+ 后台级联破解
   · 读写 / 复制 / 粘贴全流程进度条
   · 设备 3D 展示（真实照片贴图 + 苹果台式机式旋转动画）
   · macOS 液态玻璃外观设置（磨砂 / 高光 / 圆角 / 背景可换）
   ============================================================ */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  ready: false,
  connected: false,
  mode: '',
  address: '',
  settings: {},
  methods: [],
  chain: [],
  cards: [],
  dump: {},
  selectedMethod: 'staticnested',
  busy: false,
  selPort: '',
  spin: { on: true, speed: 18, deg: 0, dragging: false },
  crack: { handle: '', running: false, sector: 0, chain: [], attempts: [] },
  crack_mode: 'cloud',
  engine: null,
  wired: { devices: [], sel: null, connected: false, logMark: 0, timer: 0 },
  jobs: {},
  timer: 0,
};

/* ---------------------------------------------------------- 内置背景预设 */
const PRESETS = [
  { id: 'aurora', name: '极光',
    css: "radial-gradient(120% 120% at 15% 10%, #1b2a6b, transparent 55%)," +
         "radial-gradient(110% 110% at 85% 15%, #6a2a86, transparent 52%)," +
         "radial-gradient(120% 130% at 50% 105%, #0d4f6b, transparent 58%)," +
         "linear-gradient(150deg, #070a16, #0b0f22 45%, #050711)" },
  { id: 'sunset', name: '暮色',
    css: "radial-gradient(120% 120% at 10% 15%, #7a2f5e, transparent 55%)," +
         "radial-gradient(110% 110% at 90% 20%, #c85a34, transparent 52%)," +
         "radial-gradient(120% 120% at 45% 100%, #3a1d5e, transparent 58%)," +
         "linear-gradient(160deg, #14060f, #1c0a1e 50%, #07040c)" },
  { id: 'ocean', name: '深海',
    css: "radial-gradient(120% 120% at 20% 5%, #0f4f7a, transparent 55%)," +
         "radial-gradient(110% 110% at 85% 25%, #125e6e, transparent 52%)," +
         "radial-gradient(120% 130% at 55% 105%, #0a2f52, transparent 58%)," +
         "linear-gradient(150deg, #040a12, #061521 45%, #03080f)" },
  { id: 'forest', name: '苔原',
    css: "radial-gradient(120% 120% at 12% 12%, #2f5d3a, transparent 55%)," +
         "radial-gradient(110% 110% at 88% 18%, #6b7a2e, transparent 52%)," +
         "radial-gradient(120% 130% at 50% 105%, #1c3f4a, transparent 58%)," +
         "linear-gradient(150deg, #06100c, #0a1712 45%, #040906)" },
  { id: 'mono', name: '石墨',
    css: "radial-gradient(120% 120% at 20% 8%, #3a3a3c, transparent 55%)," +
         "radial-gradient(110% 110% at 82% 22%, #4a4a4d, transparent 52%)," +
         "radial-gradient(120% 130% at 50% 108%, #1c1c1e, transparent 58%)," +
         "linear-gradient(150deg, #0a0b10, #12141c 45%, #07080c)" },
];

/* ---------------------------------------------------------- 基础工具 */
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind = '') {
  const box = $('#toasts');
  if (!box) return;
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 420);
  }, 2600);
}

function log(text, level = 'info') {
  const body = $('#logBody');
  if (!body) return;
  const line = document.createElement('div');
  line.className = 'l-' + level;
  const t = document.createElement('span');
  t.className = 'l-time';
  t.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  line.appendChild(t);
  line.appendChild(document.createTextNode(String(text)));
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
  while (body.childElementCount > 600) body.removeChild(body.firstChild);
}

function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = text;
}

function setBadge(sel, text, kind = '') {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) return;
  el.textContent = text;
  el.className = 'badge ' + kind;
}

function busy(on, hint) {
  state.busy = on;
  setText('#tbHint', hint || (on ? '处理中…' : '等待操作'));
  document.body.style.cursor = on ? 'progress' : '';
}

function confirmBox(title, text) {
  return new Promise(resolve => {
    const mask = $('#modalMask');
    if (!mask) { resolve(window.confirm(title + '\n' + text)); return; }
    setText('#modalTitle', title);
    setText('#modalText', text);
    mask.classList.add('open');
    const done = ok => {
      mask.classList.remove('open');
      $('#modalOk').onclick = null;
      $('#modalCancel').onclick = null;
      resolve(ok);
    };
    $('#modalOk').onclick = () => done(true);
    $('#modalCancel').onclick = () => done(false);
  });
}

/* 后端接口就绪判定 */
const CORE_API = ['app_info', 'get_settings', 'save_settings', 'list_wallpapers',
                  'card_list', 'scan', 'scan_ports', 'connect', 'connect_usb',
                  'find_card', 'status',
                  /* 破解双模式：引擎状态与本地向量自检（后端 app/api.py L374/L379 暴露） */
                  'crack_engine_status', 'crack_vectors'];

function apiReady() {
  const a = (window.pywebview && window.pywebview.api) || null;
  if (!a) return false;
  return CORE_API.every(k => typeof a[k] === 'function');
}

/* v2.1 新增：USB 直连（厂商通道）必需接口 */
const WIRED_API = ['wired_devices', 'probe_wired', 'connect_wired',
                   'wired_send_frame', 'wired_log'];

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(method, ...args) {
  let fn = (window.pywebview && window.pywebview.api) ? window.pywebview.api[method] : undefined;
  for (let i = 0; i < 40 && typeof fn !== 'function'; i++) {
    await sleep(50);
    fn = (window.pywebview && window.pywebview.api) ? window.pywebview.api[method] : undefined;
  }
  if (typeof fn !== 'function') {
    throw new Error(apiReady() ? ('后端缺少接口：' + method) : '后端接口尚未就绪，请稍候再试');
  }
  const res = await fn(...args);
  if (res && res.ok === false) throw new Error(res.error || '操作失败');
  return res ? (res.data !== undefined ? res.data : res) : null;
}

/* ---------------------------------------------------------- 进度条工具 */
const JOB_UI = {
  dump:  { bar: '#dumpBar',  pct: '#dumpPct',  meta: '#dumpMeta',  time: '#dumpTime',  title: '读取整卡' },
  paste: { bar: '#writeBar', pct: '#writePct', meta: '#writeMeta', time: '#writeTime', title: '写入卡片' },
  write: { bar: '#writeBar', pct: '#writePct', meta: '#writeMeta', time: '#writeTime', title: '写入整卡' },
  writecard: { bar: '#writeCardBar', pct: '#writeCardPct', meta: '#writeCardMeta',
               time: '#writeCardTime', title: '独立写卡' },
  phonenfc: { bar: '#phoneNfcBar', pct: '#phoneNfcPct', meta: '#phoneNfcMeta',
              time: '#phoneNfcTime', title: '写录手机 / 手环 NFC' },
  scan:  { bar: '#scanBar', pct: '#scanPct', meta: '#scanMeta', time: '#scanTime',
           title: '自动识别 / 破解扫描' },
  copy:  { bar: '#copyBar',  pct: '#copyPct',  meta: '#copyMeta',  time: '#copyTime',  title: '复制整卡' },
};

function setBar(sel, pct, kind) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) return;
  const v = Math.max(0, Math.min(100, pct));
  el.style.width = v + '%';
  const box = el.parentElement;
  if (box && box.classList.contains('progress')) {
    box.classList.remove('done', 'fail');
    if (kind === 'done') box.classList.add('done');
    if (kind === 'fail') box.classList.add('fail');
    /* 有进度或进入完成/失败态即退出空闲占位态；归零重跑则回到占位态 */
    if (v > 0 || kind === 'done' || kind === 'fail') box.classList.remove('is-idle');
    else if (kind === 'run') box.classList.add('is-idle');
  }
}

/* 让进度条回到「空闲占位」外观：虚线轨道 + data-idle 文案，避免出现一行纯空白 */
function setBarIdle(sel, idle = true) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (!el) return;
  const box = el.parentElement;
  if (!box || !box.classList.contains('progress')) return;
  box.classList.remove('done', 'fail');
  box.classList.toggle('is-idle', !!idle);
  if (idle) el.style.width = '0%';
}

function resetProgress(job) {
  const ui = JOB_UI[job];
  state.jobs[job] = { t0: Date.now() };
  if (!ui) return;
  setBar(ui.bar, 0, 'run');
  setText(ui.pct, '0%');
  setText(ui.meta, '准备中…');
  setText(ui.time, '0.0 s');
}

function updateProgress(job, done, total, label) {
  const ui = JOB_UI[job];
  const r = state.jobs[job] || (state.jobs[job] = { t0: Date.now() });
  const pct = total ? (done / total * 100) : 0;
  const el = ((Date.now() - r.t0) / 1000).toFixed(1);
  if (ui) {
    setBar(ui.bar, pct, 'run');
    setText(ui.pct, Math.round(pct) + '%');
    setText(ui.meta, label || '');
    setText(ui.time, el + ' s');
  }
  dockShow(ui ? ui.title : job, pct, label || '', el + ' s');
}

function finishProgress(job, ok, label) {
  const ui = JOB_UI[job];
  const r = state.jobs[job] || {};
  const el = r.t0 ? ((Date.now() - r.t0) / 1000).toFixed(1) : '';
  if (ui) {
    setBar(ui.bar, ok ? 100 : 0, ok ? 'done' : 'fail');
    setText(ui.pct, ok ? '100%' : '—');
    if (label) setText(ui.meta, label);
    if (el) setText(ui.time, el + ' s');
  }
  dockDone(ui ? ui.title : job, ok);
  setTimeout(() => { if (ui) setBar(ui.bar, 0, 'run'); }, 2200);
}

/* 全局进度浮层 */
function dockShow(title, pct, meta, time) {
  const d = $('#progDock');
  if (!d) return;
  d.classList.remove('is-idle', 'is-ok', 'is-fail');
  setText('#progTitle', title);
  setText('#progPct', Math.round(pct) + '%');
  setText('#progMeta', meta || '—');
  setText('#progTime', time || '—');
  setBar('#progBar', pct, 'run');
}

function dockDone(title, ok) {
  const d = $('#progDock');
  if (!d) return;
  d.classList.remove('is-idle');
  d.classList.add(ok ? 'is-ok' : 'is-fail');
  setText('#progTitle', title + (ok ? ' · 完成' : ' · 失败'));
  setText('#progPct', ok ? '100%' : '—');
  setBar('#progBar', ok ? 100 : 0, ok ? 'done' : 'fail');
  setTimeout(() => {
    d.classList.add('is-idle');
    d.classList.remove('is-ok', 'is-fail');
  }, 2600);
}

/* ---------------------------------------------------------- 后端推送 */
window.__push = function (channel, payload) {
  try {
    if (channel === 'log') {
      log(payload.text, payload.level);
    } else if (channel === 'progress') {
      onProgress(payload);
    } else if (channel === 'crack') {
      onCrackEvent(payload);
    }
  } catch (e) { /* 推送异常不影响主流程 */ }
};

function onProgress(p) {
  const job = p.job || '';
  const ui = JOB_UI[job];
  const pct = typeof p.percent === 'number' ? p.percent
            : (p.total ? p.done / p.total * 100 : 0);
  const r = state.jobs[job] || (state.jobs[job] = { t0: Date.now() });
  const el = ((Date.now() - r.t0) / 1000).toFixed(1) + ' s';
  if (ui) {
    setBar(ui.bar, pct, 'run');
    setText(ui.pct, Math.round(pct) + '%');
    setText(ui.meta, p.label || '');
    setText(ui.time, el);
  }
  dockShow(ui ? ui.title : ('任务 ' + job), pct, p.label || '', el);
}

/* 破解完成后：对齐小程序的「问你怎么处理」——写录手机 / 手环 或 整卡克隆 */
async function askAfterCrack(sector, keys) {
  if (!state.connected) return;
  const keyText = (keys || []).filter(Boolean).join(' / ') || '—';
  const goPhone = await confirmBox('破解完成 · 选择下一步',
    `扇区 ${sector} 的密钥已拿到（${keyText}）。\n\n` +
    '要不要「写录手机 / 手表 NFC」？\n' +
    '确定 → 打开写录流程（先制转接卡，再让手机模拟）\n' +
    '取消 → 继续询问是否整卡克隆到新卡');
  if (goPhone) {
    const box = $('#phoneNfcResult');
    if (box) {
      box.innerHTML = '<h4>写录手机 / 手表 NFC 步骤</h4><pre class="steps">' +
        escapeHtml(phoneNfcStepsText()) + '</pre>';
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    toast('写录手机 / 手表：先制转接卡（放原卡）→ 再打开手机模拟实体卡', 'info');
    log('破解完成 → 选择「写录手机 / 手表 NFC」（0x27 转接卡流程）', 'info');
    return;
  }
  const goWrite = await confirmBox('写入新卡',
    '把刚读到的整卡数据克隆到一张新卡吗？\n（请先把新卡放到感应区，再点「写入整卡」）');
  if (goWrite) {
    toast('请放入新卡，然后点「写入整卡」完成克隆', 'info');
    log('破解完成 → 建议执行整卡克隆（写入整卡）', 'info');
  }
}
function onCrackEvent(p) {
  if (p.stage === 'start') {
    state.crack.chain = (p.chain || []).map(id => ({ id, state: 'wait', label: id, cost: 0, detail: '' }));
    state.crack.attempts = [];
    state.crack.sector = p.sector;
    renderChainMini();
    renderMethods();
    renderAttempts();
    setBadge('#autoCrackBadge', '级联破解中', 'info live');
    setBadge('#crackStateBadge', '级联中', 'info live');
    setBar('#crackBar', 2, 'run');
    setText('#crackPct', '0%');
    setText('#crackMeta', `扇区 ${p.sector} · 共 ${(p.chain || []).length} 种方式`);
    setBar('#crackBigBar', 2, 'run');
    setText('#crackBigPct', '0%');
    setText('#crackBigMeta', '正在启动级联破解…');
    state.jobs.crack = { t0: Date.now() };
    log(`开始级联破解（扇区 ${p.sector}，${(p.chain || []).length} 种方式）`, 'info');
  } else if (p.stage === 'attempt') {
    const row = state.crack.chain.find(c => c.id === p.method);
    if (row) {
      row.state = p.ok ? 'ok' : 'fail';
      row.label = p.label || p.method;
      row.cost = p.elapsed;
      row.detail = p.detail || '';
    }
    state.crack.attempts.push({ method: p.method, label: p.label || p.method,
                                ok: !!p.ok, elapsed: p.elapsed, detail: p.detail || '' });
    renderChainMini();
    renderMethods();
    renderAttempts();
    const t = state.jobs.crack && state.jobs.crack.t0
      ? ((Date.now() - state.jobs.crack.t0) / 1000).toFixed(1) + ' s' : '—';
    setText('#crackBigTime', t);
    setText('#dumpTime', $('#dumpTime') ? $('#dumpTime').textContent : '—');
    log(`${p.label || p.method} ${p.ok ? '成功' : '失败'}（${p.elapsed}s）` +
        (p.detail ? ' · ' + p.detail : ''), p.ok ? 'ok' : 'warn');
  } else if (p.stage === 'done') {
    if (p.ok) {
      setBadge('#autoCrackBadge', '破解成功', 'ok');
      setBadge('#crackStateBadge', '成功', 'ok');
      setBar('#crackBar', 100, 'done');
      setText('#crackPct', '100%');
      setText('#crackMeta', `扇区 ${p.sector} · ${p.label || p.method} · ${p.elapsed}s`);
      setBar('#crackBigBar', 100, 'done');
      setText('#crackBigPct', '100%');
      setText('#crackBigMeta', `扇区 ${p.sector} 破解成功`);
      showCrackKeys(p.keys || [], p.sector);
      toast(`破解成功：扇区 ${p.sector} 密钥已获得`, 'ok');      askAfterCrack(p.sector, p.keys || []);
    } else {
      setBadge('#autoCrackBadge', '破解未成功', 'warn');
      setBadge('#crackStateBadge', '未成功', 'warn');
      setBar('#crackBar', 100, 'fail');
      setText('#crackPct', '—');
      setText('#crackMeta', p.summary || '所有方式均未成功');
      setBar('#crackBigBar', 100, 'fail');
      setText('#crackBigPct', '—');
      setText('#crackBigMeta', p.summary || '所有方式均未成功');
      setText('#crackBigTime', (p.elapsed || 0) + ' s');
      showCrackFail(p.summary || '所有破解方式均未成功', p.attempts || []);
      toast('破解未成功，详见日志', 'err');
    }
  }
}

function renderChainMini() {
  const box = $('#chainMini');
  if (!box) return;
  if (!state.crack.chain.length) {
    box.innerHTML = '<div class="chain-row"><span>等待寻卡后自动开始</span></div>';
    return;
  }
  box.innerHTML = state.crack.chain.map((c, i) => {
    const cls = c.state === 'run' ? 'run' : (c.state === 'ok' ? 'ok'
              : (c.state === 'fail' ? 'fail' : (c.state === 'cancel' ? 'cancel' : '')));
    const st = c.state === 'ok' ? '✓ ' + (c.cost || 0) + 's'
             : (c.state === 'fail' ? '✗ ' + (c.cost || 0) + 's'
             : (c.state === 'run' ? '进行中' : '待命'));
    return `<div class="chain-row ${cls}"><span>${i + 1}. ${escapeHtml(c.label || c.id)}</span>` +
           `<span class="st">${escapeHtml(st)}</span></div>`;
  }).join('');
}

function renderAttempts() {
  const box = $('#crackAttempts');
  if (!box) return;
  if (!state.crack.attempts.length) { box.innerHTML = ''; return; }
  box.innerHTML = state.crack.attempts.map(a =>
    `<div class="attempt ${a.ok ? 'ok' : 'fail'}">` +
    `<span>${escapeHtml(a.label || a.method)}</span>` +
    `<span class="a-st">${a.ok ? '成功' : '失败'} · ${a.elapsed}s</span></div>`
  ).join('');
}

function showCrackKeys(keys, sector) {
  const html = '<h4>扇区密钥</h4>' + keys.map(k =>
    `<div><span class="k">扇区 ${sector}</span> → <span class="g">${escapeHtml(k)}</span></div>`
  ).join('') + '<p class="hint">点击可复制到剪贴板。</p>';
  ['#crackResultMini', '#crackResult'].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = html;
    el.classList.add('copyable');
    el.onclick = async () => {
      try {
        await api('clipboard_write', keys.join('\n'));
        toast('密钥已复制到剪贴板', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

function showCrackFail(summary, attempts) {
  const list = (attempts || []).map(a =>
    `<div><span class="r">✗</span> ${escapeHtml(a.label || a.method)} —— ` +
    `${escapeHtml(a.detail || '未成功')}（${a.elapsed}s）</div>`).join('');
  const html = `<h4>破解结果</h4><div><span class="r">未成功</span>：${escapeHtml(summary)}</div>` +
               (list ? '<h4>尝试记录</h4>' + list : '');
  ['#crackResultMini', '#crackResult'].forEach(sel => {
    const el = $(sel);
    if (el) { el.innerHTML = html; el.classList.remove('copyable'); el.onclick = null; }
  });
}

/* ---------------------------------------------------------- 导航 */
function switchView(name) {
  $$('.nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.view === name));
  $$('.view').forEach(v => v.classList.toggle('is-active', v.id === 'view-' + name));
  const titles = { device: '设备', card: '寻卡读写', crack: '密钥破解',
                   box: '卡夹', settings: '外观设置' };
  setText('#tbTitle', titles[name] || name);
  if (name === 'box') refreshBox();
  if (name === 'settings') refreshWalls();
}

/* ---------------------------------------------------------- 设备 3D 展示 */
function initDevice3D() {
  const stage = $('#deviceStage');
  const model = $('#deviceModel');
  if (!stage || !model) return;

  /* 机身厚度：注入多层同形圆角切片（替代原先会露出直角的四块侧板），保证旋转时外轮廓始终是圆角 */
  const shell = $('#d3Shell');
  if (shell && !shell.childElementCount) {
    const DEPTH = 18, LAYERS = 9;
    for (let i = 0; i < LAYERS; i++) {
      const t = ((i / (LAYERS - 1)) - 0.5) * 0.94;  /* -0.47 … +0.47 → z = -8.46px … +8.46px，严格小于正脸 ±9px */
      const shade = Math.abs(t) * 2;               /* 0（最外层）… 1（中间层） */
      const s = document.createElement('span');
      s.className = 'd3-slice' + (t <= -0.4 ? ' hi' : '');
      s.style.transform = `translateZ(${(t * DEPTH).toFixed(2)}px)`;
      s.style.filter = `brightness(${(1 - shade * 0.22).toFixed(3)})`;
      shell.appendChild(s);
    }
  }

  /* 正视图资源自检：切片收窄后正面由该 SVG 独立呈现，一旦它 404/解码失败正面就只剩底色
     （表现为"纯白块"），这里显式把加载失败暴露到控制台，便于定位是资源问题还是遮挡问题 */
  const face = $('#deviceFace');
  if (face) {
    const reportFace = () => {
      if (face.naturalWidth) return;
      console.warn('[device3d] 正视图未加载（正面会只剩底色）：' + face.getAttribute('src'));
    };
    if (face.complete) reportFace();
    else face.addEventListener('load', reportFace, { once: true });
    face.addEventListener('error', reportFace, { once: true });
  }

  function paint() {
    const deg = state.spin.deg;
    model.style.transform = `rotateX(-7deg) rotateY(${deg}deg)`;
    const glare = 0.5 + 0.5 * Math.cos(deg * Math.PI / 180);
    const g = $('.d3-glare');
    if (g) g.style.setProperty('--glare', glare.toFixed(3));
  }

  let last = performance.now();
  function loop(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    if (state.spin.on && !state.spin.dragging) {
      state.spin.deg = (state.spin.deg + state.spin.speed * dt) % 360;
      paint();
    }
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);

  let startX = 0, startDeg = 0;
  stage.addEventListener('pointerdown', e => {
    state.spin.dragging = true;
    startX = e.clientX;
    startDeg = state.spin.deg;
    stage.setPointerCapture && stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', e => {
    if (!state.spin.dragging) return;
    state.spin.deg = startDeg + (e.clientX - startX) * 0.6;
    paint();
  });
  const stop = () => { state.spin.dragging = false; };
  stage.addEventListener('pointerup', stop);
  stage.addEventListener('pointercancel', stop);
  stage.addEventListener('pointerleave', stop);
  paint();
}

function setLed(kind) {
  const led = $('#devLed');
  if (!led) return;
  led.className = 'd3-led' + (kind ? ' ' + kind : '');
  const map = { '': '未连接', on: '已连接', busy: '通信中', err: '异常' };
  setBadge('#ledBadge', map[kind] || '未连接', kind === 'on' ? 'ok' : (kind === 'busy' ? 'info live' : (kind === 'err' ? 'err' : '')));
}

/* ---------------------------------------------------------- 连接：蓝牙 */
function renderDevices(list) {
  const ul = $('#devList');
  if (!ul) return;
  ul.innerHTML = '';
  if (!list || !list.length) {
    ul.innerHTML = '<li class="empty">没有发现设备。请确认读卡器已开机，且与手机微信断开</li>';
    return;
  }
  list.forEach(d => {
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="dev-ico">◈</div>
      <div class="dev-main">
        <b>${escapeHtml(d.name || '未知设备')}</b>
        <span>${escapeHtml(d.address || '')}</span>
      </div>
      ${d.likely_reader ? '<span class="pill-tag">疑似读卡器</span>' : ''}
      <span class="dev-rssi">${d.rssi == null ? '—' : d.rssi} dBm</span>`;
    li.onclick = () => doConnect(d.address, d.name);
    ul.appendChild(li);
  });
}

async function doScan() {
  try {
    busy(true, '扫描蓝牙设备…');
    setBadge('#scanBadge', '扫描中', 'info live');
    const line = $('#scanline');
    if (line) line.classList.add('on');
    setHtml('#devList', '<li class="empty">正在搜索附近的 BLE 设备…</li>');
    const list = await api('scan', 6);
    renderDevices(list);
    setBadge('#scanBadge', `发现 ${list.length} 个`, list.length ? 'ok' : 'warn');
    log(`蓝牙扫描完成，发现 ${list.length} 个设备`, list.length ? 'ok' : 'warn');
  } catch (e) {
    toast('扫描失败：' + e.message, 'err');
    setBadge('#scanBadge', '失败', 'err');
    log('扫描失败：' + e.message, 'err');
  } finally {
    const line = $('#scanline');
    if (line) line.classList.remove('on');
    busy(false);
  }
}

function setHtml(sel, html) {
  const el = $(sel);
  if (el) el.innerHTML = html;
}

async function doConnect(address, name) {
  if (!address) { toast('设备地址为空', 'err'); return; }
  try {
    busy(true, '连接中…');
    setLed('busy');
    setText('#kvState', '连接中…');
    const snap = await api('connect', address);
    state.connected = true;
    state.mode = 'ble';
    state.address = address;
    applyConnectedUI(snap, address, name);
    toast('蓝牙已连接', 'ok');
    log(`已连接蓝牙设备 ${name || ''} ${address}`, 'ok');
    setTimeout(maybeAutoFind, 700);
  } catch (e) {
    state.connected = false;
    setLed('err');
    setText('#kvState', '连接失败');
    setBadge('#connBadge', '连接失败', 'err');
    toast('连接失败：' + e.message, 'err');
    log('连接失败：' + e.message, 'err');
  } finally {
    busy(false);
  }
}

async function doQuickConnect() {
  try {
    busy(true, '扫描中…');
    const list = await api('scan', 6);
    renderDevices(list);
    if (!list || !list.length) { toast('没有发现任何设备', 'err'); return; }
    const target = list.find(d => d.likely_reader) || list[0];
    log(`自动选择 ${target.name}（${target.address}）`);
    busy(false);
    await doConnect(target.address, target.name);
    return;
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    busy(false);
  }
}

/* ---------------------------------------------------------- 连接：USB 串口 */
function fillBaud(choices, current) {
  const sel = $('#baudSel');
  if (!sel) return;
  const list = choices && choices.length ? choices : [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600];
  sel.innerHTML = list.map(b => `<option value="${b}">${b}</option>`).join('');
  sel.value = String(current || 115200);
  if (!sel.value) sel.value = '115200';
}

function portName(p) {
  return p.port || p.device || p.name || '';
}

function renderPorts(data) {
  const ul = $('#portList');
  if (!ul) return;
  const ports = (data && data.ports) || [];
  ul.innerHTML = '';
  if (!ports.length) {
    ul.innerHTML = '<li class="empty">没有发现 USB 串口。请插好读卡器，必要时安装 CH340 / CP210x 驱动</li>';
    return;
  }
  ports.forEach((p, i) => {
    const name = portName(p);
    const desc = p.description || p.desc || p.hwid || p.chip || '';
    const li = document.createElement('li');
    li.dataset.port = name;
    li.innerHTML = `
      <div class="dev-ico">⌁</div>
      <div class="dev-main">
        <b>${escapeHtml(name)}</b>
        <span>${escapeHtml(desc || 'USB 串口设备')}</span>
      </div>
      <div class="dev-actions">
        <button class="btn sm" data-act="probe">握手</button>
        <button class="btn primary sm" data-act="conn">连接</button>
      </div>`;
    li.onclick = e => {
      if (e.target.closest('button')) return;
      $$('#portList li').forEach(x => x.classList.remove('sel'));
      li.classList.add('sel');
      state.selPort = name;
    };
    li.querySelectorAll('[data-act]').forEach(b => {
      b.onclick = ev => {
        ev.stopPropagation();
        state.selPort = name;
        $$('#portList li').forEach(x => x.classList.toggle('sel', x === li));
        if (b.dataset.act === 'conn') doConnectUsb(name);
        else doProbePort(name);
      };
    });
    ul.appendChild(li);
    if (i === 0) { state.selPort = name; li.classList.add('sel'); }
  });
}

async function doScanPorts() {
  try {
    busy(true, '枚举串口…');
    setBadge('#scanBadge', '枚举中', 'info live');
    setHtml('#portList', '<li class="empty">正在枚举本机串口…</li>');
    const data = await api('scan_ports');
    fillBaud(data.baud_choices, data.default_baud || (state.settings.baud || 115200));
    renderPorts(data);
    const n = ((data && data.ports) || []).length;
    const usb = ((data && data.usb_devices) || []).length;
    setBadge('#scanBadge', `串口 ${n} / USB ${usb}`, n ? 'ok' : 'warn');
    log(`串口枚举完成：${n} 个串口，${usb} 个 USB 设备`, n ? 'ok' : 'warn');
  } catch (e) {
    toast('枚举失败：' + e.message, 'err');
    setBadge('#scanBadge', '枚举失败', 'err');
  } finally {
    busy(false);
  }
}

async function doProbePort(port) {
  const target = port || state.selPort || currentPortFromList();
  if (!target) { toast('请先枚举并选择一个串口', 'err'); return; }
  const baud = +($('#baudSel') ? $('#baudSel').value : 115200) || 115200;
  try {
    busy(true, `握手测试 ${target}…`);
    const res = await api('probe_serial', target, baud);
    const ok = !!(res && (res.ok === undefined ? true : res.ok));
    const ver = res && (res.version || res.fw || '');
    log(`握手 ${target}@${baud}：${ok ? '收到协议响应' : '无有效响应'}` +
        (ver ? ` · 固件 ${ver}` : ''), ok ? 'ok' : 'warn');
    toast(ok ? `${target} 握手成功，可点「连接」` : `${target} 未按本协议应答`, ok ? 'ok' : 'err');
  } catch (e) {
    toast('握手失败：' + e.message, 'err');
    log(`握手 ${target} 失败：${e.message}`, 'err');
  } finally {
    busy(false);
  }
}

function currentPortFromList() {
  const li = $('#portList li.sel');
  return li ? li.dataset.port : '';
}

async function doConnectUsb(port) {
  const target = port || state.selPort || currentPortFromList();
  if (!target) { toast('请先枚举并选择一个串口', 'err'); return; }
  const baud = +($('#baudSel') ? $('#baudSel').value : 115200) || 115200;
  try {
    busy(true, 'USB 连接中…');
    setLed('busy');
    setText('#kvState', '连接中…');
    const snap = await api('connect_usb', target, baud);
    state.connected = true;
    state.mode = 'usb';
    state.address = target;
    applyConnectedUI(snap, target, `USB ${target}`);
    toast('USB 已连接', 'ok');
    log(`已通过 USB 连接 ${target} @ ${baud}`, 'ok');
    setTimeout(maybeAutoFind, 700);
  } catch (e) {
    state.connected = false;
    setLed('err');
    setText('#kvState', '连接失败');
    setBadge('#connBadge', '连接失败', 'err');
    toast('USB 连接失败：' + e.message, 'err');
    log('USB 连接失败：' + e.message, 'err');
  } finally {
    busy(false);
  }
}

/* ---------------------------------------------------------- USB 直连（厂商通道，v2.1） */
function hex2(v) {
  const n = Number(v || 0);
  return n ? '0x' + n.toString(16).padStart(2, '0').toUpperCase() : '—';
}

function currentWiredFromList() {
  const li = $('#wiredList li.sel');
  if (!li) return null;
  const key = li.dataset.key;
  return (state.wired.devices || []).find(d => (d.vid_pid + '#' + (d.serial || '')) === key
    || (d.vid_pid + '#' + d.vid_pid) === key) || null;
}

function renderWired(devices) {
  const ul = $('#wiredList');
  if (!ul) return;
  const list = devices || [];
  state.wired.devices = list;
  state.wired.sel = list[0] || null;
  ul.innerHTML = '';
  if (!list.length) {
    ul.innerHTML = '<li class="empty">未发现可直连的 USB 设备（需厂商自定义类 + Bulk 端点）。' +
      '请插好读卡器，必要时安装 WinUSB / libusb 驱动</li>';
    return;
  }
  list.forEach((d, i) => {
    const key = d.vid_pid + '#' + (d.serial || i);
    const li = document.createElement('li');
    li.dataset.key = key;
    const tag = d.known ? '已知型号' : '通用自定义类';
    li.innerHTML = `
      <div class="dev-ico">⌘</div>
      <div class="dev-main">
        <b>${escapeHtml(d.label || d.vid_pid)}
          <span class="badge ${d.known ? 'ok' : 'info'}">${tag}</span></b>
        <span>${escapeHtml(d.vid_pid)} · 接口 ${(d.interfaces || []).length} 个 ·
          bulk out ${hex2(d.bulk_out)} / in ${hex2(d.bulk_in)}
          ${d.serial ? ' · SN ' + escapeHtml(d.serial) : ''}</span>
      </div>
      <div class="dev-actions">
        <button class="btn sm" data-act="probe">握手</button>
        <button class="btn primary sm" data-act="conn">连接</button>
      </div>`;
    li.onclick = e => {
      if (e.target.closest('button')) return;
      $$('#wiredList li').forEach(x => x.classList.remove('sel'));
      li.classList.add('sel');
      state.wired.sel = d;
    };
    li.querySelectorAll('[data-act]').forEach(b => {
      b.onclick = ev => {
        ev.stopPropagation();
        state.wired.sel = d;
        $$('#wiredList li').forEach(x => x.classList.toggle('sel', x === li));
        if (b.dataset.act === 'conn') doConnectWired(d);
        else doProbeWired(d);
      };
    });
    ul.appendChild(li);
    if (i === 0) li.classList.add('sel');
  });
}

async function doScanWired() {
  try {
    busy(true, '发现有线设备…');
    setBadge('#wiredBadge', '枚举中', 'info live');
    const line = $('#scanline');
    if (line) line.classList.add('on');
    setHtml('#wiredList', '<li class="empty">正在枚举本机 USB 设备…</li>');
    const res = await api('wired_devices');
    const list = Array.isArray(res) ? res : ((res && res.devices) || []);
    renderWired(list);
    setBadge('#wiredBadge', list.length ? `发现 ${list.length} 个` : '未发现',
             list.length ? 'ok' : 'warn');
    log(`USB 有线枚举完成：${list.length} 个可直连设备`, list.length ? 'ok' : 'warn');
    if (!list.length) toast('未发现可直连的 USB 设备', 'warn');
  } catch (e) {
    setBadge('#wiredBadge', '枚举失败', 'err');
    toast('有线枚举失败：' + e.message, 'err');
    log('有线枚举失败：' + e.message, 'err');
  } finally {
    const line = $('#scanline');
    if (line) line.classList.remove('on');
    busy(false);
  }
}

async function doProbeWired(d) {
  const dev = d || state.wired.sel || currentWiredFromList();
  if (!dev) { toast('请先发现并选择一个 USB 设备', 'err'); return; }
  try {
    busy(true, `握手探测 ${dev.label || dev.vid_pid}…`);
    const res = await api('probe_wired', dev.vid, dev.pid, dev.serial || '');
    const ok = !!(res && res.ok);
    const sent = (res && res.sent) || [];
    if (sent.length) log('有线握手已发出：' + sent.join(' | '), 'info');
    if (res && res.raw) log('有线握手回包：' + res.raw, ok ? 'ok' : 'warn');
    setBadge('#wiredBadge', ok ? '握手成功' : '无应答', ok ? 'ok' : 'warn');
    setText('#wiredHint', (res && res.detail) || '—');
    toast((res && res.detail) || (ok ? '握手成功' : '设备无应答'), ok ? 'ok' : 'warn');
    log('有线握手结论：' + ((res && res.detail) || '—'), ok ? 'ok' : 'warn');
    refreshWiredLog();
  } catch (e) {
    toast('握手失败：' + e.message, 'err');
    log('有线握手失败：' + e.message, 'err');
  } finally {
    busy(false);
  }
}

async function doConnectWired(d) {
  const dev = d || state.wired.sel || currentWiredFromList();
  if (!dev) { toast('请先发现并选择一个 USB 设备', 'err'); return; }
  try {
    busy(true, 'USB 直连中…');
    setLed('busy');
    setText('#kvState', '连接中…');
    const snap = await api('connect_wired', dev.vid, dev.pid, dev.serial || '');
    state.connected = true;
    state.mode = 'wired';
    state.address = dev.vid_pid;
    state.wired.connected = true;
    state.wired.sel = dev;
    applyConnectedUI(snap, dev.vid_pid, dev.label);
    setBadge('#wiredBadge', '已连接', 'ok');
    const w = (snap && snap.wired) || {};
    log(`USB 直连已打开 ${dev.label || ''} ${dev.vid_pid}（接口 ${w.iface}，` +
        `bulk out ${hex2(dev.bulk_out)} / in ${hex2(dev.bulk_in)}）`, 'ok');
    toast('USB 直连已连接', 'ok');
    refreshWiredLog();
    setTimeout(maybeAutoFind, 700);
  } catch (e) {
    state.connected = false;
    state.wired.connected = false;
    setLed('err');
    setText('#kvState', '连接失败');
    setBadge('#wiredBadge', '连接失败', 'err');
    toast('USB 直连失败：' + e.message, 'err');
    log('USB 直连失败：' + e.message, 'err');
  } finally {
    busy(false);
  }
}

async function doWiredDisconnect() {
  try {
    await api('disconnect');
  } catch (e) { /* 忽略 */ }
  state.wired.connected = false;
  state.connected = false;
  setBadge('#wiredBadge', '未连接', '');
  toast('USB 直连已断开', 'ok');
  log('USB 直连已断开', 'info');
}

function parseHexFrame(text) {
  const clean = String(text || '').replace(/0x/gi, ' ').replace(/[^0-9a-fA-F]/g, ' ');
  const parts = clean.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '';
  return parts.map(p => p.padStart(2, '0').slice(-2)).join(' ').toUpperCase();
}

async function doSendWiredFrame() {
  const input = $('#wiredFrame');
  const hex = parseHexFrame(input ? input.value : '');
  if (!hex) { toast('请填写十六进制帧，例如 FF 06 01 00 00 00 00 FE', 'err'); return; }
  if (input) input.value = hex;
  try {
    busy(true, '试发帧…');
    const res = await api('wired_send_frame', hex, 1.5);
    const replies = (res && res.replies) || [];
    log(`有线试发 ${hex}（${(res && res.sent_len) || 0} 字节，耗时 ` +
        `${(res && res.elapsed) || 0}s）→ ` +
        (replies.length ? `收到 ${replies.length} 包回包` : '无应答'),
        replies.length ? 'ok' : 'warn');
    if (!replies.length) {
      toast('帧已发出，但设备暂未应答', 'warn');
      setText('#wiredHint', '帧已发出、无应答：固件可能仍使用私有帧格式，或端点需要先复位接口。');
    } else {
      replies.forEach(r => log('  ← ' + (r.hex || ''), 'ok'));
      toast('收到设备回包', 'ok');
    }
    refreshWiredLog();
  } catch (e) {
    toast('试发失败：' + e.message, 'err');
    log('有线试发失败：' + e.message, 'err');
  } finally {
    busy(false);
  }
}

async function refreshWiredLog(clear) {
  const box = $('#wiredLog');
  if (!box) return;
  try {
    const res = await api('wired_log', 160, !!clear);
    const items = (res && res.items) || [];
    if (!items.length) {
      box.innerHTML = '<span class="empty">原始收发日志为空</span>';
      return;
    }
    box.innerHTML = items.map(it => {
      const dir = it.dir === 'rx' ? 'rl-rx' : 'rl-tx';
      const arrow = it.dir === 'rx' ? '←' : '→';
      return `<div class="rl ${dir}"><span>${escapeHtml(it.ts || '')}</span>` +
             `<span>${arrow}</span><span>${escapeHtml(it.hex || '')}</span>` +
             `<span>(${it.len || 0}B)</span></div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  } catch (e) { /* 未连接时静默 */ }
}

async function clearWiredLog() {
  await refreshWiredLog(true);
  toast('已清空有线收发日志', 'ok');
}

function applyConnectedUI(snap, address, name) {
  snap = snap || {};
  const mode = snap.mode || state.mode || '';
  const modeText = mode === 'usb' ? 'USB 串口（有线）'
    : (mode === 'wired' ? 'USB 直连（有线）' : '蓝牙无线');
  setLed('on');
  setText('#kvState', '已连接');
  setText('#kvMode', modeText);
  setText('#kvMac', (mode === 'usb' || mode === 'wired')
    ? (snap.port || snap.wired && snap.wired.vid_pid || address || '—')
    : (snap.mac || address || '—'));
  setBadge('#connBadge', '已连接', 'ok');
  if (mode === 'wired') setBadge('#wiredBadge', '已连接', 'ok');
  const pill = $('#connPill');
  if (pill) {
    pill.classList.add('on');
    const s = pill.querySelector('span');
    if (s) s.textContent = snap.transport_label || name || '已连接';
  }
  if (snap.baud) {
    const sel = $('#baudSel');
    if (sel && !sel.value) sel.value = String(snap.baud);
  }
  api('device_info').then(info => {
    setText('#kvVer', info.version || '—');
    setText('#kvSn', info.sn || '—');
    const sn = $('#d3Sn');
    if (sn && info.sn) sn.textContent = 'SN ' + info.sn;
    refreshBattery();
  }).catch(() => {});
}

async function refreshBattery() {
  const show = !!(state.settings && state.settings.power_display);
  const el = $('#kvBattery');
  const row = el ? el.closest('.kv') : null;
  if (row) row.style.display = show ? '' : 'none';
  if (!show) return;
  try {
    const st = await api('device_state');
    setText('#kvBattery', (st && st.battery_text) ? st.battery_text : '—');
  } catch (e) { setText('#kvBattery', '—'); }
}
function maybeAutoFind() {
  if (!state.connected) return;
  if (state.mode === 'wired') {
    // 有线固件尚未确认按本协议应答，先不自动寻卡，避免刷一屏红色报错
    log('USB 直连已就绪：若固件支持本协议，可手动点「寻卡读卡」；有线模式暂不自动寻卡', 'info');
    return;
  }
  if (!(state.settings && state.settings.auto_find)) return;
  doFind(true);
}

async function doDisconnect() {
  try {
    await api('disconnect');
  } catch (e) { /* 忽略 */ }
  state.connected = false;
  state.mode = '';
  state.address = '';
  state.wired.connected = false;
  setLed('');
  setText('#kvState', '未连接');
  setText('#kvMode', '—');
  setText('#kvMac', '—');
  setText('#kvVer', '—');
  setText('#kvSn', '—');
  setBadge('#connBadge', '未连接');
  setBadge('#wiredBadge', '未连接', '');
  const pill = $('#connPill');
  if (pill) {
    pill.classList.remove('on');
    const s = pill.querySelector('span');
    if (s) s.textContent = '未连接';
  }
  $("#d3Sn") && ($('#d3Sn').textContent = 'SN —');
  toast('已断开连接', 'ok');
}

async function doRefreshInfo() {
  try {
    const info = await api('device_info');
    setText('#kvVer', info.version || '—');
    setText('#kvSn', info.sn || '—');
    const sn = $('#d3Sn');
    if (sn && info.sn) sn.textContent = 'SN ' + info.sn;
    refreshBattery();
    toast('设备信息已刷新', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

function switchConnTab(mode) {
  const isUsb = mode === 'usb' || mode === 'wired' || mode === 'serial';
  $('#tabBle') && $('#tabBle').classList.toggle('is-active', !isUsb);
  $('#tabUsb') && $('#tabUsb').classList.toggle('is-active', isUsb);
  $('#paneBle') && $('#paneBle').classList.toggle('is-hidden', isUsb);
  $('#paneUsb') && $('#paneUsb').classList.toggle('is-hidden', !isUsb);
  state.mode_tab = isUsb ? 'usb' : 'ble';
  if (isUsb && state.wired.connected) refreshWiredLog();
}

/* ---------------------------------------------------------- 寻卡 / 识卡 */
function chipBusy(on) {
  const c = $('#chipCard');
  if (!c) return;
  c.classList.toggle('is-busy', !!on);
  if (on) { c.classList.remove('is-found'); setText('#chipClass', '寻卡中…'); }
}

async function doFind(silent) {
  if (state.busy && !silent) { /* 允许重复寻卡 */ }
  try {
    busy(true, '寻卡中…');
    chipBusy(true);
    const c = await api('find_card');
    state.connected = true;
    fillCardInfo(c);
    const card = $('#chipCard');
    if (card) {
      card.classList.remove('is-busy');
      card.classList.add('is-found');
      setTimeout(() => card.classList.remove('is-found'), 2800);
    }
    const cls = $('#chipClass');
    if (cls) {
      cls.textContent = c.class_label || c.card_type || '未知卡';
      cls.className = 'chip-class ' + (c.crackable ? 'ok' : 'warn');
    }
    const uid = (c.uid || '').match(/.{1,2}/g);
    setText('#chipUid', uid ? uid.join(' ') : '—— —— —— ——');
    setBadge('#scanBadge', c.class_label || '已寻卡', 'ok');
    log(`寻卡成功：${c.class_label || ''} / ${c.card_type} / UID ${c.uid} / SAK ${c.sak}` +
        (c.class_note ? `（${c.class_note}）` : ''), 'ok');
    toast(`寻卡成功：${c.class_label || c.card_type}`, 'ok');

    if (c.crack_job) {
      log('已在后台启动自动级联破解，不影响继续读卡', 'info');
      startCrackPolling(c.crack_job, c);
    } else if (!c.crackable) {
      setBadge('#autoCrackBadge', '该卡不可破解', 'warn');
      setText('#crackMeta', 'ID 卡无密钥概念，已跳过破解');
      setBadge('#crackStateBadge', '不适用', 'warn');
    } else {
      setBadge('#autoCrackBadge', '待破解', 'warn');
      setText('#crackMeta', '已停止自动破解（设置中可开启）');
    }
    return c;
  } catch (e) {
    chipBusy(false);
    if (!silent) {
      toast('寻卡失败：' + e.message, 'err');
      setText('#chipClass', '未检测到卡片');
    }
    log('寻卡失败：' + e.message, silent ? 'info' : 'err');
  } finally {
    busy(false);
    chipBusy(false);
  }
}

function fillCardInfo(c) {
  setText('#fUid', c.uid || '—');
  setText('#fClass', (c.class_label || '—') + (c.class ? `（${c.class}）` : ''));
  setText('#fType', c.card_type || '—');
  setText('#fSak', c.sak || '—');
  setText('#fIsUid', c.is_uid_card ? '是' : '否');
  setText('#fBand', c.band || '—');
  setText('#kvChip', (c.class_label || '—') + ' / ' + (c.card_type || '—'));
}

/* ---------------------------------------------------------- 破解 */
function renderMethods() {
  const box = $('#methodList');
  const badge = $('#chainBadge');
  const list = state.methods || [];
  if (badge) badge.textContent = `${list.length} / ${(state.chain || list).length}`;
  if (!box) return;
  if (!list.length) { box.innerHTML = '<p class="hint">未读取到破解方式列表。</p>'; return; }
  box.innerHTML = list.map((m, i) => {
    const row = state.crack.chain.find(c => c.id === m.id);
    const st = row ? row.state : 'wait';
    const cls = st === 'run' ? 'run' : (st === 'ok' ? 'ok' : (st === 'fail' ? 'fail' : ''));
    const label = st === 'ok' ? `✓ ${row.cost}s` : (st === 'fail' ? `✗ ${row.cost}s`
                : (st === 'run' ? '进行中…' : (i === 0 ? '首选' : '待命')));
    return `<div class="method ${cls}" data-id="${m.id}">
      <div class="idx">${i + 1}</div>
      <div class="m-body">
        <div class="m-name">${escapeHtml(m.name)}</div>
        <div class="m-desc">${escapeHtml(m.desc || '')}</div>
      </div>
      <div class="m-st">${escapeHtml(label)}</div>
    </div>`;
  }).join('');
}

async function startCrackPolling(handle, cardInfo) {
  if (!handle) return;
  state.crack.handle = handle;
  state.crack.running = true;
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    try {
      const st = await api('job_status', handle);
      const pct = st.progress || 0;
      const stateName = st.state;
      const kind = stateName === 'done' ? 'done'
                 : ((stateName === 'error' || stateName === 'cancelled') ? 'fail' : 'run');
      setBar('#crackBar', pct, kind);
      setText('#crackPct', Math.round(pct) + '%');
      setText('#crackMeta', st.message || '');
      setBar('#crackBigBar', pct, kind);
      setText('#crackBigPct', Math.round(pct) + '%');
      setText('#crackBigMeta', st.message || '');
      if (st.elapsed != null) setText('#crackBigTime', st.elapsed + ' s');
      if (stateName === 'running') {
        setBadge('#autoCrackBadge', `破解中 ${Math.round(pct)}%`, 'info live');
        setBadge('#crackStateBadge', `破解中 ${Math.round(pct)}%`, 'info live');
        return;
      }
      clearInterval(state.timer);
      state.crack.running = false;
      const res = st.result || {};
      if (stateName === 'done' && res && res.ok) {
        const keys = res.keys || [];
        if (!state.crack.chain.some(c => c.state === 'ok')) {
          const row = state.crack.chain.find(c => c.id === res.method);
          if (row) { row.state = 'ok'; row.cost = res.elapsed || 0; row.label = res.label || res.method; }
        }
        renderChainMini();
        renderMethods();
        showCrackKeys(keys, res.sector != null ? res.sector : state.crack.sector);
        setBadge('#autoCrackBadge', '破解成功', 'ok');
        setBadge('#crackStateBadge', '成功', 'ok');
        setBar('#crackBar', 100, 'done');
        setText('#crackPct', '100%');
        setText('#crackMeta', `${res.label || res.method} · ${res.elapsed}s`);
        setBar('#crackBigBar', 100, 'done');
        setText('#crackBigPct', '100%');
        setText('#crackBigMeta', `扇区 ${res.sector} 破解成功（${res.label || res.method}）`);
        toast('破解成功，密钥已获得', 'ok');
        log(`破解成功：${(keys || []).join(', ')}`, 'ok');
      } else {
        const msg = st.error || (res && res.summary) || '未获得密钥';
        setBadge('#autoCrackBadge', '破解未成功', 'warn');
        setBadge('#crackStateBadge', '未成功', 'warn');
        showCrackFail(msg, (res && res.attempts) || []);
        setBar('#crackBar', 100, 'fail');
        setText('#crackPct', '—');
        setText('#crackMeta', msg);
        setBar('#crackBigBar', 100, 'fail');
        setText('#crackBigPct', '—');
        setText('#crackBigMeta', msg);
        log('破解未成功：' + msg, 'err');
      }
    } catch (e) {
      clearInterval(state.timer);
      state.crack.running = false;
      log('破解状态查询失败：' + e.message, 'err');
    }
  }, 900);
}

async function doCrackAgain() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const sector = +(state.settings.crack_sector || 0);
  const timeout = +(state.settings.crack_timeout || 25);
  const mode = state.crack_mode === 'local' ? 'local' : 'cloud';
  const ok = await confirmBox('重新级联破解',
    `扇区：${sector}\n模式：${mode === 'local' ? '本地离线（mfkey32）' : '云端解算（失败自动降级本地）'}\n` +
    `超时：${timeout} 秒（每种方式）\n` +
    '请保持卡片贴在读卡器上。\n\n确定开始吗？');
  if (!ok) return;
  try {
    state.crack.chain = (state.chain || state.methods.map(m => m.id))
      .map(id => ({ id, state: 'wait', label: (state.methods.find(m => m.id === id) || {}).name || id, cost: 0 }));
    renderChainMini();
    renderMethods();
    const res = await api('crack_start', sector, null, timeout, mode);
    startCrackPolling(res.handle);
    toast('已开始级联破解', 'ok');
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
    log('启动破解失败：' + e.message, 'err');
  }
}

async function doManualCrack() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const sector = +$('#crackSector').value;
  const keyType = +$('#crackKeyType').value;
  const timeout = +$('#crackTimeout').value || 25;
  const mode = state.crack_mode === 'local' ? 'local' : 'cloud';
  const ok = await confirmBox('手动破解',
    `扇区：${sector}\n密钥类型：${keyType ? 'KEY B' : 'KEY A'}\n` +
    `模式：${mode === 'local' ? '本地离线（mfkey32）' : '云端解算（失败自动降级本地）'}\n` +
    `超时：${timeout} 秒（每种方式）\n\n` +
    '将按顺序尝试全部破解方式，前一种失败自动切换下一种。\n' +
    '请保持卡片贴在读卡器上。\n\n确定开始吗？');
  if (!ok) return;
  try {
    state.crack.chain = (state.chain || state.methods.map(m => m.id))
      .map(id => ({ id, state: 'wait', label: (state.methods.find(m => m.id === id) || {}).name || id, cost: 0 }));
    renderChainMini();
    renderMethods();
    resetProgress('crack');
    const res = await api('crack_start', sector, null, timeout, mode);
    startCrackPolling(res.handle);
    toast('已开始破解', 'ok');
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
  }
}

async function doCrackCancel() {
  if (!state.crack.handle) { toast('当前没有正在进行的破解', 'err'); return; }
  try {
    await api('job_cancel', state.crack.handle);
    toast('正在中止…', 'ok');
    log('已请求中止破解', 'warn');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------- 扇区数据 */
function normalizeHex(text) {
  const clean = String(text || '').replace(/[^0-9a-fA-F]/g, '').toUpperCase().slice(0, 32);
  return clean.replace(/(.{2})/g, '$1 ').trim();
}

function renderSectors() {
  const wrap = $('#sectors');
  if (!wrap) return;
  let html = '';
  for (let s = 0; s < 16; s++) {
    const key = state.crackedKeys && state.crackedKeys[s];
    html += `<div class="sector">
      <div class="sector-head"><b>扇区 ${s}</b>` +
      `<span class="key-tag">${key ? escapeHtml(key) : `块 ${s * 4}–${s * 4 + 3}`}</span></div>`;
    for (let b = 0; b < 4; b++) {
      const blk = s * 4 + b;
      const val = state.dump[blk] || '';
      const cls = 'blk' + (b === 3 ? ' trailer' : '') + (val ? '' : ' empty');
      html += `<div class="${cls}" data-block="${blk}">
        <span class="n">${String(blk).padStart(2, '0')}</span>
        <input class="blk-input mono" data-block="${blk}" maxlength="47" spellcheck="false"
               value="${escapeHtml(val)}" placeholder="${b === 3 ? '密钥块' : '00 00 00 …'}">
        <button class="wbtn" data-write="${blk}" title="写入该块">写</button>
      </div>`;
    }
    html += '</div>';
  }
  wrap.innerHTML = html;
  $$('#sectors .blk-input').forEach(inp => {
    inp.addEventListener('input', () => {
      const b = +inp.dataset.block;
      state.dump[b] = inp.value.toUpperCase();
      const row = inp.closest('.blk');
      if (row) row.classList.toggle('empty', !inp.value.trim());
    });
    inp.addEventListener('blur', () => {
      inp.value = normalizeHex(inp.value);
      state.dump[+inp.dataset.block] = inp.value;
    });
  });
  $$('#sectors [data-write]').forEach(btn => {
    btn.onclick = () => writeSingleBlock(+btn.dataset.write);
  });
}

async function writeSingleBlock(block) {
  const raw = ($('#sectors') ? ($(`#sectors .blk-input[data-block="${block}"]`) || {}).value : '') || '';
  const val = String(raw).replace(/\s/g, '');
  if (val.length !== 32) { toast('该块需要 32 位 Hex（16 字节）', 'err'); return; }
  if (block % 4 === 3) {
    const ok = await confirmBox('写入密钥块',
      `块 ${block} 是扇区尾块，写入后会同时改变该扇区的密钥与访问控制位。\n` +
      '若写入的密钥不是你记录的密钥，该扇区可能再也读不出来。\n\n确定写入吗？');
    if (!ok) return;
  }
  const key = ($('#keyInput') ? $('#keyInput').value : '').replace(/\s/g, '');
  const kt = +($('#keyType') ? $('#keyType').value : 0);
  try {
    busy(true, `写入第 ${block} 块…`);
    setLed('busy');
    const res = await api('write_block', block, val, key, kt, true);
    log(`第 ${block} 块写入成功（帧格式 ${res.format}，回读一致，耗时 ${res.elapsed}s）`, 'ok');
    toast(`块 ${block} 写入成功`, 'ok');
  } catch (e) {
    toast('写入失败：' + e.message, 'err');
    log(`写块 ${block} 失败：${e.message}`, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doDump() {
  const key = ($('#keyInput') ? $('#keyInput').value : '').replace(/\s/g, '');
  const kt = +($('#keyType') ? $('#keyType').value : 0);
  try {
    busy(true, '读取整卡…');
    setLed('busy');
    setBadge('#dumpBadge', '读取中', 'info live');
    state.dump = {};
    resetProgress('dump');
    const res = await api('dump_card', true, key, kt, 'dump');
    const blocks = res.blocks || {};
    Object.keys(blocks).forEach(k => {
      if (blocks[k]) state.dump[+k] = normalizeHex(blocks[k]);
    });
    renderSectors();
    const got = Object.keys(state.dump).length;
    const failed = Object.keys(res.errors || {}).length;
    setBadge('#dumpBadge', `成功 ${res.ok_count}/${res.total}`, failed ? 'warn' : 'ok');
    finishProgress('dump', got > 0, `成功 ${res.ok_count} 块 / 失败 ${failed} 块`);
    toast(`读取完成：成功 ${got} 块`, got ? 'ok' : 'err');
    log(`整卡读取完成：成功 ${res.ok_count}/${res.total} 块，失败 ${failed} 块，耗时 ${res.elapsed}s`,
        got ? 'ok' : 'err');
  } catch (e) {
    finishProgress('dump', false, e.message);
    setBadge('#dumpBadge', '读取失败', 'err');
    toast('读取失败：' + e.message, 'err');
    log('整卡读取失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doWriteAll() {
  const blocks = Object.keys(state.dump)
    .map(Number)
    .filter(b => (state.dump[b] || '').replace(/\s/g, '').length === 32)
    .sort((a, b) => a - b);
  if (!blocks.length) { toast('没有可写入的数据，请先读取整卡', 'err'); return; }
  const ok = await confirmBox('写入整卡',
    `即将向目标卡写入 ${blocks.length} 个数据块（含扇区尾块）。\n` +
    '写入会覆盖目标卡原有数据，且每块都会回读校验。\n\n确定继续吗？');
  if (!ok) return;
  const key = ($('#keyInput') ? $('#keyInput').value : '').replace(/\s/g, '');
  const kt = +($('#keyType') ? $('#keyType').value : 0);
  let done = 0, failed = 0;
  try {
    busy(true, '写入整卡…');
    setLed('busy');
    resetProgress('write');
    const t0 = Date.now();
    for (let i = 0; i < blocks.length; i++) {
      const b = blocks[i];
      updateProgress('write', i, blocks.length, `写入第 ${b} 块…`);
      try {
        await api('write_block', b, state.dump[b].replace(/\s/g, ''), key, kt, true);
        done++;
      } catch (err) {
        failed++;
        log(`写块 ${b} 失败：${err.message}`, 'err');
      }
      updateProgress('write', i + 1, blocks.length, `已完成第 ${b} 块`);
    }
    const el = ((Date.now() - t0) / 1000).toFixed(1);
    finishProgress('write', failed === 0, `成功 ${done} 块 / 失败 ${failed} 块 · ${el}s`);
    toast(`写卡完成：成功 ${done}，失败 ${failed}`, failed ? 'err' : 'ok');
    log(`整卡写入完成：成功 ${done} 块，失败 ${failed} 块，耗时 ${el}s`, failed ? 'warn' : 'ok');
  } catch (e) {
    finishProgress('write', false, e.message);
    toast('写卡中断：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

function clearDump() {
  state.dump = {};
  renderSectors();
  setBadge('#dumpBadge', '未读取');
  setText('#dumpPct', '0%');
  setText('#dumpMeta', '等待读取');
  setText('#dumpTime', '—');
  setText('#writePct', '0%');
  setText('#writeMeta', '等待写入');
  setText('#writeTime', '—');
  setBar('#dumpBar', 0, 'run');
  setBar('#writeBar', 0, 'run');
}

/* ---------------------------------------------------------- 卡夹 */
async function refreshBox() {
  try {
    state.cards = (await api('card_list')) || [];
    renderCards();
    setBadge('#boxBadge', `${state.cards.length} 张`, state.cards.length ? 'ok' : '');
  } catch (e) {
    log('读取卡夹失败：' + e.message, 'err');
  }
}

function renderCards() {
  const grid = $('#cardGrid');
  if (!grid) return;
  if (!state.cards.length) {
    grid.innerHTML = '<p class="empty-state">卡夹还是空的。放一张卡到读卡器上，点「一键复制卡」。</p>';
    return;
  }
  grid.innerHTML = '';
  state.cards.forEach(c => {
    const when = c.created_at ? new Date(c.created_at * 1000).toLocaleString('zh-CN') : '';
    const keyCount = Object.keys(c.keys || {}).length;
    const el = document.createElement('div');
    el.className = 'box-card';
    el.innerHTML = `
      <div class="bc-head">
        <b class="bc-name">${escapeHtml(c.name)}</b>
        <span class="badge info">${escapeHtml(c.class_label || c.card_type || '卡')}</span>
      </div>
      <div class="bc-uid">UID ${escapeHtml(c.uid || '—')}</div>
      <div class="bc-meta">
        <span>${c.block_count || 0} 个数据块</span>
        <span>${keyCount ? `密钥 ${keyCount} 扇区` : '无密钥记录'}</span>
        <span>${escapeHtml(when)}</span>
      </div>
      <div class="row">
        <button class="btn primary sm" data-act="paste">粘贴写卡</button>
        <button class="btn ghost sm" data-act="view">查看</button>
        <button class="btn ghost sm" data-act="rename">改名</button>
        <button class="btn ghost sm" data-act="export">导出</button>
        <button class="btn danger sm" data-act="del">删除</button>
      </div>`;
    el.querySelectorAll('[data-act]').forEach(b => {
      b.onclick = () => cardAction(b.dataset.act, c);
    });
    grid.appendChild(el);
  });
}

async function cardAction(act, card) {
  try {
    if (act === 'paste') {
      const ok = await confirmBox('粘贴写卡',
        `将把「${card.name}」的 ${card.block_count} 个数据块写入当前卡片，\n` +
        '包含扇区尾块（密钥块），会覆盖目标卡原有数据，逐块回读校验。\n\n' +
        '请确认目标卡片已放在读卡器上。\n\n确定写卡吗？');
      if (!ok) return;
      busy(true, '写卡中…');
      setLed('busy');
      resetProgress('paste');
      const res = await api('paste_card', card.id, true, '');
      const failed = (res.failed || []).length;
      finishProgress('paste', failed === 0,
        `成功 ${res.written} 块 / 失败 ${failed} 块 · ${res.elapsed}s`);
      (res.failed || []).forEach(f => log(`写块 ${f.block} 失败：${f.error}`, 'err'));
      toast(`写卡完成：成功 ${res.written} 块，失败 ${failed} 块`, failed ? 'err' : 'ok');
      log(`粘贴写卡完成：成功 ${res.written}，失败 ${failed}，跳过 ${res.skipped}，耗时 ${res.elapsed}s`,
          failed ? 'warn' : 'ok');
    } else if (act === 'view') {
      const full = await api('card_get', card.id);
      state.dump = {};
      let has = false;
      Object.keys(full.blocks || {}).forEach(k => {
        if (full.blocks[k]) { state.dump[+k] = normalizeHex(full.blocks[k]); has = true; }
      });
      state.crackedKeys = full.keys || {};
      if (!has) { toast('这张卡没有可用数据', 'err'); return; }
      renderSectors();
      switchView('card');
      toast(`已载入「${card.name}」的 ${Object.keys(state.dump).length} 个块`, 'ok');
    } else if (act === 'rename') {
      const name = window.prompt('新的卡片名称', card.name);
      if (name && name.trim()) {
        await api('card_rename', card.id, name.trim());
        toast('已重命名', 'ok');
        refreshBox();
      }
    } else if (act === 'export') {
      const r = await api('card_export', card.id);
      if (r && r.canceled) return;
      toast('已导出到 ' + r.path, 'ok');
    } else if (act === 'del') {
      const ok = await confirmBox('删除卡片', `确定从卡夹中删除「${card.name}」吗？此操作不可撤销。`);
      if (!ok) return;
      await api('card_remove', card.id);
      toast('已删除', 'ok');
      refreshBox();
    }
  } catch (e) {
    toast(e.message, 'err');
    log('卡夹操作失败：' + e.message, 'err');
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doCopyCard() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  try {
    busy(true, '复制整卡…');
    setLed('busy');
    resetProgress('copy');
    const res = await api('copy_card', '');
    finishProgress('copy', true,
      `共 ${res.blocks} 块 · ${res.elapsed}s`);
    toast(`复制完成，共 ${res.blocks} 个数据块`, 'ok');
    log(`复制卡完成：${res.blocks} 块，UID ${res.card.uid}`, 'ok');
    if (res.crack_job) startCrackPolling(res.crack_job);
    refreshBox();
  } catch (e) {
    finishProgress('copy', false, e.message);
    toast('复制失败：' + e.message, 'err');
    log('复制卡失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doImportCard() {
  try {
    const r = await api('card_import');
    if (r && r.canceled) return;
    toast('已导入：' + r.name, 'ok');
    refreshBox();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------- 外观设置 */
function applyAppearance(s) {
  s = s || {};
  const glass = s.glass == null ? 26 : s.glass;
  const alpha = s.opacity == null ? 4 : s.opacity;
  document.documentElement.style.setProperty('--blur', glass + 'px');
  document.documentElement.style.setProperty('--alpha', alpha / 100);
  if ($('#rngBlur')) { $('#rngBlur').value = glass; setText('#lblBlur', glass); }
  if ($('#rngAlpha')) { $('#rngAlpha').value = alpha; setText('#lblAlpha', alpha); }
  document.body.classList.toggle('light', s.theme === 'light');

  const bg = $('#bg');
  if (!bg) return;
  if (s.wallpaper) {
    bg.style.backgroundImage = `url("wallpapers/${encodeURIComponent(s.wallpaper)}")`;
    bg.classList.add('has-image');
  } else {
    const preset = PRESETS.find(p => p.id === (s.preset || 'aurora')) || PRESETS[0];
    bg.style.backgroundImage = preset.css;
    bg.classList.remove('has-image');
  }
}

function renderWalls(wallpapers) {
  const grid = $('#wallGrid');
  if (!grid) return;
  wallpapers = Array.isArray(wallpapers) ? wallpapers : [];
  const st = state.settings || {};
  const cur = st.wallpaper || '';
  const curPreset = st.preset || 'aurora';
  grid.innerHTML = '';

  PRESETS.forEach(p => {
    const d = document.createElement('div');
    d.className = 'wall-item' + (!cur && curPreset === p.id ? ' sel' : '');
    d.dataset.preset = p.id;
    d.style.backgroundImage = p.css;
    d.innerHTML = `<span class="wall-name">${p.name}</span>`;
    d.onclick = async () => {
      try {
        state.settings = await api('save_settings', { wallpaper: '', preset: p.id });
        applyAppearance(state.settings);
        renderWalls(wallpapers);
      } catch (e) { toast(e.message, 'err'); }
    };
    grid.appendChild(d);
  });

  wallpapers.forEach(w => {
    const d = document.createElement('div');
    d.className = 'wall-item' + (cur === w.name ? ' sel' : '');
    d.dataset.file = w.name;
    d.style.backgroundImage = `url("${encodeURI(w.url)}")`;
    d.innerHTML = `<span class="wall-name">自定义</span>`;
    d.onclick = async () => {
      try {
        state.settings = await api('save_settings', { wallpaper: w.name });
        applyAppearance(state.settings);
        renderWalls(wallpapers);
      } catch (e) { toast(e.message, 'err'); }
    };
    grid.appendChild(d);
  });
}

async function refreshWalls() {
  let walls = [];
  try { walls = (await api('list_wallpapers')) || []; }
  catch (e) { log('壁纸列表读取失败，已回退内置背景：' + e.message, 'err'); }
  renderWalls(walls);
}

/* ---------------------------------------------------------- P0 · 独立写卡 / 读 ID 卡 */
let writeModes = [];

function renderWriteModes(modes) {
  writeModes = modes || [];
  const sel = $('#wMode');
  if (!sel) return;
  if (!writeModes.length) {
    sel.innerHTML = '<option value="normal">普通写卡（0x18）</option>';
    return;
  }
  sel.innerHTML = writeModes.map(m =>
    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}（${escapeHtml(m.cmd)}）</option>`
  ).join('');
  const i = writeModes.findIndex(m => m.id === 'normal');
  sel.value = writeModes[i >= 0 ? i : 0].id;
}

function writeNoInput() {
  const raw = (($('#wCardNo') || {}).value || '').replace(/\s/g, '').toUpperCase();
  if (!/^[0-9A-F]{1,10}$/.test(raw)) {
    toast('卡号需为 10 位 Hex（不足 10 位按低位对齐）', 'err');
    return '';
  }
  return raw.padStart(10, '0');
}

function renderWriteResult(res) {
  const box = $('#writeCardResult');
  if (!box || !res) return;
  const rows = [];
  if (res.checked) {
    const c = res.checked;
    rows.push(`<div>卡号校验：<span class="${c.ok ? 'g' : 'r'}">${c.ok ? '通过' : '未通过'}</span>` +
      ` · 待写 <span class="k">${escapeHtml(c.target)}</span>` +
      ` · 当前 <span class="k">${escapeHtml(c.current)}</span>` +
      `（${escapeHtml(c.source || '')}）</div>`);
  }
  (res.steps || []).forEach(s => {
    if (s.step === 'check') return;
    const d = s.detail || {};
    const label = s.step === 'card_no' ? '写卡号 0x0C'
      : `${escapeHtml(d.mode_label || '')} 0x${(d.cmd || '').replace('0x', '')} 第 ${s.block} 块`;
    const vtxt = d.verify_text || '未校验';
    const cls = d.verified === false ? 'r' : (d.verified ? 'g' : '');
    rows.push(`<div>${label}：<span class="${cls}">${escapeHtml(vtxt)}</span>` +
      ` · ${d.elapsed != null ? d.elapsed + 's' : ''}</div>`);
  });
  if (!rows.length) { box.innerHTML = '<p class="hint">未产生写入步骤。</p>'; return; }
  box.innerHTML = '<h4>写卡明细</h4>' + rows.join('') +
    `<div class="hint">卡类型：${escapeHtml(res.class_label || '—')}` +
    `${res.uid ? ' · UID ' + escapeHtml(res.uid) : ''}` +
    `${res.card_no ? ' · 卡号 ' + escapeHtml(res.card_no) : ''}</div>`;
}

async function doReadCurrentNo() {
  try {
    busy(true, '读取当前卡号…');
    setLed('busy');
    const info = await api('read_id_card');
    fillIdCard(info);
    const val = (info.id_hex || '').slice(-10);
    if ($('#wCardNo')) $('#wCardNo').value = val;
    toast(`当前卡号 ${val} · 频率 ${info.freq}`, 'ok');
    log(`0x26 读 ID 卡：卡号 ${info.id_hex} · 频率 ${info.freq} · ${info.card_type}`, 'ok');
  } catch (e) {
    toast('读取卡号失败：' + e.message, 'err');
    log('0x26 读 ID 卡失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

function fillIdCard(info) {
  setText('#idNo', info.id_hex || '—');
  setText('#idDec', info.id_dec || '—');
  setText('#idFreq', info.freq || '—');
  setText('#idFreqId', info.freq_id != null ? String(info.freq_id) : '—');
  setText('#idType', info.card_type || '—');
  setText('#idCode', String(info.card_code != null ? info.card_code : '—'));
  setText('#idFacility', String(info.facility_code != null ? info.facility_code : '—'));
  setText('#idRaw', info.raw || '—');
  setBadge('#idCardBadge', info.freq || '已读取', 'ok');
}

async function doReadIdCard() {
  try {
    busy(true, '读取 ID 卡…');
    setLed('busy');
    setBadge('#idCardBadge', '读取中', 'info live');
    const info = await api('read_id_card');
    fillIdCard(info);
    toast(`ID 卡 ${info.id_hex} · 频率 ${info.freq}`, 'ok');
    log(`读 ID 卡成功：卡号 ${info.id_hex} · 频率 ${info.freq}（档位 ${info.freq_id}）· ` +
        `${info.card_type} · 卡代码 ${info.card_code} · 设施码 ${info.facility_code}`, 'ok');
  } catch (e) {
    setBadge('#idCardBadge', '读取失败', 'err');
    toast('读 ID 卡失败：' + e.message, 'err');
    log('0x26 读 ID 卡失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doWriteCardNo(onlyNo) {
  const no = writeNoInput();
  if (!no) return;
  const mode = ($('#wMode') || {}).value || 'normal';
  const kt = +($('#keyType') ? $('#keyType').value : 0);
  const key = ($('#keyInput') ? $('#keyInput').value : '').replace(/\s/g, '');
  const check = !!($('#wCheckNo') || {}).checked;
  const verify = !!($('#wVerify') || {}).checked;
  const head = onlyNo ? '仅写卡号（0x0C）' : '开始写卡';
  const ok = await confirmBox(head,
    `待写卡号：${no}\n写卡档位：${mode}\n` +
    (check ? '写前会回读当前卡号做校验，不一致则中止。\n' : '已关闭写前校验，将直接写入。\n') +
    '写入会覆盖卡上原有卡号数据。\n\n确定继续吗？');
  if (!ok) return;
  try {
    busy(true, head + '…');
    setLed('busy');
    setBadge('#writeCardBadge', '写入中', 'info live');
    resetProgress('writecard');
    const res = onlyNo
      ? await api('write_card_no', no, verify)
      : await api('write_card', no, mode, state.dump || {}, verify, check, key, kt, 'writecard');
    renderWriteResult(res.steps ? res : { steps: [{ step: 'card_no', detail: res }] });
    const good = res.ok !== false && res.verified !== false;
    setBadge('#writeCardBadge', good ? '写入完成' : '写入有异常', good ? 'ok' : 'warn');
    finishProgress('writecard', good, res.verify_text || (good ? '写入完成' : '存在未通过项'));
    toast(good ? '写卡完成' : '写卡完成但校验未通过', good ? 'ok' : 'err');
    log(`${head} 结束：${res.verify_text || ''}（耗时 ${res.elapsed != null ? res.elapsed : '—'}s）`,
        good ? 'ok' : 'warn');
  } catch (e) {
    finishProgress('writecard', false, e.message);
    setBadge('#writeCardBadge', '写入失败', 'err');
    toast('写卡失败：' + e.message, 'err');
    log('写卡失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

/* ---------------------------------------------------------- P1 · 手机 / 手环 NFC 写录
   命令语义对齐小程序 card-write 页：0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡
   → 0x16/0x18 写 IC 数据。与「写 UID / 魔术卡」（0x27 改写卡片 UID）、
   「封卡」（0x17，不可逆）是不同流程。 */
let phoneNfcInfo = null;

function renderPhoneModes(info) {
  phoneNfcInfo = info || null;
  const modes = (info && info.modes) || [];
  const sel = $('#pnMode');
  if (!sel) return;
  if (!modes.length) {
    sel.innerHTML = '<option value="fast">极速写卡（0x16）</option>';
    return;
  }
  sel.innerHTML = modes.map(m =>
    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)}（${escapeHtml(m.cmd)}）</option>`
  ).join('');
  sel.value = modes.some(m => m.id === 'fast') ? 'fast' : modes[0].id;
}

/* 读取定长 Hex 输入框；空串返回 ''（表示交由设备自取），非法返回 null */
function hexFieldValue(sel, size, name) {
  const raw = (($(sel) || {}).value || '').replace(/0x/gi, '').replace(/\s/g, '').toUpperCase();
  if (!raw) return '';
  if (!new RegExp(`^[0-9A-F]{1,${size * 2}}$`).test(raw)) {
    toast(`${name}需为 ${size * 2} 位 Hex（不足按低位对齐）`, 'err');
    return null;
  }
  return raw.padStart(size * 2, '0');
}

function renderPhoneNfcResult(res, note) {
  const box = $('#phoneNfcResult');
  if (!box || !res) return;
  const rows = [];
  const ck = res.checked || null;
  if (ck) {
    const rc = ck.read_back || {};
    const cc = ck.cmd || {};
    rows.push(`<div>写前校验：<span class="${rc.ok ? 'g' : 'r'}">${rc.ok ? '通过' : '未通过'}</span>` +
      ` · 0x24 设备比对 ${cc.ok ? '通过' : '未通过'}（status ${cc.status != null ? cc.status : '—'}）` +
      ` · 目标 <span class="k">${escapeHtml(rc.target || '—')}</span>` +
      ` / 当前 <span class="k">${escapeHtml(rc.current || '—')}</span></div>`);
  }
  const fixed = { card_no: '写卡号 0x0C', bridge: '生成转接卡 0x27（8 字节写 UID）' };
  (res.steps || []).forEach(s => {
    if (s.step === 'check') return;
    const d = s.detail || {};
    const label = s.step === 'block'
      ? `${escapeHtml(d.mode_label || res.mode_label || '')} 0x${String(d.cmd || '').replace('0x', '')} 第 ${s.block} 块`
      : (fixed[s.step] || s.step);
    const bad = d.ok === false || d.verified === false;
    const vtxt = d.verify_text || d.status_text || (d.ok === false ? '失败' : '未校验');
    rows.push(`<div>${label}：<span class="${bad ? 'r' : (d.verified ? 'g' : '')}">` +
      `${escapeHtml(vtxt)}</span>${d.elapsed != null ? ' · ' + d.elapsed + 's' : ''}</div>`);
  });
  if (!rows.length) { box.innerHTML = '<p class="hint">未产生写入步骤。</p>'; return; }
  box.innerHTML = '<h4>手机 / 手环 NFC 写录明细</h4>' + rows.join('') +
    `<div class="hint">卡类型：${escapeHtml(res.class_label || '—')}` +
    `${res.uid ? ' · 转接卡 UID ' + escapeHtml(res.uid) : ''}` +
    `${res.card_no ? ' · 卡号 ' + escapeHtml(res.card_no) : ''}` +
    `${res.prep_only ? ' · 仅生成转接卡（未写 IC 数据）' : ''}` +
    `${res.warning ? ' · ' + escapeHtml(res.warning) : ''}` +
    `${note ? ' · ' + escapeHtml(note) : ''}</div>`;
}

function phoneNfcStepsText() {
  const steps = (phoneNfcInfo && phoneNfcInfo.steps) || [];
  if (!steps.length) {
    return '0x11 寻卡 → 0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡 → 0x16/0x18 写 IC 数据 → 0x07 回读校验';
  }
  return steps.map((s, i) => `${i + 1}. ${s.label}：${s.desc || ''}`).join('\n');
}

async function doWritePhoneNfc() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const uid = hexFieldValue('#pnUid', 6, '转接卡 UID');
  if (uid === null) return;
  const cardNo = hexFieldValue('#pnCardNo', 5, '卡号');
  if (cardNo === null) return;
  const mode = ($('#pnMode') || {}).value || 'fast';
  const prepOnly = !!($('#pnPrepOnly') || {}).checked;
  const checkNo = !!($('#pnCheckNo') || {}).checked;
  const verify = !!($('#pnVerify') || {}).checked;
  const ok = await confirmBox('写入手机 / 手环 NFC',
    '流程：转接卡两步法（0x0C 写卡号 → 0x26 回读 → 0x27 生成转接卡' +
    (prepOnly ? '）' : ' → 0x16/0x18 写 IC 数据）') + '\n' +
    `写块档位：${mode}\n` +
    `转接卡 UID：${uid || '（设备自取）'}\n` +
    `模拟卡号：${cardNo || '（不写卡号）'}\n` +
    (checkNo ? '写前会用 0x24 + 0x26 校验卡号。\n' : '已关闭写前校验。\n') +
    '注意：本流程≠写 UID / 魔术卡（0x27 改写卡片 UID），也≠封卡（0x17，不可逆）。\n\n确定继续吗？');
  if (!ok) return;

  const blocks = {};
  Object.keys(state.dump || {}).forEach(k => {
    const v = String(state.dump[k] || '').replace(/\s/g, '');
    if (v.length === 32) blocks[k] = v.toUpperCase();
  });
  try {
    busy(true, '写录手机 / 手环 NFC…');
    setLed('busy');
    setBadge('#phoneNfcBadge', '写录中', 'info live');
    resetProgress('phonenfc');
    const res = await api('write_phone_nfc', uid, cardNo, prepOnly ? {} : blocks,
                          mode, verify, checkNo, '', -1, prepOnly, 'phonenfc');
    renderPhoneNfcResult(res);
    const good = res.ok !== false && res.verified !== false;
    setBadge('#phoneNfcBadge', good ? '写录完成' : '写录有异常', good ? 'ok' : 'warn');
    finishProgress('phonenfc', good, res.verify_text || (good ? '写录完成' : '存在未通过项'));
    toast(good ? '手机 / 手环 NFC 写录完成' : '写录完成但校验未通过', good ? 'ok' : 'err');
    log(`写录手机 / 手环 NFC（${res.mode_label || mode}${prepOnly ? '·仅转接卡' : ''}）：` +
        `${res.steps ? res.steps.length : 0} 步 · ${res.verify_text || '未校验'}`, good ? 'ok' : 'warn');
  } catch (e) {
    finishProgress('phonenfc', false, e.message);
    setBadge('#phoneNfcBadge', '写录失败', 'err');
    toast('写录失败：' + e.message, 'err');
    log('写录手机 / 手环 NFC 失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doWriteUidCard() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const uid = hexFieldValue('#pnUid', 6, '转接卡 UID');
  if (uid === null) return;
  const ok = await confirmBox('写 UID / 生成转接卡（0x27）',
    'DATA = 0x27 00 + 6 字节 UID（大端），共 8 字节。\n' +
    (uid ? `待写 UID：${uid}` : '未填 UID：将退化为单字节 0x27，由设备自取当前卡 UID。') + '\n' +
    '这会直接改写卡片 UID（魔术卡 / 转接卡专用），与「手机 / 手环写录」的完整流程不同。\n\n确定继续吗？');
  if (!ok) return;
  try {
    busy(true, '写 UID / 生成转接卡…');
    setLed('busy');
    const res = await api('write_uid_card', uid, true);
    toast(res.ok ? `0x27 完成：${res.verify_text || '未校验'}` : `0x27 失败：${res.status_text || ''}`,
          res.ok ? 'ok' : 'err');
    log(`0x27 写 UID / 生成转接卡：${res.payload || ''} → ${res.ok ? '成功' : '失败 ' + (res.status_text || '')}`,
        res.ok ? 'ok' : 'err');
  } catch (e) {
    toast('写 UID 失败：' + e.message, 'err');
    log('0x27 写 UID 失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doLock(cmd) {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  if (!($('#lockConfirm') || {}).checked) {
    toast('封卡 / 锁卡不可逆，请先勾选「我已知晓」', 'err');
    return;
  }
  const isUfuid = cmd === 'ufuid';
  const head = isUfuid ? '封卡 UFUID（0x17）' : '锁卡（0x23）';
  const ok = await confirmBox(head,
    `${head} 为单字节指令，执行后该卡不能再被改写（不可逆）。\n` +
    '封卡用于 UFUID / 类魔术卡；锁卡用于普通卡片锁定。\n\n确定继续吗？');
  if (!ok) return;
  try {
    busy(true, head + '…');
    setLed('busy');
    const res = await api(isUfuid ? 'lock_ufuid' : 'lock_card', true);
    toast(res.ok ? `${head} 完成（不可逆）` : `${head} 失败：${res.err || ''}`, res.ok ? 'ok' : 'err');
    log(`${head}：${res.ok ? '完成（不可逆）' : '失败 ' + (res.err || '')}` +
        `${res.verify_text ? ' · ' + res.verify_text : ''}`, res.ok ? 'warn' : 'err');
  } catch (e) {
    toast(`${head} 失败：` + e.message, 'err');
    log(`${head} 失败：` + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

/* ---------------------------------------------------------- P1 · 自动识别 / 一键破解扫描 */
function fillScan(info) {
  const card = (info && info.card) || {};
  const idc = (info && info.id_card) || {};
  setText('#scanSource', info.source || '—');
  setText('#scanNo', idc.id_hex || card.card_no || card.uid || '—');
  setText('#scanType', idc.card_type || card.class_label || card.card_type || '—');
  setText('#scanCode', idc.card_code != null ? String(idc.card_code) : '—');
  setText('#scanFacility', idc.facility_code != null ? String(idc.facility_code) : '—');
  setText('#scanAdvice', info.crack_reason || '—');
  setBadge('#scanBadge', info.identified ? '已识别' : '未识别', info.identified ? 'ok' : 'warn');
}

function renderScanResult(info, extra) {
  const box = $('#scanResult');
  if (!box || !info) return;
  const card = info.card || {};
  const idc = info.id_card || {};
  const rows = [];
  rows.push(`<div>识别来源：<span class="k">${escapeHtml(info.source || '—')}</span>` +
    `${info.identify_error ? ' · 0x38 未命中：' + escapeHtml(info.identify_error) : ''}</div>`);
  if (idc.id_hex) {
    rows.push(`<div>0x38 卡号：<span class="k">${escapeHtml(idc.id_hex)}</span> · ` +
      `${escapeHtml(idc.card_type || '')} · 频率 ${escapeHtml(idc.freq || '—')} · ` +
      `卡代码 ${escapeHtml(String(idc.card_code != null ? idc.card_code : '—'))}</div>`);
  }
  if (card.uid) {
    rows.push(`<div>0x11 寻卡：UID <span class="k">${escapeHtml(card.uid)}</span> · ` +
      `${escapeHtml(card.class_label || card.card_type || '—')}</div>`);
  }
  rows.push(`<div>破解建议：<span class="${info.crack_recommended ? 'g' : ''}">` +
    `${escapeHtml(info.crack_reason || '—')}</span></div>`);
  if (extra) rows.push(`<div>${escapeHtml(extra)}</div>`);
  box.innerHTML = '<h4>自动识别 / 扫描结论</h4>' + rows.join('');
}

function prepareCrackChain() {
  state.crack.chain = (state.chain || state.methods.map(m => m.id))
    .map(id => ({ id, state: 'wait',
                  label: (state.methods.find(m => m.id === id) || {}).name || id, cost: 0 }));
  renderChainMini();
  renderMethods();
}

async function doAutoIdentify() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  try {
    busy(true, '自动识别卡片…');
    setLed('busy');
    setBadge('#scanBadge', '识别中', 'info live');
    const info = await api('auto_identify');
    const wrap = { identified: true, source: '0x38 自动识别', id_card: info,
                   card: {}, crackable: false,
                   crack_reason: 'ID 卡无密钥概念，无需破解（如需 IC 卡破解请用「一键破解扫描」）' };
    fillScan(wrap);
    renderScanResult(wrap);
    toast(`自动识别：卡号 ${info.id_hex} · ${info.card_type}`, 'ok');
    log(`0x38 自动识别：卡号 ${info.id_hex} · ${info.card_type} · 卡代码 ${info.card_code} · ` +
        `设施码 ${info.facility_code} · 频率 ${info.freq}`, 'ok');
  } catch (e) {
    setBadge('#scanBadge', '识别失败', 'err');
    toast('自动识别失败：' + e.message, 'err');
    log('0x38 自动识别失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doCardNoValid() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const no = writeNoInput();
  if (!no) return;
  try {
    busy(true, '卡号校验（0x24）…');
    setLed('busy');
    const res = await api('card_no_valid', no);
    toast(res.ok ? `0x24 校验通过（当前卡号与 ${no} 一致）`
                 : `0x24 校验未通过：${res.err || '状态 ' + res.status}`,
          res.ok ? 'ok' : 'err');
    log(`0x24 卡号校验：${res.ok ? '通过' : '未通过'}（status ${res.status}）· 原始帧 ${res.raw || ''}`,
        res.ok ? 'ok' : 'warn');
    setText('#scanSource', '0x24 卡号校验');
    setText('#scanNo', no);
    setText('#scanAdvice', res.ok ? '设备侧校验通过' : (res.err || '校验未通过'));
    setBadge('#scanBadge', res.ok ? '校验通过' : '校验未通过', res.ok ? 'ok' : 'warn');
  } catch (e) {
    toast('卡号校验失败：' + e.message, 'err');
    log('0x24 卡号校验失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

async function doCrackScan() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const sector = +(state.settings.crack_sector || 0);
  const keyType = +(state.settings.default_key_type || 0);
  const auto = !!($('#scanAutoCrack') || {}).checked;
  try {
    busy(true, '自动识别 / 破解扫描…');
    setLed('busy');
    setBadge('#scanBadge', '扫描中', 'info live');
    resetProgress('scan');
    const info = await api('crack_scan', auto, sector, keyType, '', 'scan');
    fillScan(info);
    let note = '';
    if (info.crack_job) {
      prepareCrackChain();
      startCrackPolling(info.crack_job, info.card);
      note = `已按建议启动后台级联破解（任务 ${info.crack_job}），进度见「破解」页。`;
    } else if (auto && info.crackable && !info.crack_job) {
      note = '建议破解但后台任务未启动，请到「破解」页手动执行。';
    }
    renderScanResult(info, note);
    const good = info.ok !== false;
    setBadge('#scanBadge', info.identified ? '扫描完成' : '未识别到卡片', good ? 'ok' : 'warn');
    finishProgress('scan', good, info.crack_reason || '扫描完成');
    toast(info.identified ? `识别到 ${info.source}：${info.crack_reason}` : '未识别到卡片，请贴卡重试',
          good ? 'ok' : 'err');
    log(`一键破解扫描：${info.source} → ${info.crack_reason}` +
        `${info.crack_job ? '（后台任务 ' + info.crack_job + '）' : ''}`, good ? 'ok' : 'warn');
  } catch (e) {
    finishProgress('scan', false, e.message);
    setBadge('#scanBadge', '扫描失败', 'err');
    toast('扫描失败：' + e.message, 'err');
    log('一键破解扫描失败：' + e.message, 'err');
  } finally {
    busy(false);
    setLed(state.connected ? 'on' : '');
  }
}

/* ---------------------------------------------------------- 事件绑定 */
function bindUI() {
  $$('.nav-item').forEach(b => b.onclick = () => switchView(b.dataset.view));

  /* 设备页 · 3D */
  if ($('#btnSpinToggle')) {
    $('#btnSpinToggle').onclick = () => {
      state.spin.on = !state.spin.on;
      $('#btnSpinToggle').textContent = state.spin.on ? '暂停旋转' : '继续旋转';
    };
  }
  if ($('#btnSpinReset')) {
    $('#btnSpinReset').onclick = () => {
      state.spin.deg = 0;
      $('#deviceModel') && ($('#deviceModel').style.transform = 'rotateX(-7deg) rotateY(0deg)');
      toast('已复位', 'ok');
    };
  }
  if ($('#rngSpin')) {
    $('#rngSpin').value = state.spin.speed;
    setText('#lblSpin', state.spin.speed);
    $('#rngSpin').oninput = e => {
      state.spin.speed = +e.target.value;
      setText('#lblSpin', state.spin.speed);
    };
    $('#rngSpin').onchange = async e => {
      try { state.settings = await api('save_settings', { device_spin: +e.target.value }); }
      catch (err) { /* 忽略 */ }
    };
  }

  /* 连接方式切换 */
  if ($('#tabBle')) $('#tabBle').onclick = () => switchConnTab('ble');
  if ($('#tabUsb')) $('#tabUsb').onclick = () => switchConnTab('usb');

  /* 蓝牙 */
  if ($('#btnScan')) $('#btnScan').onclick = doScan;
  if ($('#btnQuickConnect')) $('#btnQuickConnect').onclick = doQuickConnect;

  /* USB */
  if ($('#btnScanPorts')) $('#btnScanPorts').onclick = doScanPorts;
  if ($('#btnProbePort')) $('#btnProbePort').onclick = () => doProbePort('');

  /* USB 直连（厂商通道） */
  if ($('#btnScanWired')) $('#btnScanWired').onclick = doScanWired;
  if ($('#btnProbeWired')) $('#btnProbeWired').onclick = () => doProbeWired(null);
  if ($('#btnWiredDisconnect')) $('#btnWiredDisconnect').onclick = doWiredDisconnect;
  if ($('#btnSendWired')) $('#btnSendWired').onclick = doSendWiredFrame;
  if ($('#btnClearWiredLog')) $('#btnClearWiredLog').onclick = clearWiredLog;
  if ($('#wiredFrame')) {
    $('#wiredFrame').onkeydown = e => { if (e.key === 'Enter') doSendWiredFrame(); };
  }

  /* 设备信息 */
  if ($('#btnRefreshInfo')) $('#btnRefreshInfo').onclick = doRefreshInfo;
  if ($('#btnDisconnect')) $('#btnDisconnect').onclick = doDisconnect;

  /* 寻卡读写 */
  if ($('#btnFind')) $('#btnFind').onclick = () => doFind(false);
  if ($('#btnCopyUid')) {
    $('#btnCopyUid').onclick = async () => {
      const uid = $('#fUid') ? $('#fUid').textContent : '';
      if (!uid || uid === '—') { toast('请先寻卡', 'err'); return; }
      try { await api('clipboard_write', uid); toast('UID 已复制', 'ok'); }
      catch (e) { toast(e.message, 'err'); }
    };
  }
  if ($('#btnBug')) {
    $('#btnBug').onclick = async () => {
      try {
        busy(true, '漏洞卡检测中…');
        const r = await api('detect_bug_card');
        toast(r.is_bug ? '检测到漏洞卡（可改 UID）' : '未检测到漏洞卡', r.is_bug ? 'ok' : '');
        log('漏洞卡检测：' + (r.raw || ''), 'info');
      } catch (e) { toast(e.message, 'err'); }
      finally { busy(false); }
    };
  }
  if ($('#btnDump')) $('#btnDump').onclick = doDump;
  if ($('#btnWriteAll')) $('#btnWriteAll').onclick = doWriteAll;
  if ($('#btnClearDump')) $('#btnClearDump').onclick = clearDump;
  if ($('#btnReadNo')) $('#btnReadNo').onclick = doReadCurrentNo;
  if ($('#btnWriteNo')) $('#btnWriteNo').onclick = () => doWriteCardNo(true);
  if ($('#btnWriteCard')) $('#btnWriteCard').onclick = () => doWriteCardNo(false);
  if ($('#btnReadIdCard')) $('#btnReadIdCard').onclick = doReadIdCard;
  if ($('#btnCrackAgain')) $('#btnCrackAgain').onclick = doCrackAgain;

  /* 写录对应：手机 / 手环 NFC（0x27 转接卡） */
  if ($('#btnWritePhoneNfc')) $('#btnWritePhoneNfc').onclick = doWritePhoneNfc;
  if ($('#btnWriteUidCard')) $('#btnWriteUidCard').onclick = doWriteUidCard;
  if ($('#btnPhoneNfcSteps')) {
    $('#btnPhoneNfcSteps').onclick = () => {
      const box = $('#phoneNfcResult');
      if (box) box.innerHTML = '<h4>手机 / 手环 NFC 写录步骤</h4><pre class="steps">' +
        escapeHtml(phoneNfcStepsText()) + '</pre>';
    };
  }
  if ($('#btnLockUfuid')) $('#btnLockUfuid').onclick = () => doLock('ufuid');
  if ($('#btnLockCard')) $('#btnLockCard').onclick = () => doLock('card');

  /* 破解页 */
  if ($('#btnCrack')) $('#btnCrack').onclick = doManualCrack;
  if ($('#btnCrackCancel')) $('#btnCrackCancel').onclick = doCrackCancel;

  /* 破解页：自动识别卡片 / 一键破解扫描 */
  if ($('#btnAutoIdentify')) $('#btnAutoIdentify').onclick = doAutoIdentify;
  if ($('#btnCrackScan')) $('#btnCrackScan').onclick = doCrackScan;
  if ($('#btnCardNoValid')) $('#btnCardNoValid').onclick = doCardNoValid;

  /* 卡夹 */
  if ($('#btnCopyCard')) $('#btnCopyCard').onclick = doCopyCard;
  if ($('#btnImportCard')) $('#btnImportCard').onclick = doImportCard;
  if ($('#btnRefreshBox')) $('#btnRefreshBox').onclick = refreshBox;

  /* 破解模式（云端解算 / 本地离线） */
  if ($('#modeCloud')) $('#modeCloud').onclick = () => setCrackMode('cloud', true);
  if ($('#modeLocal')) $('#modeLocal').onclick = () => setCrackMode('local', true);
  if ($('#btnVerifyLocal')) $('#btnVerifyLocal').onclick = doVerifyLocal;
  if ($('#btnWxToken')) $('#btnWxToken').onclick = doWxToken;
  /* 密码库 */
  if ($('#kbTabPublic')) $('#kbTabPublic').onclick = () => setKeyboxTab('public');
  if ($('#kbTabUser')) $('#kbTabUser').onclick = () => setKeyboxTab('user');
  if ($('#kbTabHistory')) $('#kbTabHistory').onclick = () => setKeyboxTab('history');
  if ($('#keyboxAdd')) $('#keyboxAdd').onclick = doKeyboxAdd;
  if ($('#keyboxClear')) $('#keyboxClear').onclick = doKeyboxClear;
  if ($('#keyboxVerify')) $('#keyboxVerify').onclick = doKeyboxVerify;
  if ($('#btnFormatCard')) $('#btnFormatCard').onclick = doFormatCard;

  /* 设置 */
  if ($('#btnPickWall')) {
    $('#btnPickWall').onclick = async () => {
      try {
        const r = await api('pick_wallpaper');
        if (r && r.canceled) return;
        state.settings = await api('get_settings');
        applyAppearance(state.settings);
        await refreshWalls();
        toast('背景已更换', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  }
  if ($('#btnClearWall')) {
    $('#btnClearWall').onclick = async () => {
      try {
        state.settings = await api('clear_wallpaper');
        applyAppearance(state.settings);
        await refreshWalls();
        toast('已恢复内置背景', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  }
  if ($('#rngBlur')) {
    $('#rngBlur').oninput = e => {
      setText('#lblBlur', e.target.value);
      document.documentElement.style.setProperty('--blur', e.target.value + 'px');
    };
    $('#rngBlur').onchange = async e => {
      try { state.settings = await api('save_settings', { glass: +e.target.value }); }
      catch (err) { /* 忽略 */ }
    };
  }
  if ($('#rngAlpha')) {
    $('#rngAlpha').oninput = e => {
      setText('#lblAlpha', e.target.value);
      document.documentElement.style.setProperty('--alpha', e.target.value / 100);
    };
    $('#rngAlpha').onchange = async e => {
      try { state.settings = await api('save_settings', { opacity: +e.target.value }); }
      catch (err) { /* 忽略 */ }
    };
  }
  if ($('#btnSaveSet')) {
    $('#btnSaveSet').onclick = async () => {
      try {
        const patch = {
          default_key: normalizeHex($('#setKey').value).replace(/\s/g, '') || 'FFFFFFFFFFFF',
          default_key_type: +$('#setKeyType').value,
          crack_sector: +$('#setCrackSector').value,
          crack_timeout: +$('#setCrackTimeout').value || 25,
          auto_crack: !!$('#setAutoCrack').checked,
          auto_find: !!$('#setAutoFind').checked,
          hid_enable: !!($('#setHidEnable') || {}).checked,
          non_full_key_read: !!($('#setNonFullKey') || {}).checked,
          encrypted_zero_sectors: !!($('#setEncZero') || {}).checked,
          write_card_tips: !!($('#setWriteTips') || {}).checked,
          power_display: !!($('#setPowerDisplay') || {}).checked,
          sak88: !!($('#setSak88') || {}).checked,
          device_spin: state.spin.speed,
          crack_mode: state.crack_mode === 'local' ? 'local' : 'cloud',
          cloud_endpoint: $('#setCloudEndpoint') ? $('#setCloudEndpoint').value.trim() : '',
          cloud_timeout: +($('#setCloudTimeout') ? $('#setCloudTimeout').value : 8) || 8,
        };
        const tok = $('#setCloudToken') ? $('#setCloudToken').value.trim() : '';
        if (tok) patch.cloud_token = tok;   /* 留空 = 不改动已保存的 token */
        state.settings = await api('save_settings', patch);
        try { await refreshBattery(); } catch (e) {}
        if ($('#setCloudToken')) $('#setCloudToken').value = '';
        if ($('#keyInput')) $('#keyInput').value = state.settings.default_key;
        if ($('#keyType')) $('#keyType').value = state.settings.default_key_type;
        toast('设置已保存', 'ok');
        log('设置已保存', 'ok');
        await refreshCrackEngine();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  /* 日志 / 窗口 */
  if ($('#btnLog')) $('#btnLog').onclick = () => $('#logDrawer').classList.toggle('open');
  if ($('#btnCloseLog')) $('#btnCloseLog').onclick = () => $('#logDrawer').classList.remove('open');
  if ($('#btnClearLog')) $('#btnClearLog').onclick = () => { if ($('#logBody')) $('#logBody').innerHTML = ''; };
  if ($('#wClose')) $('#wClose').onclick = () => api('win_close');
  if ($('#wMin')) $('#wMin').onclick = () => api('win_minimize');
  if ($('#wMax')) $('#wMax').onclick = () => api('win_toggle_max');
}

/* ---------------------------------------------------------- 破解双模式 */
function renderCrackMode(mode) {
  const local = mode === 'local';
  state.crack_mode = local ? 'local' : 'cloud';
  if ($('#modeCloud')) $('#modeCloud').classList.toggle('is-active', !local);
  if ($('#modeLocal')) $('#modeLocal').classList.toggle('is-active', local);
  const hint = $('#crackModeHint');
  if (hint) {
    const eng = state.engine || {};
    const cur = (eng.modes || []).find(m => m.id === state.crack_mode) || {};
    hint.textContent = cur.desc || (local
      ? '不联网，直接用本机 mfkey32 恢复密钥。'
      : '云端解算失败或超时后自动降级本地 mfkey32。');
  }
}

async function setCrackMode(mode, persist) {
  renderCrackMode(mode);
  if (!persist) return;
  const label = state.crack_mode === 'local' ? '本地离线' : '云端解算';
  try {
    state.settings = await api('save_settings', { crack_mode: state.crack_mode });
    toast('破解模式：' + label, 'ok');
    log('破解模式已切换为 ' + label, 'info');
    await refreshCrackEngine();
  } catch (e) { toast(e.message, 'err'); }
}

async function refreshCrackEngine() {
  let eng = null;
  try { eng = await api('crack_engine_status'); }
  catch (e) {
    state.engine = null;
    setBadge('#crackModeBadge', '引擎状态不可用', 'warn');
    return;
  }
  state.engine = eng || {};
  const e = state.engine;
  const local = e.local || {};
  const cloud = e.cloud || {};
  renderCrackMode(e.mode === 'local' ? 'local' : 'cloud');
  if ($('#setCloudEndpoint')) $('#setCloudEndpoint').value = cloud.endpoint || '';
  if ($('#setCloudTimeout')) $('#setCloudTimeout').value = cloud.timeout || 8;
  if ($('#setCloudToken')) {
    const tok = $('#setCloudToken');
    tok.value = '';
    /* token 不回显（密码框），只用占位文字说明当前是否已配置登录态 token */
    tok.placeholder = cloud.needs_login_token
      ? '未配置登录态 token，请粘贴（未填时云端自动降级本地）'
      : '已保存 token（留空 = 保持不变）';
  }
  const labels = e.labels || {};
  const name = labels[state.crack_mode] || (state.crack_mode === 'local' ? '本地 mfkey32' : '云端解算');
  setBadge('#crackModeBadge',
    local.available ? name + ' 可用' : name + ' · 本地算法未就绪',
    local.available ? 'ok' : 'warn');
}

/* ---------------------------------------------------------- 密码库（公共/用户/历史） */
let keyboxLib = 'public';

async function loadKeybox(lib) {
  if (lib) keyboxLib = lib;
  try {
    const r = await api('keybox_list', keyboxLib);
    renderKeybox(r);
  } catch (e) { log('密码库读取失败：' + e.message, 'err'); }
}

function renderKeybox(r) {
  const list = $('#keyboxList');
  if (!list) return;
  const items = (r && r.items) || [];
  const stats = (r && r.stats) || {};
  setText('#keyboxBadge', `公共 ${stats.public || 0} · 用户 ${stats.user || 0} · 历史 ${stats.history || 0}`);
  const isHistory = keyboxLib === 'history';
  if (!items.length) { list.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  list.innerHTML = items.map((it, i) => {
    const key = isHistory ? ((it.keys || []).join(' ') || '—') : (it.key || '—');
    const left = isHistory ? (it.uid || '—') : String(i);
    const canDel = (keyboxLib === 'user') || (keyboxLib === 'public' && !it.builtin);
    const del = canDel ? `<button class="btn ghost sm" data-kdel="${escapeHtml(it.id)}">删除</button>` : '';
    return `<div class="kb-row"><span class="kb-idx">${escapeHtml(left)}</span>`
      + `<span class="kb-key mono">${escapeHtml(key)}</span>${del}</div>`;
  }).join('');
  list.querySelectorAll('[data-kdel]').forEach(b => {
    b.onclick = async () => {
      try { await api('keybox_remove', b.dataset.kdel, '', keyboxLib); await loadKeybox(); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    };
  });
}

async function doKeyboxAdd() {
  const inp = $('#keyboxInput');
  const text = inp ? inp.value.trim() : '';
  if (!text) { toast('请输入密钥', 'err'); return; }
  try {
    const r = await api('keybox_add', text, 'user');
    if (inp) inp.value = '';
    toast(`已添加 ${r.added} 条（跳过 ${r.skipped}）`, 'ok');
    setKeyboxTab('user');
  } catch (e) { toast('添加失败：' + e.message, 'err'); }
}

async function doFormatCard() {
  if (!state.connected) { toast('请先连接读卡器', 'err'); return; }
  const ok = await confirmBox('格式化卡片（不可逆）',
    '将用当前密钥把整卡 64 个块逐块写 0，密钥块写回厂商默认值。\n' +
    '卡片数据不可恢复！\n\n确定继续吗？');
  if (!ok) return;
  try {
    busy(true, '格式化卡片…');
    setLed('busy');
    const key = (($('#setKey') || {}).value || 'FFFFFFFFFFFF').replace(/\s/g, '').toUpperCase();
    const res = await api('format_card', key, 0, true);
    const bad = (res.failed || []).length;
    toast(`格式化完成：成功 ${res.written}/${res.total} 块` + (bad ? `，失败 ${bad}` : ''), bad ? 'warn' : 'ok');
    log(`格式化卡片：成功 ${res.written}/${res.total} 块` + (bad ? `，失败 ${bad}` : ''), bad ? 'warn' : 'ok');
  } catch (e) { toast('格式化失败：' + e.message, 'err'); }
  finally { busy(false); setLed(state.connected ? 'on' : ''); }
}
async function doKeyboxVerify() {
  const out = $('#keyboxVerifyOut');
  const btn = $('#keyboxVerify');
  if (btn) btn.disabled = true;
  if (out) out.textContent = '正在逐把认证…';
  try {
    const block = +($('#keyboxBlock') ? $('#keyboxBlock').value : 0) || 0;
    const kt = +($('#keyboxKeyType') ? $('#keyboxKeyType').value : 0) || 0;
    const r = await api('keybox_batch_verify', null, keyboxLib, block, kt);
    const hits = (r && r.hits) || [];
    const text = hits.length
      ? `命中 ${hits.length} 把：扇区 ${block} KEY ${kt === 0 ? 'A' : 'B'} → ${hits[0].key}`
      : `未命中（共试 ${(r && r.tried) || 0} 把）`;
    if (out) out.textContent = text;
    toast(text, hits.length ? 'ok' : 'warn');
    log('批量验证：' + text, hits.length ? 'ok' : 'warn');
    if (hits.length) loadKeybox('history');
  } catch (e) {
    if (out) out.textContent = '批量验证失败：' + e.message;
    toast('批量验证失败：' + e.message, 'err');
  } finally { if (btn) btn.disabled = false; }
}
async function doKeyboxClear() {
  const name = keyboxLib === 'public' ? '公共资源库' : (keyboxLib === 'user' ? '用户密钥库' : '读卡历史库');
  const ok = await confirmBox('清空密码库', `确定清空「${name}」吗？`);
  if (!ok) return;
  try { await api('keybox_clear', keyboxLib); await loadKeybox(); toast('已清空', 'ok'); }
  catch (e) { toast('清空失败：' + e.message, 'err'); }
}

function setKeyboxTab(lib) {
  keyboxLib = lib;
  const map = { public: '#kbTabPublic', user: '#kbTabUser', history: '#kbTabHistory' };
  Object.keys(map).forEach(k => { const el = $(map[k]); if (el) el.classList.toggle('is-active', k === lib); });
  loadKeybox(lib);
}
async function doWxToken() {
  const out = $('#wxTokenOut');
  const btn = $('#btnWxToken');
  if (btn) btn.disabled = true;
  if (out) out.textContent = '正在扫描微信小程序进程（请保持小程序已打开）…';
  try {
    const r = await api('wx_fetch_token', true);
    const hours = (r && r.hours != null) ? r.hours : Math.floor(((r && r.expires_in) || 0) / 3600);
    const text = `已获取云端 token：UID ${(r && r.uid) || '?'} · 剩余约 ${hours} 小时 · 已自动填入并切到云端`;
    if (out) out.textContent = text;
    state.settings = await api('get_settings');
    state.crack_mode = 'cloud';
    renderCrackMode('cloud');
    if ($('#setCloudToken')) $('#setCloudToken').value = '';
    toast('已从微信获取云端 token', 'ok');
    log(text, 'ok');
    await refreshCrackEngine();
  } catch (e) {
    if (out) out.textContent = '获取失败：' + e.message;
    toast('获取 token 失败：' + e.message, 'err');
    log('获取云端 token 失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function doVerifyLocal() {
  const out = $('#crackVerifyOut');
  const btn = $('#btnVerifyLocal');
  if (btn) btn.disabled = true;
  if (out) out.textContent = '正在跑官方 mfkey32 测试向量…';
  try {
    const r = await api('crack_vectors');
    const hits = (r && r.hits) || 0;
    const total = (r && r.total) || 0;
    const ok = !!(r && r.ok);
    const text = `本地算法自检：${hits}/${total} 命中（期望密钥 ${(r && r.expect) || '—'}，`
      + `耗时 ${(r && r.elapsed) || 0}s）` + (ok ? '' : ' · ' + ((r && r.error) || '未通过'));
    if (out) out.textContent = text;
    setBadge('#crackModeBadge', ok ? `本地算法自检通过 ${hits}/${total}`
                                   : `本地算法自检未通过 ${hits}/${total}`,
      ok ? 'ok' : 'err');
    toast(ok ? `本地算法验证通过（${hits}/${total}）` : `本地算法验证未通过（${hits}/${total}）`,
      ok ? 'ok' : 'err');
    log(text, ok ? 'ok' : 'warn');
  } catch (e) {
    if (out) out.textContent = '本地算法自检失败：' + e.message;
    toast('本地算法自检失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------------------------------------------------- 启动加载 */
function fillSectorSelects() {
  const opts = [];
  for (let i = 0; i < 16; i++) opts.push(`<option value="${i}">扇区 ${i}</option>`);
  if ($('#crackSector')) $('#crackSector').innerHTML = opts.join('');
  if ($('#setCrackSector')) $('#setCrackSector').innerHTML = opts.join('');
}

async function loadBackend() {
  const plan = [];
  const step = (label, fn) => plan.push({ label, fn });

  step('读取应用信息', async () => {
    const info = await api('app_info');
    state.methods = info.crack_methods || [];
    state.chain = info.crack_chain || [];
    setText('#aboutVer', info.version || '2.2.0');
    renderMethods();
    if (info.last_card && info.last_card.uid) {
      const lc = info.last_card;
      setText('#fUid', lc.uid || '—');
      setText('#fClass', lc.class_label || '—');
      setText('#fType', lc.card_type || '—');
      setText('#fSak', lc.sak || '—');
      setText('#fBand', lc.band || '—');
      setText('#chipClass', `${lc.class_label || '上次卡片'}（缓存）`);
      const uid = (lc.uid || '').match(/.{1,2}/g);
      setText('#chipUid', uid ? uid.join(' ') : '—— —— —— ——');
      log(`载入上次卡片记录：${lc.class_label || ''} UID ${lc.uid}（${lc.ts || ''}）`, 'info');
    }
    if (info.baud_choices) fillBaud(info.baud_choices, info.baud_choices.includes(115200) ? 115200 : info.baud_choices[0]);
    renderWriteModes(info.write_modes);
    renderPhoneModes(info.phone_nfc);
  });

  step('读取设置', async () => {
    state.settings = await api('get_settings');
    applyAppearance(state.settings);
    if ($('#setKey')) $('#setKey').value = state.settings.default_key || 'FFFFFFFFFFFF';
    if ($('#keyInput')) $('#keyInput').value = state.settings.default_key || 'FFFFFFFFFFFF';
    if ($('#setKeyType')) $('#setKeyType').value = state.settings.default_key_type == null ? 0 : state.settings.default_key_type;
    if ($('#keyType')) $('#keyType').value = state.settings.default_key_type == null ? 0 : state.settings.default_key_type;
    if ($('#setCrackSector')) $('#setCrackSector').value = state.settings.crack_sector == null ? 0 : state.settings.crack_sector;
    if ($('#setCrackTimeout')) $('#setCrackTimeout').value = state.settings.crack_timeout || 25;
    if ($('#setAutoCrack')) $('#setAutoCrack').checked = state.settings.auto_crack !== false;
    if ($('#setAutoFind')) $('#setAutoFind').checked = state.settings.auto_find !== false;
    /* 破解页扫描开关与全局「寻卡后自动破解」保持同步 */
    if ($('#scanAutoCrack')) $('#scanAutoCrack').checked = state.settings.auto_crack !== false;
    renderCrackMode(state.settings.crack_mode === 'local' ? 'local' : 'cloud');
    loadKeybox('public');
    const kb = $('#keyboxBlock');
    if (kb && !kb.options.length) {
      for (let i = 0; i < 16; i++) {
        const o = document.createElement('option');
        o.value = String(i); o.textContent = '扇区 ' + i;
        kb.appendChild(o);
      }
    }
    if ($('#setCloudEndpoint')) $('#setCloudEndpoint').value = state.settings.cloud_endpoint || '';
    if ($('#setCloudTimeout')) $('#setCloudTimeout').value = state.settings.cloud_timeout || 8;
    const S = state.settings || {};
    const setChk = (id, v) => { const el = $(id); if (el) el.checked = !!v; };
    setChk('#setHidEnable', S.hid_enable);
    setChk('#setNonFullKey', S.non_full_key_read);
    setChk('#setEncZero', S.encrypted_zero_sectors);
    setChk('#setWriteTips', S.write_card_tips);
    setChk('#setPowerDisplay', S.power_display);
    setChk('#setSak88', S.sak88);
    if ($('#baudSel') && state.settings.baud) $('#baudSel').value = String(state.settings.baud);
    state.spin.speed = state.settings.device_spin == null ? 18 : state.settings.device_spin;
    state.spin.on = state.spin.speed > 0;
    if ($('#rngSpin')) $('#rngSpin').value = state.spin.speed;
    setText('#lblSpin', state.spin.speed);
    if ($('#btnSpinToggle')) $('#btnSpinToggle').textContent = state.spin.on ? '暂停旋转' : '继续旋转';
    switchConnTab(state.settings.connection_mode === 'usb' ? 'usb' : 'ble');
  });

  step('读取破解引擎状态', async () => { await refreshCrackEngine(); });

  step('初始化扇区表', async () => {
    fillSectorSelects();
    renderSectors();
    renderChainMini();
  });

  step('加载背景', async () => { await refreshWalls(); });

  step('加载卡夹', async () => { await refreshBox(); });

  step('读取设备状态', async () => {
    const snap = await api('status');
    if (snap && snap.connected) {
      state.connected = true;
      applyConnectedUI(snap, snap.mac || snap.port || '', snap.transport_label || '');
    }
    if (snap && snap.last_card && snap.last_card.uid && $('#fUid') && $('#fUid').textContent === '—') {
      fillCardInfo(snap.last_card);
    }
  });

  const failed = [];
  for (const s of plan) {
    try { await s.fn(); }
    catch (e) { failed.push(s); log(s.label + ' 失败：' + (e && e.message ? e.message : e), 'err'); }
  }
  if (failed.length) {
    await sleep(600);
    for (const s of failed) {
      try { await s.fn(); log(s.label + ' 重试成功', 'ok'); }
      catch (e) { log(s.label + ' 重试仍失败：' + (e && e.message ? e.message : e), 'err'); }
    }
  }
  const wallGrid = $('#wallGrid');
  if (wallGrid && wallGrid.children.length === 0) {
    try { renderWalls([]); } catch (e) { /* 忽略 */ }
  }
}

async function init() {
  if (state.ready) return;
  bindUI();
  initDevice3D();
  state.ready = true;
  log('界面已就绪，正在连接后端…', 'ok');
  await loadBackend();
  log('NFC Mini Studio 2.2.0 已就绪', 'ok');
  log('提示：蓝牙连接前请先让读卡器与手机微信断开；USB 模式（串口/直连）请先插好读卡器', 'info');
}

let __booted = false;
function boot() {
  if (__booted) return false;
  const keys = (window.pywebview && window.pywebview.api) ? Object.keys(window.pywebview.api).length : 0;
  window.__boot = { at: new Date().toISOString(), apiKeys: keys, ready: apiReady() };
  if (!apiReady()) return false;
  __booted = true;
  init().catch(e => {
    log('初始化异常：' + (e && e.message ? e.message : e), 'err');
    toast('初始化异常：' + (e && e.message ? e.message : e), 'err');
  });
  return true;
}

window.addEventListener('pywebviewready', () => boot());

document.addEventListener('DOMContentLoaded', () => {
  switchConnTab('ble');
  setLed('');
  const dock = $('#progDock');
  if (dock) dock.classList.add('is-idle');
  if (boot()) return;
  let ticks = 0;
  const timer = setInterval(() => {
    ticks += 1;
    if (boot() || ticks > 100) {
      clearInterval(timer);
      if (!__booted) {
        log('后端接口未注入（window.pywebview.api 不存在），界面已绑定但无法调用功能', 'err');
        toast('后端未就绪，请重启程序', 'err');
      }
    }
  }, 150);
});

/* ---------------------------------------------------------- 自检钩子 */
window.__diag = function () {
  const api = (window.pywebview && window.pywebview.api) || {};
  const apiKeys = Object.keys(api);
  const bg = document.getElementById('bg');
  const wallGrid = document.getElementById('wallGrid');
  const mustBind = Array.prototype.slice.call(document.querySelectorAll('button, input[type=range]'));
  const unbound = mustBind
    .filter(e => !e.onclick && !e.oninput && !e.onchange)
    .filter(e => e.id !== 'modalOk' && e.id !== 'modalCancel')
    .map(e => e.id || ('btn-noid:' + (e.textContent || '').trim().slice(0, 6)));
  const glassEl = document.querySelector('.glass');
  const model = document.getElementById('deviceModel');
  return {
    ready: !!state.ready,
    boot: window.__boot || null,
    connected: !!state.connected,
    mode: state.mode,
    apiKeys: apiKeys.length,
    apiMissingCore: CORE_API.filter(k => typeof api[k] !== 'function').join(','),
    wiredApiMissing: WIRED_API.filter(k => typeof api[k] !== 'function').join(','),
    wiredDevices: (state.wired.devices || []).length,
    wiredConnected: !!state.wired.connected,
    wiredRows: $$('#wiredList li').length,
    unbound: unbound.join(','),
    unboundCount: unbound.length,
    navCount: $$('.nav-item').length,
    wallCount: wallGrid ? wallGrid.children.length : -1,
    alpha: getComputedStyle(document.documentElement).getPropertyValue('--alpha').trim(),
    blur: getComputedStyle(document.documentElement).getPropertyValue('--blur').trim(),
    preset: (state.settings && state.settings.preset) || '',
    glassBackdrop: glassEl ? (getComputedStyle(glassEl).backdropFilter || getComputedStyle(glassEl).webkitBackdropFilter || '') : '',
    bgImage: !!(bg && bg.classList.contains('has-image')),
    activeView: ($('.view.is-active') || {}).id || '',
    deviceTransform: model ? (model.style.transform || '') : '',
    sectorCount: $$('#sectors .sector').length,
    methodCount: $$('#methodList .method').length,
    chainRows: $$('#chainMini .chain-row').length,
    usbPaneHidden: !!(document.getElementById('paneUsb') || {}).classList &&
                   document.getElementById('paneUsb').classList.contains('is-hidden'),
    logs: $$('#logBody > *').slice(-8).map(e => e.textContent.trim()),
    errLogs: $$('#logBody > *').filter(e => /l-err/.test(e.className)).map(e => e.textContent.trim()),
    toasts: $$('#toasts > *').map(e => e.textContent.trim()),
  };
};
