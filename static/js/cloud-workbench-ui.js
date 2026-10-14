/* StrimKeep 115 P1 — progressive UI. Keeps the verified workbench actions intact. */
(function () {
  'use strict';
  if (!window.cloudWorkbench || typeof window.cloudWorkbench.load !== 'function') return;

  const originalLoad = window.cloudWorkbench.load;
  const TABS = {
    accounts: ['账号与授权', '扫码授权、多账号管理'],
    files: ['云端文件', '浏览文件、快速选择任务目录'],
    transfer: ['分享转存', '预览来源、独立接收、安全确认'],
    organize: ['影视整理', 'TMDB 识别、命名、归档'],
    strm: ['STRM 媒体库', '生成、同步与播放引用'],
    playback: ['播放服务', '302、NFO、Emby 反代'],
    jobs: ['任务中心', '转存、整理与异常核实']
  };
  const ROLES = { source: '扫描来源', target: '整理归档', transfer: '分享转存' };
  let picker = null;
  let pickerVersion = 0;
  let refreshVersion = 0;

  function el(id) { return document.getElementById(id); }
  function root() { return el('tab-cloud'); }
  function htmlEscape(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, c =>
      ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  }
  function api(path) {
    return fetch('/api/cloud' + path, {credentials:'same-origin', cache:'no-store'})
      .then(async response => {
        const json = await response.json();
        if (!response.ok) throw Error(json.detail || '读取工作台信息失败');
        return json;
      });
  }
  function isVisible() { return root() && !root().hidden && root().offsetParent !== null; }
  function section() {
    const parent = root();
    if (!parent || !el('cw-tabs')) return;
    parent.classList.add('cw-refined');
    const top = parent.querySelector('.page-intro');
    if (top && !el('cw-ui-hero')) {
      top.classList.add('cw-legacy-intro');
      const hero = document.createElement('div');
      hero.id = 'cw-ui-hero';
      hero.className = 'cw-ui-hero';
      hero.innerHTML = '<div class="cw-ui-hero-copy"><span class="cw-ui-kicker">STRIMKEEP / 115 CLOUD</span>' +
        '<h2>115 媒体工作台</h2><p>把分享资源变成可管理、可播放的影视库。</p></div>' +
        '<div class="cw-ui-hero-actions"><button type="button" data-cw-tab="transfer" class="cw-ui-primary">开始转存 <span aria-hidden="true">↗</span></button>' +
        '<button type="button" data-cw-tab="organize" class="cw-ui-secondary">整理影视</button>' +
        '<button type="button" data-cw-refresh="1" class="cw-ui-secondary">刷新状态</button></div>';
      top.after(hero);
    }
    if (!el('cw-ui-stats')) {
      const stats = document.createElement('div');
      stats.id = 'cw-ui-stats';
      stats.className = 'cw-ui-stats';
      stats.setAttribute('aria-label', '网盘工作台概览');
      stats.innerHTML = [
        ['accounts', '已保存账号', '—', '点击查看授权'],
        ['pending', '待处理任务', '—', '点击查看任务'],
        ['assets', '最近播放引用', '—', '最多展示 100 条'],
        ['last', '最近任务', '—', '查看执行结果']
      ].map(([id, label, value, detail]) =>
        '<div class="cw-ui-stat"><span class="cw-ui-stat-label">' + label + '</span>' +
        '<strong id="cw-ui-' + id + '">' + value + '</strong><small>' + detail + '</small></div>'
      ).join('');
      el('cw-ui-hero').after(stats);
    }
    const tabs = el('cw-tabs');
    tabs.setAttribute('aria-label', '115 功能导航');
    tabs.querySelectorAll('[data-cw-tab]').forEach(button => {
      const val = button.dataset.cwTab;
      if (!TABS[val]) return;
      button.setAttribute('title', TABS[val][1]);
    });
    if (!el('cw-ui-guide')) {
      const guide = document.createElement('div');
      guide.id = 'cw-ui-guide';
      guide.className = 'cw-ui-guide';
      guide.innerHTML = '<div><span class="cw-ui-guide-label">推荐流程</span><strong>扫码登录 → 选目录 → 转存 → 整理 → 生成 STRM</strong>' +
        '<span class="cw-ui-guide-detail">写入前核实来源与目标，未确认的操作不会自动执行。</span></div>' +
        '<button type="button" data-cw-picker="source" class="cw-ui-secondary">选择扫描目录</button>';
      tabs.after(guide);
    }
    const cards = [
      ['cw-files', [{role:'source',name:'扫描来源'}, {role:'target',name:'整理目标'}, {role:'transfer',name:'转存目标'}]],
      ['cw-transfer', [{role:'transfer',name:'选择转存目录'}, {role:'target',name:'选择归档目录'}]],
      ['cw-organize', [{role:'source',name:'选择扫描目录'}, {role:'target',name:'选择归档目录'}]],
      ['cw-strm', [{role:'source',name:'选择 STRM 来源'}]]
    ];
    cards.forEach(([id, roles]) => {
      const parentCard = el(id);
      if (!parentCard || parentCard.querySelector('.cw-ui-directory-tools')) return;
      const tools = document.createElement('div');
      tools.className = 'cw-ui-directory-tools';
      tools.innerHTML = '<div><strong>目录快速选择</strong><small>从 115 目录树选择，无需手填 CID</small></div>' +
        '<div class="cw-ui-directory-buttons">' + roles.map(r =>
          '<button type="button" class="cw-ui-secondary" data-cw-picker="' + r.role + '">' + r.name + '</button>'
        ).join('') + '</div>';
      const header = parentCard.querySelector('h3');
      if (header) header.after(tools); else parentCard.prepend(tools);
    });
    const accountCard = el('cw-account')?.closest('.panel');
    if (accountCard) accountCard.classList.add('cw-ui-account-bar');
    ['cw-accounts','cw-files','cw-transfer','cw-organize','cw-strm','cw-playback','cw-jobs'].forEach(id => {
      const node = el(id);
      if (node) node.classList.add('cw-ui-section');
    });
    if (!parent.dataset.cwUiEvents) {
      parent.dataset.cwUiEvents = '1';
      parent.addEventListener('click', event => {
        const open = event.target.closest('[data-cw-picker]');
        if (open) { event.preventDefault(); openPicker(open.dataset.cwPicker); }
        const refresh = event.target.closest('[data-cw-refresh]');
        if (refresh) void refreshStats();
      });
    }
  }

  async function refreshStats() {
    if (!el('cw-ui-stats')) return;
    const revision = ++refreshVersion;
    const responses = await Promise.allSettled([
      api('/status'), api('/assets?limit=100&offset=0'), api('/jobs')
    ]);
    if (revision !== refreshVersion || !el('cw-ui-stats')) return;
    const status = responses[0].status === 'fulfilled' ? responses[0].value : null;
    const assets = responses[1].status === 'fulfilled' ? responses[1].value : null;
    const jobs = responses[2].status === 'fulfilled' ? responses[2].value : null;
    el('cw-ui-accounts').textContent = status ? String(status.accounts.length) : '—';
    el('cw-ui-pending').textContent = status ? String(status.pending) : '—';
    el('cw-ui-assets').textContent = assets ? String(assets.items.length) : '—';
    if (jobs) {
      const last = jobs.items[0];
      el('cw-ui-last').textContent = last ? ({success:'已完成',failed:'失败',needs_review:'待处理',uncertain:'待核实',running:'执行中',queued:'排队中'}[last.state] || last.state) : '暂无';
    }
  }

  function ensureDialog() {
    if (el('cw-ui-picker')) return el('cw-ui-picker');
    const overlay = document.createElement('div');
    overlay.id = 'cw-ui-picker';
    overlay.className = 'cw-ui-picker';
    overlay.hidden = true;
    overlay.innerHTML = '<div class="cw-ui-picker-panel" role="dialog" aria-modal="true" aria-labelledby="cw-ui-picker-title">' +
      '<div class="cw-ui-picker-head"><div><small>115 目录浏览器</small><h3 id="cw-ui-picker-title">选择目录</h3></div>' +
      '<button type="button" class="cw-ui-picker-close" data-picker-action="close" aria-label="关闭">×</button></div>' +
      '<div class="cw-ui-picker-path" id="cw-ui-picker-path">根目录</div>' +
      '<div class="cw-ui-picker-toolbar"><button type="button" class="cw-ui-secondary" data-picker-action="up">↑ 上级</button>' +
      '<button type="button" class="cw-ui-secondary" data-picker-action="reload">刷新</button>' +
      '<span id="cw-ui-picker-counter" aria-live="polite"></span></div>' +
      '<div id="cw-ui-picker-files" class="cw-ui-picker-files" aria-live="polite"></div>' +
      '<div class="cw-ui-picker-bottom"><div class="cw-ui-picker-pages">' +
      '<button type="button" data-picker-action="prev">上一页</button>' +
      '<button type="button" data-picker-action="next">下一页</button></div>' +
      '<div class="cw-ui-picker-actions"><button type="button" class="cw-ui-secondary" data-picker-action="close">取消</button>' +
      '<button type="button" class="cw-ui-primary" data-picker-action="confirm">选择当前目录</button></div></div></div>';
    overlay.addEventListener('click', async event => {
      if (event.target === overlay) { closePicker(); return; }
      const btn = event.target.closest('[data-picker-action]');
      if (!btn || !picker) return;
      const a = btn.dataset.pickerAction;
      if (a === 'close') { closePicker(); return; }
      if (a === 'confirm') { selectDirectory(); return; }
      if (a === 'up' && picker.trail.length > 1) picker.trail.pop();
      if (a === 'prev') picker.offset = Math.max(0, picker.offset - 100);
      if (a === 'next' && picker.hasMore) picker.offset += 100;
      if (a === 'up' || a === 'reload') picker.offset = 0;
      if (a === 'folder') {
        const id = btn.dataset.id;
        const name = btn.dataset.name;
        picker.trail.push({id,name});
        picker.offset = 0;
      }
      if (['up','prev','next','reload','folder'].includes(a)) await listDirectory();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && picker) { event.preventDefault(); closePicker(); }
    });
    root().append(overlay);
    return overlay;
  }

  function openPicker(role) {
    if (!(role in ROLES)) return;
    const account = el('cw-account')?.value;
    if (!account) {
      const message = el('cw-message');
      if (message) message.textContent = '请先扫码登录并选择 115 账号，再打开目录选择器。';
      return;
    }
    const overlay = ensureDialog();
    picker = {role, account, trail:[{id:'0',name:'根目录'}], offset:0, hasMore:false, previousFocus:document.activeElement};
    el('cw-ui-picker-title').textContent = '选择' + ROLES[role] + '目录';
    overlay.hidden = false;
    document.body.classList.add('cw-ui-picker-open');
    overlay.querySelector('[data-picker-action="close"]').focus();
    void listDirectory();
  }

  async function listDirectory() {
    if (!picker) return;
    const local = picker;
    const rev = ++pickerVersion;
    const node = el('cw-ui-picker-files');
    node.innerHTML = '<p class="cw-ui-picker-empty">正在读取 115 目录…</p>';
    const current = local.trail[local.trail.length-1];
    el('cw-ui-picker-path').textContent = local.trail.map(x => x.name).join(' / ');
    try {
      const qs = new URLSearchParams({account_id:local.account,cid:current.id,offset:String(local.offset)});
      const result = await api('/files?' + qs);
      if (rev !== pickerVersion || picker !== local || el('cw-account').value !== local.account) return;
      local.hasMore = Boolean(result.has_more);
      const folders = result.items.filter(item => item.is_dir);
      el('cw-ui-picker-counter').textContent = '共 ' + result.total + ' 项 · 当前页 ' + folders.length + ' 个目录';
      node.innerHTML = folders.length ? folders.map(item =>
        '<button type="button" class="cw-ui-picker-folder" data-picker-action="folder" data-id="' + htmlEscape(item.id) +
        '" data-name="' + htmlEscape(item.name) + '"><span class="cw-ui-picker-folder-icon" aria-hidden="true">▱</span>' +
        '<span>' + htmlEscape(item.name) + '</span><span aria-hidden="true">›</span></button>'
      ).join('') : '<p class="cw-ui-picker-empty">当前页没有下级文件夹，可以选择当前目录。</p>';
      overlayPageButtons(local);
    } catch (error) {
      if (rev !== pickerVersion || picker !== local) return;
      node.innerHTML = '<p class="cw-ui-picker-error">目录读取失败：' + htmlEscape(error.message) + '</p>';
    }
  }
  function overlayPageButtons(local) {
    const overlay = el('cw-ui-picker');
    overlay.querySelector('[data-picker-action="prev"]').disabled = local.offset === 0;
    overlay.querySelector('[data-picker-action="next"]').disabled = !local.hasMore;
    overlay.querySelector('[data-picker-action="up"]').disabled = local.trail.length === 1;
  }
  function selectDirectory() {
    const local = picker;
    if (!local) return;
    if (el('cw-account').value !== local.account) { closePicker(); return; }
    const folder = local.trail[local.trail.length-1];
    if (typeof window.cloudWorkbench.selectDirectory !== 'function') {
      el('cw-ui-picker-files').textContent = '目录选择接口未安装，请运行补丁安装脚本。';
      return;
    }
    try {
      window.cloudWorkbench.selectDirectory(local.role, {
        id:folder.id,
        name:local.trail.map(x=>x.name).join(' / '),
        trailIds:local.trail.map(x=>x.id)
      }, local.account);
      closePicker();
    } catch (error) {
      el('cw-ui-picker-files').innerHTML = '<p class="cw-ui-picker-error">' + htmlEscape(error.message) + '</p>';
    }
  }
  function closePicker() {
    if (!picker) return;
    const old = picker;
    picker = null;
    pickerVersion++;
    el('cw-ui-picker').hidden = true;
    document.body.classList.remove('cw-ui-picker-open');
    if (old.previousFocus && old.previousFocus.isConnected) old.previousFocus.focus();
  }

  window.cloudWorkbench.load = async function () {
    const result = await originalLoad.apply(this, arguments);
    section();
    void refreshStats();
    return result;
  };
  // Kept deliberately small for DOM regression checks; no private backend data exposed.
  window.cloudWorkbenchUi = {refreshStats};
})();
