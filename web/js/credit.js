/*
 * 署名小问号（2026-11-18 主人令：提词器 / 命令方块 / search 都要标作者）。
 * 只读展示作者与许可，不改任何第三方文件、不进任何提示词。
 * search = 本项目自研 + 内嵌的 Easydict.app（tisfeng / Copyright © 2023-2025）。
 * Easydict 自带第三方许可按它包内原始 LICENSE 逐字标，不编造。
 */
(function () {
  'use strict';
  const CREDITS = {
    search: {
      title: 'search · 作者与来源',
      rows: [
        ['本项目作者', '主人（本项目自研）'],
        ['查词内核', 'Easydict.app — 作者 tisfeng'],
        ['Easydict 版本', '2.25.0（com.izual.Easydict）'],
        ['Easydict 版权', 'Copyright © 2023-2025 tisfeng. All rights reserved.'],
        ['附带第三方', 'Dark Reader（MIT）、GitHub Copilot 图标（MIT）、其它 Pods 组件'],
        ['许可原文', 'Easydict.app/Contents/Resources/*-LICENSE.txt 及 Pods-Easydict-acknowledgements.plist'],
      ],
      note: '只标作者与许可，点开不影响查词；Easydict 是内嵌的原版 app，项目不改它的文件。各家许可原文见上面路径。',
    },
  };
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function openCredit(key) {
    const c = CREDITS[key];
    if (!c) return;
    closeCredit();
    const rows = c.rows.map(([k, v]) => {
      const val = /^https?:\/\//.test(v)
        ? '<a href="' + esc(v) + '" target="_blank" rel="noopener noreferrer">' + esc(v) + '</a>'
        : esc(v);
      return '<div class="credit-row"><b>' + esc(k) + '</b><span>' + val + '</span></div>';
    }).join('');
    const veil = document.createElement('div');
    veil.className = 'credit-veil';
    veil.dataset.creditVeil = '1';
    veil.innerHTML = '<div class="credit-card" role="dialog" aria-label="' + esc(c.title) + '">'
      + '<h3>' + esc(c.title) + '</h3>'
      + '<div class="credit-sub">点开只为标明来源，不影响功能</div>'
      + rows
      + '<p class="credit-note">' + esc(c.note) + '</p>'
      + '<div class="credit-btns"><button class="credit-close" type="button" data-credit-close>关掉</button></div>'
      + '</div>';
    veil.addEventListener('click', (e) => {
      if (e.target === veil || e.target.closest('[data-credit-close]')) closeCredit();
    });
    document.body.appendChild(veil);
  }
  function closeCredit() {
    const el = document.querySelector('[data-credit-veil]');
    if (el) el.remove();
  }
  document.addEventListener('click', (e) => {
    const q = e.target.closest('[data-credit]');
    if (q) { e.preventDefault(); e.stopPropagation(); openCredit(q.dataset.credit); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeCredit(); });
  window.openCredit = openCredit;
})();
