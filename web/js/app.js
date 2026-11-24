// search · 前端。没有框架，只有一个状态对象和一组渲染函数。

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  roots: [],
  switches: { web: true, manage: true, fun: true, verbosity: "full", pin: true },
  allMode: null,        // 简：null=各卡自理 | "expand"=全部展开 | "collapse"=全部收起
  sources: {},          // 联网里的每个源：{ "百度百科": true, "DeepL": false, ... }
  // 分类名单由后端 /api/state 单点下发（groups.web / groups.fun），JS 不另写一份
  groups: { web: [], fun: [] },
  changes: [],
  changedSnapshot: null,
  renderedTerm: null,
  dockedApplied: null,
  pinApplied: null,
  scopeInit: false,
  index: { files: 0, building: false, progress: { done: 0, total: 0 } },
  commands: { running: false, newFiles: [] },
  term: "",
  raisedTerm: "",        // 上一次已经为它抬过窗口的词：同一个词输入阶段只抬一次
  payload: null,
  web: [],
  webPending: [],
  webSince: 0,
  webToken: 0,
  webDone: true,
  memeSites: null,
  hideFiles: false,
  rootsExpanded: false,
  scope: "roots",
  reqId: 0,
  typing: 0,
  folded: new Set(),
};

/* ------------------------------------------------------------------ 工具 */
const escapeHtml = (text) => String(text == null ? "" : text)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

function markSnippet(text, terms) {
  // 片段是从原文里截的，先把 Markdown 的强调符号去掉，读起来才不花
  const cleaned = String(text || "").replace(/\*\*(.+?)\*\*/g, "$1").replace(/`/g, "");
  let html = escapeHtml(cleaned);
  const list = (terms || []).filter(Boolean).sort((a, b) => b.length - a.length)
    .map((t) => escapeHtml(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!list.length) return html;
  try {
    html = html.replace(new RegExp("(" + list.join("|") + ")", "gi"), "<mark>$1</mark>");
  } catch (_) { /* 正则异常就原样输出 */ }
  return html;
}

function toast(text, kind) {
  const box = document.createElement("div");
  box.className = "toast" + (kind === "bad" ? " is-bad" : "");
  box.textContent = text;
  $("#toastLayer").appendChild(box);
  setTimeout(() => {
    box.style.transition = "opacity .2s";
    box.style.opacity = "0";
    setTimeout(() => box.remove(), 220);
  }, kind === "bad" ? 3400 : 2200);
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
  const data = await response.json().catch(() => ({ ok: false, error: "服务没有返回 JSON" }));
  if (!response.ok && !data.error) data.error = "请求失败（" + response.status + "）";
  return data;
}

function ago(ts) {
  if (!ts) return "";
  const gap = Date.now() / 1000 - ts;
  if (gap < 60) return "刚刚";
  if (gap < 3600) return Math.floor(gap / 60) + " 分钟前";
  if (gap < 86400) return Math.floor(gap / 3600) + " 小时前";
  return Math.floor(gap / 86400) + " 天前";
}

const KIND_TEXT = { cn: "中文", en: "英语", phrase: "英语短语", mixed: "中英混写", other: "" };
const SVC_CLASS = {
  "百度百科": "is-baidu",
  "MDN（前端文档）": "is-mdn",
  "Stack Overflow": "is-so",
  "维基百科": "is-wiki",
  "萌娘百科": "is-moe",
  "英语词典": "is-easy",
  "DeepL": "is-deepl",
};
const SVC_LETTER = {
  "百度百科": "百",
  "MDN（前端文档）": "M",
  "Stack Overflow": "S",
  "维基百科": "W",
  "萌娘百科": "萌",
  "英语词典": "EN",
  "DeepL": "D",
};
// 小标沿用它原本那个色块，不另换颜色——一眼能认出是谁被关了
const SVC_COLOR = {
  "百度百科": "#2932e1",
  "MDN（前端文档）": "#1b1b1b",
  "Stack Overflow": "#f48024",
  "维基百科": "#636466",
  "萌娘百科": "#ff7ba9",
  "英语词典": "#c0472a",
  "DeepL": "#0f2b46",
};
const TOG_MARK = {
  web: { letter: "联", icon: "#i-globe", color: "#0a7cff", name: "联网" },
  manage: { letter: "管", icon: "#i-book", color: "#5b5bd6", name: "管理" },
  fun: { letter: "娱", icon: "#i-meme", color: "#ff7ba9", name: "娱乐" },
};

// 某个源归哪个分类总闸（名单由后端下发；没下发的源默认归联网）
function groupOf(name) {
  const g = state.groups || {};
  return (g.fun || []).includes(name) ? "fun" : "web";
}

// 该分类总闸下的源里有几个被单独关掉了（用于三态的「淡黄提醒」）
function anySourceOff(group) {
  return Object.keys(state.sources || {}).some(
    (name) => groupOf(name) === group && !state.sources[name]
  );
}

/* ------------------------------------------------------------------ 顶栏 */
function cbNewFiles() {
  return state.commands.running ? (state.commands.newFiles || []) : [];
}

function renderChips() {
  const changed = state.changes.filter((c) => !c.acked);
  const newFiles = cbNewFiles();
  $("#newChip").hidden = newFiles.length === 0;
  $("#newCount").textContent = String(newFiles.length);
  $("#newChip").classList.toggle("pill-new", newFiles.length > 0);
  $("#changedChip").hidden = changed.length === 0;
  $("#changedCount").textContent = String(changed.length);
  // 「正常」= 这批文件都看过了；点它把下面的文件卡一次收掉
  const hits = (state.payload && state.payload.hits) || [];
  $("#okChip").hidden = changed.length > 0 || !hits.length || state.hideFiles;
}

function renderChanges() {
  const panel = $("#changesPanel");
  const mode = panel.dataset.mode || "changed";
  if (panel.dataset.open !== "1") { panel.hidden = true; return; }
  const rows = mode === "new"
    ? cbNewFiles().slice(0, 30).map((item) => `
      <button class="change-row" data-open-new="${escapeHtml(item.path)}">
        <span class="tag tag-new">新增</span>
        <span class="name">${escapeHtml(item.name || item.path)}</span>
      </button>`).join("")
    : (state.changedSnapshot || state.changes).filter((c) => !c.acked).slice(0, 30).map((item) => `
      <button class="change-row ${item.kind === "missing" ? "is-missing" : ""}" data-path="${escapeHtml(item.path)}">
        <span class="tag">${item.kind === "missing" ? "缺失" : "已改"}</span>
        <span class="name">${escapeHtml(item.name || item.path)}</span>
      </button>`).join("");
  if (!rows) { panel.hidden = true; return; }
  panel.hidden = false;
  panel.innerHTML = rows;
  $$("[data-path]", panel).forEach((row) => row.addEventListener("click", () => reveal(row.dataset.path)));
  $$("[data-open-new]", panel).forEach((row) => row.addEventListener("click", async () => {
    await openAt(row.dataset.openNew, 0);
    await api("/api/cb-new/ack", { method: "POST", body: "{}" });
    panel.dataset.open = "0";
    renderChanges();
    poll(0);
  }));
}

/* ------------------------------------------------------------------ 目录 */
// 文件栏收起态按「宽度上限」决定露几个，超宽的收进「+N ▾」；展开后全部显示、可拖拽换序。
// 宽度才是真正的约束（名字长短不一也不会把栏撑变形）。想宽想窄改 ROOTS_MAX_W 一个数字即可。
const ROOTS_MAX_W = 220;   // 约两个文件夹宽
let rootsTruncated = false;

function rootChipHtml(root) {
  return `<span class="root ${root.exists ? "" : "is-dead"}" data-path="${escapeHtml(root.path)}" title="点击在访达里定位 · 拖动可换顺序">
      <svg class="icon"><use href="#i-folder"/></svg>
      <span>${escapeHtml(root.name)}</span>
      <button class="x" data-remove="${escapeHtml(root.path)}" title="移除">
        <svg class="icon" style="width:9px;height:9px"><use href="#i-close"/></svg>
      </button>
    </span>`;
}

function renderRoots() {
  const box = $("#rootChips");
  const cb = state.commands || {};
  if (state.switches.docked && cb.running) {
    $(".roots-label").textContent = "读";
    $("#addRootBtn").hidden = true;
    box.innerHTML = `<span class="root is-cb" title="${escapeHtml(cb.root || "")}">
      <svg class="icon"><use href="#i-cube"/></svg>
      <span>命令方块${cb.rootName ? " · " + escapeHtml(cb.rootName) : ""}</span>
    </span>`;
    return;
  }
  $(".roots-label").textContent = "文件";
  $("#addRootBtn").hidden = false;
  if (!state.roots.length) { box.innerHTML = `<span class="root-empty">还没登记文件夹</span>`; return; }

  // 先全部画出来，再按可用宽度决定收起哪几个——是“宽度”在管数量，不是“永远 2 个”
  box.innerHTML = state.roots.map(rootChipHtml).join("");
  $$("[data-remove]", box).forEach((btn) => btn.addEventListener("click", async (event) => {
    event.stopPropagation();
    const data = await api("/api/roots", { method: "POST", body: JSON.stringify({ action: "remove", path: btn.dataset.remove }) });
    if (data.roots) { state.roots = data.roots; renderRoots(); renderChips(); }
    toast("已移除");
  }));

  const chips = Array.from(box.querySelectorAll(".root"));
  const gap = 5;                 // .root-chips 的 gap
  const MORE_W = 42;             // +N 按钮占位
  const cap = state.rootsExpanded ? Infinity : ROOTS_MAX_W;
  let used = 0, firstHidden = -1;
  for (let i = 0; i < chips.length; i++) {
    const w = chips[i].getBoundingClientRect().width + gap;
    const limit = firstHidden === -1 ? cap - MORE_W : cap;
    if (!state.rootsExpanded && i > 0 && used + w > limit) { firstHidden = i; break; }
    used += w;
  }
  if (firstHidden !== -1) for (let j = firstHidden; j < chips.length; j++) chips[j].hidden = true;
  const hiddenCount = firstHidden === -1 ? 0 : chips.length - firstHidden;
  rootsTruncated = hiddenCount > 0;

  if (hiddenCount > 0) {
    box.insertAdjacentHTML("beforeend", `<button class="root-more" type="button" title="展开全部 ${state.roots.length} 个文件夹">
      <b>+${hiddenCount}</b>
      <svg class="icon"><use href="#i-chevron"/></svg>
    </button>`);
  } else if (state.rootsExpanded && chips.length > 1) {
    // 全部展开、且确有可收内容时，给个收起入口，chevron 翻上去
    box.insertAdjacentHTML("beforeend", `<button class="root-more is-open" type="button" title="收起">
      <svg class="icon"><use href="#i-chevron"/></svg>
    </button>`);
  }
  const more = box.querySelector(".root-more");
  if (more) more.addEventListener("click", () => {
    state.rootsExpanded = !state.rootsExpanded;
    renderRoots();
  });
}

// 拖拽排序：在 .root 上按下，移动超过阈值就进入拖动；点击（没拖动）则定位文件夹。
// 收起状态下只点不拖，避免把隐藏目录挤到末尾。监听器挂在容器上，重渲染不失效。
let rootDrag = null;
let rootDragMoved = false;

function bindRootDrag() {
  const box = $("#rootChips");
  if (!box) return;
  box.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const chip = event.target.closest(".root");
    if (!chip || !box.contains(chip)) return;
    if (!chip.dataset.path) return;                              // 命令方块接管时的 is-cb 芯片不响应
    if (event.target.closest(".x")) return;                       // 移除键单独处理
    // 收起且确有被收起的目录时只点不拖：先展开才能排，避免把隐藏目录挤到末尾
    if (!state.rootsExpanded && rootsTruncated) return;
    rootDrag = { chip, startX: event.clientX, startY: event.clientY, moved: false, pid: event.pointerId };
    try { chip.setPointerCapture(event.pointerId); } catch (_) {}
  });
  box.addEventListener("pointermove", (event) => {
    if (!rootDrag) return;
    const dx = event.clientX - rootDrag.startX;
    const dy = event.clientY - rootDrag.startY;
    if (!rootDrag.moved && Math.hypot(dx, dy) > 5) {
      rootDrag.moved = true;
      rootDrag.chip.classList.add("is-dragging");
      document.body.classList.add("is-root-dragging");
    }
    if (rootDrag.moved) {
      const moreBtn = box.querySelector(".root-more");
      const others = Array.from(box.querySelectorAll(".root")).filter((c) => c !== rootDrag.chip);
      let placed = false;
      for (const c of others) {
        const r = c.getBoundingClientRect();
        if (event.clientX < r.left + r.width / 2) {
          box.insertBefore(rootDrag.chip, c);
          placed = true;
          break;
        }
      }
      if (!placed) {
        if (moreBtn) box.insertBefore(rootDrag.chip, moreBtn);
        else box.appendChild(rootDrag.chip);
      }
    }
  });
  const end = (cancelled) => {
    if (!rootDrag) return;
    const { chip, moved, pid } = rootDrag;
    try { chip.releasePointerCapture(pid); } catch (_) {}
    chip.classList.remove("is-dragging");
    document.body.classList.remove("is-root-dragging");
    if (!cancelled && moved) {
      const order = Array.from(box.querySelectorAll(".root")).map((c) => c.dataset.path);
      state.rootsExpanded = true;            // 拖动成立说明全部都展开了，保持展开
      rootDragMoved = true;                  // 屏蔽紧随其后的 click，别又开一次文件夹
      commitRootOrder(order);
    }
    rootDrag = null;
  };
  box.addEventListener("pointerup", () => end(false));
  box.addEventListener("pointercancel", () => end(true));
  // 点击定位文件夹：用 click 处理，和拖拽判定分开，互不打架
  box.addEventListener("click", (event) => {
    if (rootDragMoved) { rootDragMoved = false; return; }
    const chip = event.target.closest(".root");
    if (!chip || !box.contains(chip)) return;
    if (!chip.dataset.path) return;                              // 命令方块接管时的 is-cb 芯片不响应
    if (event.target.closest(".x")) return;
    openAt(chip.dataset.path, 0);
    const r = state.roots.find((x) => x.path === chip.dataset.path);
    toast("已在访达里定位：" + (r ? r.name : chip.dataset.path.split("/").pop()));
  });
}

async function commitRootOrder(order) {
  const data = await api("/api/roots", { method: "POST", body: JSON.stringify({ action: "reorder", order }) });
  if (data.roots) state.roots = data.roots;
  renderRoots();                            // 用后端返回的权威顺序重排一遍
}

async function addRoot() {
  const picked = await api("/api/pick-folder");
  if (!picked.path) return;
  const data = await api("/api/roots", { method: "POST", body: JSON.stringify({ action: "add", path: picked.path }) });
  if (data.error) { toast(data.error, "bad"); return; }
  state.roots = data.roots || state.roots;
  renderRoots(); renderChips();
  toast("已登记 " + picked.path.split("/").pop());
  poll(0);
}

/* ------------------------------------------------------------------ 输入区 */
function renderInput(payload) {
  const term = (payload && payload.term) || "";
  const input = $("#findInput");
  if (document.activeElement !== input) input.value = term;
  const kindText = KIND_TEXT[(payload && payload.kind) || ""] || "";
  $("#kindBadge").hidden = !kindText;
  $("#kindText").textContent = kindText;

  const en = (payload && payload.trans && payload.trans.en) || "";
  const zh = (payload && payload.trans && payload.trans.zh) || "";
  $("#pairEn").textContent = en || "—";
  $("#pairZh").textContent = zh || "—";
  $(".pair-row").hidden = !(en || zh);
  $("#inputNote").textContent = state.scope === "disk" ? "不看登记目录" : "";
  const scopeBtn = $("#scopeBtn");
  if (scopeBtn) scopeBtn.classList.toggle("is-on", state.scope === "disk");
}

/* ------------------------------------------------------------------ 卡片 */
function blockShell(opts) {
  const folded = state.folded.has(opts.id) || opts.wait;
  // 头上一排小工具：
  //  还在查  → 转轮（加载中） + 关闭（×，把这张卡收起来）
  //  查完了  → 关掉这个源（它会缩成上面一个小标） + 收起
  const tools = `
        <span class="head-tools">
          ${opts.wait ? `<span class="spin"></span>` : ""}
          ${opts.wait
            ? `<button class="svc-close" data-close="${escapeHtml(opts.id)}" title="收起这张卡">
                 <svg class="icon"><use href="#i-close"/></svg></button>`
            : (opts.src ? `<button class="svc-off" data-src-off="${escapeHtml(opts.src)}"
                 title="关掉「${escapeHtml(opts.name)}」，它会缩成上面一个小标"><svg class="icon"><use href="#i-power"/></svg></button>` : "")}
          <svg class="icon chev"><use href="#i-chevron"/></svg>
        </span>`;
  return `
    <section class="block ${opts.extra || ""} ${opts.cls || ""} ${folded ? "is-folded" : ""} ${opts.wait ? "is-wait" : ""}"
      data-block="${opts.id}"${opts.src ? ` data-src="${escapeHtml(opts.src)}"` : ""}>
      <header class="block-head">
        <span class="svc ${opts.svcClass}">${opts.letter || ""}</span>
        <span class="block-name">${escapeHtml(opts.name)}</span>
        ${opts.sub ? `<span class="block-sub">${escapeHtml(opts.sub)}</span>` : ""}
        ${tools}
      </header>
      <div class="fold"><div class="fold-in">
        <div class="block-body">${opts.body || ""}</div>
        ${opts.foot ? `<div class="block-foot">${opts.foot}</div>` : ""}
      </div></div>
    </section>`;
}

function dictBlock(payload) {
  const local = payload && payload.local;
  if (!local) return "";
  const verbosity = state.switches.verbosity;
  const layers = [
    ["① 拆词", local.break],
    ["③ 为什么需要它", local.why],
    ["④ 落在哪", local.where],
  ].filter((row) => row[1]);
  const body = `
    <h2 class="word">${escapeHtml(local.title)}</h2>
    ${local.one ? `<p class="def">${escapeHtml(local.one)}</p>` : ""}
    ${verbosity === "brief" || !layers.length ? "" : `<div class="layers">${layers.map((row) => `
      <div class="layer"><h4>${row[0]}</h4><p>${escapeHtml(row[1])}</p></div>`).join("")}</div>`}`;
  const foot = `
    ${local.file ? `<button class="ico" data-open-dict="1" title="打开词库原文">
      <svg class="icon"><use href="#i-book"/></svg></button>` : ""}
    <button class="ico" data-copy="${escapeHtml(local.title)}" title="复制">
      <svg class="icon"><use href="#i-copy"/></svg></button>`;
  return blockShell({
    id: "dict", name: "本地词库", sub: local.source ? local.source.split("#")[0] : "",
    svcClass: "is-local", letter: "库", body, foot,
  });
}

function filesBlock(hits, terms, payload) {
  if (!hits || !hits.length) return "";
  const fromCb = payload && payload.hitsSource === "commandblock";
  const title = fromCb ? "命令方块" : (state.scope === "disk" ? "整台电脑" : "本地文件");
  const sub = hits.length + " 处" + (fromCb && payload.hitsRoot ? " · " + payload.hitsRoot : "");
  const rows = hits.map((hit) => `
    <div class="hit" data-path="${escapeHtml(hit.path)}" data-line="${hit.line || 0}">
      <div class="hit-top">
        <span class="hit-name">${escapeHtml(hit.name)}</span>
        ${hit.line > 1 ? `<span class="hit-line">${hit.line}</span>` : ""}
        <span class="hit-dir">${escapeHtml(shortDir(hit))}</span>
      </div>
      ${hit.snippet ? `<div class="hit-snippet">${markSnippet(hit.snippet, terms)}</div>` : ""}
      <button class="hit-reveal" data-reveal="${escapeHtml(hit.path)}" title="在访达里显示">
        <svg class="icon"><use href="#i-arrow"/></svg>
      </button>
    </div>`).join("");
  return blockShell({
    id: "files",
    name: title,
    sub,
    svcClass: fromCb ? "is-cb" : "is-files", letter: fromCb ? "块" : "文",
    body: rows,
  });
}

// 一条联网结果 = 一张卡。卡片头上有电源键，点掉这个源，它就缩进状态栏
function webCardHtml(item) {
  const cls = SVC_CLASS[item.source] || "is-wiki";
  const letter = SVC_LETTER[item.source] || "网";
  // 接口报错：只留一个标志，不把报错原文当翻译结果贴出来
  if (item.error) {
    return blockShell({
      id: "web-" + item.source, name: item.source, svcClass: cls, letter,
      body: `<p class="web-err"><span class="err-dot">!</span>${escapeHtml(item.error || "接口异常，已跳过")}</p>`,
      foot: "", src: item.source, extra: "is-web is-bad",
    });
  }
  const body = `
    <p class="web-sum">${escapeHtml(item.summary)}</p>
    ${item.url ? `<a class="web-link" data-url="${escapeHtml(item.url)}">
      <svg class="icon"><use href="#i-link"/></svg>${escapeHtml(shortUrl(item.url))}</a>` : ""}`;
  const foot = `
    <button class="ico" data-copy="${escapeHtml(item.summary)}" title="复制摘要">
      <svg class="icon"><use href="#i-copy"/></svg></button>`;
  return blockShell({
    id: "web-" + item.source, name: item.source, svcClass: cls, letter,
    body, foot, src: item.source, extra: "is-web",
  });
}

// 还在查的源：卡片先占好位，收起来，头上一圈转轮（关掉它也能点）
function waitCardHtml(name) {
  return blockShell({
    id: "wait-" + name, name, src: name, wait: true, extra: "is-web",
    svcClass: SVC_CLASS[name] || "is-wiki", letter: SVC_LETTER[name] || "网",
    body: "",
  });
}

function shortUrl(url) {
  try {
    const parsed = new URL(url);
    const path = decodeURIComponent(parsed.pathname).slice(0, 26);
    return parsed.hostname.replace("www.", "") + path;
  } catch (_) { return "打开原文"; }
}

function shortDir(hit) {
  if (hit.root === "disk") return hit.dir.replace(/^\/Users\/[^/]+/, "~");
  const root = state.roots.find((r) => r.path === hit.root);
  const name = root ? root.name : "…";
  const inside = root ? (hit.dir || "").slice(root.path.length).replace(/^\//, "") : "";
  return inside ? name + "/" + inside : name;
}

/* ------------------------------------------------------------------ 组装 */
function renderLocal() {
  const payload = state.payload || { term: "", hits: [] };
  renderInput(payload);
  const hits = state.hideFiles ? [] : (payload.hits || []);
  const verbosity = state.switches.verbosity;
  const html = [
    verbosity === "off" ? "" : dictBlock(payload),
    filesBlock(hits, payload.terms || [payload.term], payload),
  ].join("");
  $("#blocks").innerHTML = html;
  bindBlocks($("#blocks"));
  if (state.renderedTerm !== payload.term) {
    state.renderedTerm = payload.term;
    $("#scroll").scrollTop = 0;
  }
  updateEmpty();
  renderChips();
}

function updateEmpty() {
  const has = !!($("#blocks").textContent.trim() || $("#webCards").textContent.trim());
  $("#emptyHint").hidden = has;
  if (has) return;
  $("#emptyHint").innerHTML = state.term
    ? `<p>这个词没有本地记录，也没抓到网页。</p>`
    : `<p>在别处复制一个词，这里会自己跟上。</p>`;
}

// 没词也没卡（且根本没有在查的词）时，把整块卡片区收起：
// 面板直接缩到只剩输入框，下面不留下半屏空白
function updateScrollEmpty() {
  const scroll = $("#scroll");
  if (!scroll) return;
  const hasCards = $("#blocks").children.length || $("#webCards").children.length;
  scroll.classList.toggle("is-empty", !hasCards && !state.term);
}

// 卡片里的点击都在这儿挂：新插进来的联网卡也能用同一套
function bindBlocks(root) {
  if (!root) return;
  root.querySelectorAll(".block-head").forEach((head) => {
    head.addEventListener("click", () => {
      const block = head.closest(".block");
      if (!block) return;
      const id = block.dataset.block;
      if (state.folded.has(id)) state.folded.delete(id); else state.folded.add(id);
      block.classList.toggle("is-folded");
    });
  });
  root.querySelectorAll("[data-open-dict]").forEach((btn) => btn.addEventListener("click", (event) => {
    event.stopPropagation();
    const local = state.payload && state.payload.local;
    if (local && local.file) openAt(local.file, local.line || 0);
  }));
  root.querySelectorAll("[data-src-off]").forEach((btn) => btn.addEventListener("click", (event) => {
    event.stopPropagation();          // 别顺带把卡片折叠了
    powerOffSource(btn.dataset.srcOff, btn);
  }));
  root.querySelectorAll("[data-close]").forEach((btn) => btn.addEventListener("click", (event) => {
    event.stopPropagation();
    const block = btn.closest(".block");
    if (block) block.classList.toggle("is-folded");
  }));
  root.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async (event) => {
    event.stopPropagation();
    const text = btn.dataset.copy;
    const data = await api("/api/copy", { method: "POST", body: JSON.stringify({ text }) });
    toast(data.ok ? "已复制" : (data.error || "复制失败"), data.ok ? "" : "bad");
  }));
  root.querySelectorAll("[data-url]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    openUrl(link.dataset.url);
  }));
  root.querySelectorAll(".hit").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("[data-reveal]")) return;
    openAt(row.dataset.path, Number(row.dataset.line) || 0);
  }));
  root.querySelectorAll("[data-reveal]").forEach((btn) => btn.addEventListener("click", (event) => {
    event.stopPropagation();
    reveal(btn.dataset.reveal);
  }));
}

function render() {
  renderLocal();
  if (window.__scrollUpdate) requestAnimationFrame(window.__scrollUpdate);
  document.documentElement.scrollLeft = 0;
  document.body.scrollLeft = 0;
  updateEmpty();
  updateScrollEmpty();
}

/* ---------------------------------------------- 联网卡片：一张张横着进来 */
function clearWebCards() {
  state.web = [];
  state.webPending = [];
  state.webSince = 0;
  state.webDone = true;
  const box = $("#webCards");
  if (box) box.innerHTML = "";
  updateScrollEmpty();
}

// 只收掉某个分类总闸下的卡（关联网不误伤娱乐，反之亦然）
function dropGroupCards(group) {
  const names = new Set(Object.keys(state.sources || {}).filter((n) => groupOf(n) === group));
  state.web = (state.web || []).filter((item) => !names.has(item.source));
  state.webPending = (state.webPending || []).filter((n) => !names.has(n));
  const box = $("#webCards");
  if (box) box.querySelectorAll("[data-src]").forEach((card) => {
    if (names.has(card.dataset.src)) card.remove();
  });
  updateEmpty();
  updateScrollEmpty();
}

function waitCards(names) {
  const box = $("#webCards");
  if (!box) return;
  box.innerHTML = (names || []).map(waitCardHtml).join("");
  bindBlocks(box);          // 还在转圈的卡也能点「收起 / 关掉这个源」
  updateScrollEmpty();
}

// 一条结果到了：把同名占位卡换掉，插到最上层（最新的排最上面）
function showWebItem(item, index) {
  const box = $("#webCards");
  if (!box) return;
  const name = item.source;
  box.querySelectorAll(`[data-src="${cssEscape(name)}"]`).forEach((old) => old.remove());
  const holder = document.createElement("div");
  holder.innerHTML = webCardHtml(item);
  const card = holder.firstElementChild;
  card.style.setProperty("--d", Math.min(index, 5) * 45 + "ms");
  // 不再按先来后到往下堆：新来的直接排到列表最上层
  box.insertBefore(card, box.firstElementChild);
  bindBlocks(card);
  updateEmpty();
  updateScrollEmpty();
  jumpToTop(card);
}

// 新内容跳到最上层：把刚到的这张卡顶到可视区最上面（不是滚到底，也不是不动）
function jumpToTop(card) {
  const sc = $("#scroll");
  if (!sc || !card) return;
  try {
    card.scrollIntoView({ block: "start", inline: "nearest", behavior: "smooth" });
  } catch (_) {
    try { card.scrollIntoView(true); } catch (_) { }
  }
}

// 等不到结果的源：占位卡撤掉，别一直转圈骗她
function syncWaitCards(pending) {
  state.webPending = pending || [];
  const box = $("#webCards");
  if (!box) return;
  box.querySelectorAll(".is-wait").forEach((card) => {
    if (!state.webPending.includes(card.dataset.src)) card.remove();
  });
}

/* ---------------------------------------------- 状态栏小标（关闭的服务） */
function closedMarks() {
  const out = [];
  // 只把「单个源」关掉时缩成小标；分类总闸关掉不再额外生成小标。
  // 源归哪个总闸由后端下发的 groups 决定：联网总闸开着才列 web 组，娱乐总闸开着才列 fun 组。
  Object.keys(state.sources || {}).forEach((name) => {
    if (state.sources[name]) return;
    const gate = groupOf(name);
    if (!state.switches[gate]) return;
    out.push({ key: "src:" + name, letter: SVC_LETTER[name] || "网", name,
               color: SVC_COLOR[name] || "#8e8e93", svc: name });
  });
  return out;
}

function renderMarks(opts) {
  const box = $("#srcTags");
  if (!box) return;
  const peek = (opts && opts.peek) || "";
  const before = box.children.length;
  box.innerHTML = closedMarks().map((mark) => {
    const hidden = mark.key === "src:" + peek || mark.key === peek;
    const inner = mark.icon
      ? `<svg class="icon"><use href="${mark.icon}"/></svg>`
      : escapeHtml(mark.letter);
    return `<button class="src-tag" type="button" data-mark="${escapeHtml(mark.key)}"
      style="--c:${mark.color}${hidden ? ";visibility:hidden" : ""}"
      title="${escapeHtml(mark.name)} 已关，点一下打开">${inner}</button>`;
  }).join("");
  box.querySelectorAll("[data-mark]").forEach((btn) => btn.addEventListener("click", (event) => {
    event.stopPropagation();
    openMark(btn.dataset.mark, btn);
  }));
  // 只有真的多了小标才把滑轨推到末尾，别每 6 秒把她的视野拽回去
  const rail = box.closest(".svcrow");
  if (rail && box.children.length > before) {
    requestAnimationFrame(() => { rail.scrollLeft = rail.scrollWidth; });
  }
}

async function openMark(key, btn) {
  btn.classList.add("is-out");
  if (key === "web" || key === "manage" || key === "fun") {
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key, value: true }) });
    state.switches = data.switches || state.switches;
    renderSwitches();
    toast(TOG_MARK[key].name + "已开");
    if (key === "manage") { poll(0); lookup(state.term); }
    else lookup(state.term);
    return;
  }
  if (key.startsWith("src:")) toggleSource(key.slice(4), true);
}

/* ---------------------------------------------- 抛物线：关掉的图标飞进状态栏 */
function flyToMark(from, to, mark, onLand) {
  const size = 20;
  const sx = from.left + from.width / 2;
  const sy = from.top + from.height / 2;
  const ex = to.left + to.width / 2;
  const ey = to.top + to.height / 2;
  const node = document.createElement("span");
  node.className = "flier";
  node.style.width = size + "px";
  node.style.height = size + "px";
  node.style.background = mark.color || "#8e8e93";
  node.style.fontSize = "10px";
  if (mark.icon) {
    node.innerHTML = `<svg class="icon" style="width:11px;height:11px"><use href="${mark.icon}"/></svg>`;
  } else {
    node.textContent = mark.letter || "";
  }
  document.body.appendChild(node);

  // 二次贝塞尔：控制点抬到起点上方，先抛起来再落进状态栏
  const rise = Math.max(46, Math.min(96, Math.abs(ex - sx) * 0.34 + 34));
  const cx = sx + (ex - sx) * 0.46;
  const cy = Math.min(sy, ey) - rise;
  const frames = [];
  const steps = 18;
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    const u = 1 - t;
    const x = u * u * sx + 2 * u * t * cx + t * t * ex;
    const y = u * u * sy + 2 * u * t * cy + t * t * ey;
    const scale = 1 - 0.36 * t;
    frames.push({
      offset: t,
      transform: "translate(" + (x - size / 2) + "px," + (y - size / 2) + "px) scale(" + scale + ")",
      opacity: t > 0.86 ? 0.65 : 1,
    });
  }
  const anim = node.animate(frames, { duration: 470, easing: "linear", fill: "forwards" });
  anim.onfinish = () => {
    node.remove();
    if (onLand) onLand();
  };
}

// 点卡片头上的电源：关掉这个源，图标飞进状态栏，落地弹一下
async function powerOffSource(name, btn) {
  if (!name) return;
  const from = btn.getBoundingClientRect();
  const data = await api("/api/source", { method: "POST", body: JSON.stringify({ name, value: false }) });
  if (data.error) { toast(data.error, "bad"); return; }
  state.sources = data.sources || state.sources;

  renderMarks({ peek: name });
  const mark = $(`#srcTags [data-mark="src:${cssEscape(name)}"]`);
  const markMeta = { letter: SVC_LETTER[name] || "网", color: SVC_COLOR[name] || "#8e8e93" };

  // 卡片先收起来，眼睛才会跟着图标走
  const card = btn.closest(".block");
  if (card) {
    card.style.height = card.offsetHeight + "px";
    card.style.overflow = "hidden";
    requestAnimationFrame(() => {
      card.style.transition = "height .24s cubic-bezier(.32,.72,0,1), opacity .2s, margin-bottom .24s";
      card.style.height = "0px";
      card.style.opacity = "0";
      card.style.marginBottom = "0px";
    });
    setTimeout(() => card.remove(), 260);
  }

  if (mark) {
    const to = mark.getBoundingClientRect();
    flyToMark(from, to, markMeta, () => {
      mark.style.visibility = "";
      mark.classList.remove("is-pop");
      void mark.offsetWidth;
      mark.classList.add("is-pop");
    });
  }
  state.web = (state.web || []).filter((item) => item.source !== name);
  setTimeout(() => { renderSwitches(); renderMarks(); }, 480);
  toast("已关掉 " + name);
}

function cssEscape(text) {
  return String(text == null ? "" : text).replace(/["\\]/g, "\\$&");
}

/* ------------------------------------------------------------------ 查询 */
async function lookup(term, options) {
  const opts = options || {};
  const verbosity = state.switches.verbosity;
  // 三个分类总闸平级：任意一个开着且还有属于它的源，就继续联网
  const wantWeb = verbosity !== "off"
    && Object.keys(state.sources || {}).some((n) => state.sources[n] && state.switches[groupOf(n)]);
  state.webToken += 1;                 // 作废还在跑的上一次
  if (!term) {
    state.term = ""; state.payload = null; state.hideFiles = false;
    state.raisedTerm = "";
    clearWebCards();
    render();
    return;
  }
  state.term = term;
  state.hideFiles = false;
  // 输入框换词就立刻把面板抬到最上层，不等联网结果：
  // 划词 / 剪贴板触发同样走这里；多数中文词联网 0 条，等结果就永远不触发。
  if (state.raisedTerm !== term) {
    state.raisedTerm = term;
    bringToFront();
  }
  clearWebCards();
  const seq = ++state.reqId;
  const query = encodeURIComponent(term);
  if (state.scope === "disk") {
    // 全盘：词义照旧本地出，文件命中改由系统索引来找
    const [data, disk] = await Promise.all([
      api("/api/lookup?q=" + query),
      api("/api/search?q=" + query + "&scope=disk"),
    ]);
    if (seq !== state.reqId) return;
    if (data.error) { toast(data.error, "bad"); return; }
    data.hits = disk.hits || [];
    state.payload = data;
    render();
    if (wantWeb) streamWeb(term);
    return;
  }
  const data = await api("/api/lookup?q=" + query);
  if (seq !== state.reqId) return;
  if (data.error) { toast(data.error, "bad"); return; }
  state.payload = data;
  if (!state.payload || opts.skeleton) state.hideFiles = false;
  render();
  if (wantWeb) streamWeb(term);
}

// 联网：先把每个源摆成一张收起来的小卡转圈，回来一条就换一条
async function streamWeb(term) {
  const token = ++state.webToken;
  const enabled = Object.keys(state.sources || {}).filter(
    (name) => state.sources[name] && state.switches[groupOf(name)]);
  state.web = [];
  state.webSince = 0;
  state.webDone = false;
  state.webFocused = false;     // 本轮流第一次出现内容时把面板顶到最上层
  state.webPending = enabled.slice();
  waitCards(enabled);
  updateEmpty();

  let data;
  try {
    data = await api("/api/web/start", { method: "POST", body: JSON.stringify({ term }) });
  } catch (_) {
    return;
  }
  if (token !== state.webToken) return;
  if (data.error) { syncWaitCards([]); return; }
  absorbWeb(data, token);
  if (data.done) { finishWeb(data); return; }
  pumpWeb(token, term);
}

function absorbWeb(data, token) {
  if (!data || token !== state.webToken) return;
  let added = 0;
  (data.items || []).forEach((item) => {
    if (state.web.some((old) => old.source === item.source)) return;
    state.web.push(item);
    showWebItem(item, state.web.length - 1);
    added++;
  });
  // 本轮流第一次冒出新内容，把面板顶到最上层（只触发一次，不反复打扰）
  if (added && !state.webFocused) { state.webFocused = true; bringToFront(); }
  if (typeof data.since === "number") state.webSince = data.since;
  if (data.pending && data.pending.length !== state.webPending.length) syncWaitCards(data.pending);
}

async function pumpWeb(token, term) {
  while (token === state.webToken && !state.webDone) {
    await new Promise((resolve) => setTimeout(resolve, 170));
    if (token !== state.webToken) return;
    const data = await api("/api/web/poll?q=" + encodeURIComponent(term) + "&since=" + state.webSince);
    if (token !== state.webToken) return;
    if (data.error) return;
    absorbWeb(data, token);
    if (data.done) { finishWeb(data); return; }
  }
}

function finishWeb(data) {
  state.webDone = true;
  syncWaitCards([]);
  if (data && data.trans && state.payload) {
    const trans = state.payload.trans || {};
    if (!trans.zh && !trans.en) {
      state.payload.trans = data.trans;
      renderInput(state.payload);
    }
  }
  updateEmpty();
}

/* ------------------------------------------------------------------ 动作 */
async function openAt(path, line) {
  const data = await api("/api/open", { method: "POST", body: JSON.stringify({ path, line }) });
  if (data.error) { toast(data.error, "bad"); return; }
  if (data.result && data.result.how === "commandblock") toast("已跳到命令方块第 " + line + " 行");
}

async function reveal(path) {
  const data = await api("/api/open", { method: "POST", body: JSON.stringify({ path, reveal: true }) });
  if (data.error) toast(data.error, "bad");
}

async function openUrl(url) {
  const data = await api("/api/open", { method: "POST", body: JSON.stringify({ url }) });
  if (data.error) toast(data.error, "bad");
}

function nativeCall(payload) {
  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.native) {
    window.webkit.messageHandlers.native.postMessage(payload);
    return true;
  }
  return false;
}

// 输入框换词 / 新增搜索内容时把面板抬到最上层（原生 focus 分支 + 一次看得见的浮起）
function bringToFront() {
  nativeCall({ cmd: "focus", term: state.term || "" });
  // 原生把窗口层级临时抬到浮动层（3 秒后按 pin 状态还原）；
  // 这里再补一次可见的浮起高亮。不抢键盘、不激活 app。
  const body = document.body;
  body.classList.remove("is-jump");
  void body.offsetWidth;                 // 强制重排：连续两轮也能重播动画
  body.classList.add("is-jump");
  setTimeout(() => body.classList.remove("is-jump"), 560);
}

/* ------------------------------------------------------------------ 查梗 · 娱乐网站 */
// 内联查（站点 API 命中才出结果）是加分项，查不到就退回到下面的目录，
// 点开网站照样能查——所以「能连上并查到」这一条，靠目录就能兜底。
function memeOpen() {
  if (!state.switches.fun) { toast("娱乐已关，点顶栏「娱」打开", "bad"); return; }
  const panel = $("#memePanel");
  if (!panel) return;
  panel.hidden = false;
  const term = state.term || $("#findInput").value.trim();
  if (term) $("#memeInput").value = term;
  // 先用上次的目录渲染（不闪「没加载」），再拉一次最新的：
  // 站点是在虚幻客户端里增删的，客户端保存会同步到这份清单，打开面板就该看到新的
  if (state.memeSites) memeRenderDir();
  memeLoadSites();
  setTimeout(() => $("#memeInput").focus(), 30);
}

function memeClose() {
  const panel = $("#memePanel");
  if (panel) panel.hidden = true;
}

async function memeLoadSites() {
  const data = await api("/api/meme-sites");
  if (data.off) { state.memeSites = []; memeRenderDir(); return; }
  state.memeSites = Array.isArray(data.sites) ? data.sites : [];
  memeRenderDir();
}

function memeRenderDir() {
  const box = $("#memeDir");
  if (!box) return;
  if (!state.switches.fun) {
    box.innerHTML = '<div class="meme-dir-empty">娱乐已关，点顶栏「娱」打开。</div>';
    return;
  }
  const sites = state.memeSites || [];
  if (!sites.length) {
    box.innerHTML = '<div class="meme-dir-empty">目录还没加载出来（服务可能没开）。</div>';
    return;
  }
  const REL = { alive: "已验证", doubtful: "接口失效", dead: "已停用" };
  const term = $("#memeInput").value.trim();
  box.innerHTML = sites.map((s) => {
    const rel = s.reliability || "doubtful";
    const name = escapeHtml(s.name || s.domain || "");
    const note = escapeHtml(s.note || "");
    const goUrl = (s.search && term) ? s.search.replace("{q}", encodeURIComponent(term)) : (s.url || "");
    const homeUrl = s.url || "";
    const goLabel = (s.search && term) ? "搜一下" : "去网站";
    const showHome = homeUrl && homeUrl !== goUrl;
    return `
      <div class="meme-site">
        <div class="ms-main">
          <div class="ms-name">${name}<span class="ms-badge ${rel}">${REL[rel] || rel}</span></div>
          ${note ? `<div class="ms-note">${note}</div>` : ""}
        </div>
        <div class="ms-actions">
          ${goUrl ? `<button class="ms-btn go" data-url="${escapeHtml(goUrl)}">${goLabel}</button>` : ""}
          ${showHome ? `<button class="ms-btn" data-url="${escapeHtml(homeUrl)}">官网</button>` : ""}
        </div>
      </div>`;
  }).join("");
  box.querySelectorAll(".ms-btn").forEach((b) => {
    b.addEventListener("click", () => openUrl(b.dataset.url));
  });
}

async function memeGo() {
  const term = $("#memeInput").value.trim();
  const result = $("#memeResult");
  if (!state.switches.fun) { result.innerHTML = '<div class="mr-empty">娱乐已关，点顶栏「娱」打开。</div>'; return; }
  if (!term) { result.innerHTML = '<div class="mr-empty">先输入梗名，或粘一句带梗的话。</div>'; return; }
  result.innerHTML = '<div class="mr-empty">正在查…</div>';
  memeRenderDir();   // 让目录里的「搜一下」跟当前词对齐
  try {
    const data = await api("/api/meme?q=" + encodeURIComponent(term));
    const res = data && data.result;
    if (res && res.title) {
      const link = res.url ? ` <a href="#" data-url="${escapeHtml(res.url)}" style="color:var(--blue)">查看原文 ↗</a>` : "";
      result.innerHTML =
        `<div class="mr-src">${escapeHtml(res.source || "查梗")}</div>` +
        `<div class="mr-title">${escapeHtml(res.title)}</div>` +
        (res.summary ? `<div>${escapeHtml(res.summary)}</div>` : "") + link;
      const a = result.querySelector("a[data-url]");
      if (a) a.addEventListener("click", (e) => { e.preventDefault(); openUrl(a.dataset.url); });
    } else {
      result.innerHTML = '<div class="mr-empty">站点接口暂时没查到（有的站会限流/抽风）。下面目录里点开网站照样能查。</div>';
    }
  } catch (err) {
    result.innerHTML = '<div class="mr-empty">连不上查梗服务（可能没开）。下面目录里点开网站照样能查。</div>';
  }
}

/* ------------------------------------------------------------------ 状态 */
// 总闸三态：开=无色，全关=黄，只关了其中几个=淡黄提醒
function setTog(sel, on, name, partial) {
  const node = $(sel);
  if (!node) return;
  node.classList.toggle("is-on", on && !partial);
  node.classList.toggle("is-part", !!on && !!partial);
  node.classList.toggle("is-off", !on);
  node.title = !on
    ? name + "已关，点一下打开"
    : (partial ? name + "开着，但有几个源被关了；点一下全关" : name + "开着，点一下关掉");
}

function renderSwitches() {
  const sw = state.switches;
  // 关 = 收起全部 · 开 = 展开全部 · 简 = 词义精简（中英对照＋一句话）
  state.allMode = sw.verbosity === "off" ? "collapse" : (sw.verbosity === "full" ? "expand" : null);
  setTog("#webToggle", !!sw.web, "联网", anySourceOff("web"));
  setTog("#manageToggle", !!sw.manage, "管理", false);
  setTog("#funToggle", !!sw.fun, "娱乐", anySourceOff("fun"));
  $("#pinBtn").classList.toggle("is-on", !!sw.pin);
  $("#dockBtn").classList.toggle("is-on", !!sw.docked);
  $("#cornerBtn").hidden = !sw.docked;
  $$("#verbSeg button").forEach((btn) => btn.classList.toggle("is-on", btn.dataset.v === sw.verbosity));
  document.body.classList.toggle("is-brief", sw.verbosity === "brief");
  document.body.classList.toggle("all-expand", state.allMode === "expand");
  document.body.classList.toggle("all-collapse", state.allMode === "collapse");
  syncDock();
  syncPin();
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function toggleSource(name, on) {
  const data = await api("/api/source", { method: "POST", body: JSON.stringify({ name, value: on }) });
  if (data.error) { toast(data.error, "bad"); return; }
  state.sources = data.sources || state.sources;
  if (on) {
    const mark = $(`#srcTags [data-mark="src:${cssEscape(name)}"]`);
    if (mark) { mark.classList.add("is-out"); await wait(130); }
  }
  renderMarks();
  renderSwitches();
  if (on) {
    toast(name + " 已开回来");
    if (state.switches[groupOf(name)] && state.term) streamWeb(state.term);
  } else {
    state.web = (state.web || []).filter((item) => item.source !== name);
    toast("已关掉 " + name);
  }
}

// 一键把某个分类总闸下的源全打开 / 全关（不串到别的分类）
async function setAllSources(on, group) {
  const names = Object.keys(state.sources || {}).filter(
    (name) => groupOf(name) === group && !!state.sources[name] !== on);
  for (const name of names) {
    const data = await api("/api/source", { method: "POST", body: JSON.stringify({ name, value: on }) });
    if (data.sources) state.sources = data.sources;
  }
  renderMarks();
  renderSwitches();
}

// 窗口大小由原生管：这里只在「该不该是吸附状态」变了的时候说一次
function syncDock() {
  const want = !!state.switches.docked;
  if (state.dockedApplied === want) return;
  state.dockedApplied = want;
  nativeCall({ cmd: "dock", on: want, corner: state.switches.corner || "br" });
  // 从吸附状态退出来，重新按内容高矮把窗口量回去
  if (!want) setTimeout(reportHeight, 320);
}

// 让原生把窗口改到跟内容一样高：内容少就短，内容多就长，不留空位
let lastReportedH = 0;
function reportHeight() {
  if (state.switches && state.switches.docked) return;
  const panel = $(".panel");
  if (!panel) return;
  const height = Math.ceil(panel.getBoundingClientRect().height);
  if (!height || Math.abs(height - lastReportedH) < 2) return;
  lastReportedH = height;
  nativeCall({ cmd: "size", height });
}

// 置顶也要跟上次关软件时一致，不然重开永远是「置顶」
function syncPin() {
  const want = !!state.switches.pin;
  if (state.pinApplied === want) return;
  state.pinApplied = want;
  nativeCall({ cmd: "pin", on: want });
}

// 分段控件（关/开/简）直接给每个按钮上色，不再用滑动滑块

function renderStat() {
  const index = state.index || {};
  const node = $("#statText");
  if (!node) return;
  if (!state.switches.manage) { node.textContent = "管理已关"; return; }
  if (index.building) {
    const progress = index.progress || {};
    node.textContent = "索引中 " + (progress.done || 0) + "/" + (progress.total || 0);
    return;
  }
  const files = index.files || 0;
  const when = index.lastBuildAt ? ago(index.lastBuildAt) : "未建";
  node.textContent = state.switches.docked ? files + " · " + when : files + " 文件 · " + when;
}

async function poll(force) {
  const data = await api("/api/state");
  if (data.error) return;
  state.roots = data.roots || [];
  state.switches = data.switches || state.switches;
  state.sources = data.sources || state.sources;
  state.groups = data.groups || state.groups;
  state.index = data.index || state.index;
  state.changes = data.changes || [];
  state.commands = data.commands || state.commands;
  // 第一次连上：范围用后端算的（登记过目录就只搜登记的，没有就搜整台电脑）
  if (!state.scopeInit && data.scope) { state.scope = data.scope; state.scopeInit = true; }
  renderRoots(); renderChips(); renderChanges(); renderSwitches(); renderMarks(); renderStat();
  if (force && !$("#findInput").value) {
    const term = (data.clip && data.clip.term) || "";
    if (term) { $("#findInput").value = term; lookup(term); }
  }
}

/* ------------------------------------------------------------------ 事件 */
function on(sel, event, handler) {
  const node = $(sel);
  if (node) node.addEventListener(event, handler);
}

function wire() {
  on("#axGo", "click", () => {
    nativeCall({ cmd: "open", url: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" });
    toast("在列表里把 search 勾上，回来就能划词");
  });
  on("#axNo", "click", () => {
    sessionStorage.setItem("axHintOff", "1");
    $("#axHint").hidden = true;
  });
  on("#addRootBtn", "click", addRoot);

  on("#changedChip", "click", async () => {
    const panel = $("#changesPanel");
    if (panel.dataset.open === "1" && panel.dataset.mode === "changed") {
      panel.dataset.open = "0";
      state.changedSnapshot = null;
      renderChanges();
      return;
    }
    // 先拍下这次要给她看的名单，再复位计数——列表照样能点、能跳
    state.changedSnapshot = state.changes.slice();
    panel.dataset.mode = "changed";
    panel.dataset.open = "1";
    renderChanges();
    await api("/api/ack", { method: "POST", body: JSON.stringify({ all: true }) });
    poll(0);
  });
  // 点「正常」= 这批文件都看过了，把下面的文件卡一次收掉，回到干净状态
  on("#okChip", "click", () => {
    if (!(state.payload && (state.payload.hits || []).length)) {
      toast("登记目录里的文件没有变化");
      return;
    }
    state.hideFiles = true;      // 只藏起来，别把数据扔了：再查一次还能出来
    render();
    toast("文件已收起");
  });
  on("#newChip", "click", () => {
    const panel = $("#changesPanel");
    const isOpen = panel.dataset.open === "1" && panel.dataset.mode === "new";
    panel.dataset.mode = "new";
    panel.dataset.open = isOpen ? "0" : "1";
    renderChanges();
  });

  on("#pinBtn", "click", async () => {
    const next = !state.switches.pin;
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "pin", value: next }) });
    state.switches = data.switches || state.switches;
    renderSwitches();
    nativeCall({ cmd: "pin", on: next });
  });

  on("#closeBtn", "click", () => { if (!nativeCall({ cmd: "hide" })) window.close(); });

  // 一键重启：后端拉起同端口的新进程接管、旧进程退场；前端侦测到「先断后通」就刷新，
  // 这样后端新代码、前端新页面一起生效。两次重启之间用 restarting 拦一下，别连点。
  let restarting = false;
  on("#restartBtn", "click", async () => {
    if (restarting) return;
    restarting = true;
    toast("重启中…");
    await api("/api/restart", { method: "POST", body: "{}" }).catch(() => {});
    let down = false;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      try {
        const r = await fetch("/api/health");
        if (r.ok) { if (down) { location.reload(); return; } }
        else down = true;
      } catch (_) { down = true; }
      await wait(350);
    }
    location.reload();           // 兜底：到时间还没自动刷新就强刷一次
  });

  on("#refreshBtn", "click", () => lookup($("#findInput").value.trim() || state.term));
  on("#clearBtn", "click", () => {
    $("#findInput").value = ""; $("#findInput").focus(); lookup("");
  });
  on("#copyBtn", "click", async () => {
    const text = $("#findInput").value.trim() || state.term;
    if (!text) return;
    const data = await api("/api/copy", { method: "POST", body: JSON.stringify({ text }) });
    toast(data.ok ? "已复制「" + text + "」" : "复制失败", data.ok ? "" : "bad");
  });

  on("#dockBtn", "click", async () => {
    const next = !state.switches.docked;
    if (next && !(state.commands && state.commands.running)) {
      toast("命令方块没开，先打开它");
      return;
    }
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "docked", value: next }) });
    state.switches = data.switches || state.switches;
    renderSwitches(); renderRoots(); poll(0);
    toast(next ? "接上命令方块：文件改读它里面的" : "断开命令方块");
    lookup(state.term);
  });

  on("#cornerBtn", "click", async () => {
    const next = state.switches.corner === "bl" ? "br" : "bl";
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "corner", value: next }) });
    state.switches = data.switches || state.switches;
    state.dockedApplied = true;
    nativeCall({ cmd: "dock", on: true, corner: next });
    toast(next === "bl" ? "挪到左下角" : "挪到右下角");
  });

  on("#easyBtn", "click", () => {
    const term = state.term || $("#findInput").value.trim();
    if (!term) return;
    const url = "easydict://query?text=" + encodeURIComponent(term);
    if (!nativeCall({ cmd: "open", url })) window.location.href = url;
  });
  on("#webBtn", "click", () => {
    const term = state.term || $("#findInput").value.trim();
    if (!term) return;
    openUrl("https://www.baidu.com/s?wd=" + encodeURIComponent(term));
  });

  // 查梗 · 娱乐网站：打开面板；面板里再走内联查 + 站点目录两条路
  on("#memeBtn", "click", () => memeOpen());
  on("#memeGo", "click", () => memeGo());
  on("#memePanel", "click", (e) => { if (e.target.id === "memePanel") memeClose(); });
  on('[data-act="meme-close"]', "click", () => memeClose());
  on("#memeInput", "keydown", (e) => { if (e.key === "Enter") memeGo(); });
  on("#memeInput", "input", () => memeRenderDir());

  on("#scopeBtn", "click", async () => {
    const next = state.scope === "disk" ? "roots" : "disk";
    if (next === "roots" && !state.roots.length) { toast("还没登记文件夹，先点右边的 + 加一个", "bad"); return; }
    state.scope = next;
    // 记下来：下次打开还是这个范围
    api("/api/switch", { method: "POST", body: JSON.stringify({ key: "scope", value: next }) });
    toast(next === "disk" ? "改搜整台电脑" : "只搜登记的文件夹");
    lookup(state.term || $("#findInput").value.trim());
  });

  on("#webToggle", "click", async () => {
    // 只要还没「彻底开着」（总闸开 + 没源被关），点一下就是全部打开；
    // 彻底开着的时候点，才是一次全关。
    const fullyOn = !!state.switches.web && !anySourceOff("web");
    const next = !fullyOn;
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "web", value: next }) });
    state.switches = data.switches || state.switches;
    if (next) await setAllSources(true, "web"); else renderMarks();
    renderSwitches();
    if (next) {
      toast("联网已全开");
      lookup(state.term);
    } else {
      // 关掉 = 联网这组停：把属于联网的卡收掉，娱乐组的卡留着
      dropGroupCards("web");
      toast("联网已关（娱乐不受影响）");
    }
  });

  on("#manageToggle", "click", async () => {
    const next = !state.switches.manage;
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "manage", value: next }) });
    state.switches = data.switches || state.switches;
    renderSwitches();
    renderStat();
    toast(next ? "会翻登记目录里的文件" : "管理已关，不再扫登记目录");
    poll(0);
    lookup(state.term);
  });

  on("#funToggle", "click", async () => {
    // 和联网总闸一样：彻底开着时点一下全关，其余情况点一下全开
    const fullyOn = !!state.switches.fun && !anySourceOff("fun");
    const next = !fullyOn;
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "fun", value: next }) });
    state.switches = data.switches || state.switches;
    if (next) await setAllSources(true, "fun"); else renderMarks();
    renderSwitches();
    if (next) {
      toast("娱乐已全开");
      lookup(state.term);
    } else {
      dropGroupCards("fun");
      memeClose();
      toast("娱乐已关（萌娘百科＋查梗都停）");
    }
  });

  $$("#verbSeg button").forEach((btn) => btn.addEventListener("click", async () => {
    const data = await api("/api/switch", { method: "POST", body: JSON.stringify({ key: "verbosity", value: btn.dataset.v }) });
    state.switches = data.switches || state.switches;
    renderSwitches();
    // 关/开/简 都只是显示状态切换（收起全部 / 展开全部 / 词义精简），不重查、不丢数据
  }));

  const input = $("#findInput");
  input.addEventListener("input", () => {
    clearTimeout(state.typing);
    const value = input.value.trim();
    state.typing = setTimeout(() => lookup(value), 260);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { clearTimeout(state.typing); lookup(input.value.trim()); }
    if (event.key === "Escape") { input.blur(); }
  });
  input.addEventListener("focus", () => input.select());

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
      event.preventDefault(); input.focus(); input.select();
    }
    if (event.key === "Escape") { if (document.activeElement === input) input.blur(); else nativeCall({ cmd: "hide" }); }
  });

  // 状态条和服务行都能一直往右排，排不下就横着滑：滚轮也让它横着走
  [".pills", ".svcrow"].forEach((sel) => {
    const rail = $(sel);
    if (!rail) return;
    rail.addEventListener("wheel", (event) => {
      if (!event.deltaY && !event.deltaX) return;
      if (rail.scrollWidth <= rail.clientWidth) return;
      event.preventDefault();
      rail.scrollLeft += event.deltaY + event.deltaX;
    }, { passive: false });
  });

  // 窗口高度跟着内容走：内容一变就告诉原生，它把窗口改长改短
  const panel = $(".panel");
  if (panel && window.ResizeObserver) {
    const ro = new ResizeObserver(() => reportHeight());
    ro.observe(panel);
  }
  reportHeight();

  // 空白处按下就能拖窗口（按钮、文件夹、命中行上不算）
  bindDrag(".topbar");
  bindDrag(".pair-row");
  bindDrag(".roots");
  bindDrag(".foot");

  // 文件栏：拖拽换顺序 / 点击定位文件夹（挂在容器上，重渲染不失效）
  bindRootDrag();
}

function bindDrag(sel) {
  const node = document.querySelector(sel);
  if (!node) return;
  node.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    if (event.target.closest("button, a, input, .root, .hit, .web-link")) return;
    nativeCall({ cmd: "drag" });
  });
}

/* ------------------------------------------------------------------ SSE */
function connect() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.type === "clip") {
      // 只有「面板在前台、且她正在输入框打字」才跳过。
      // 面板在后台时 activeElement 仍是输入框，旧的判断会把剪贴板/划词全部拦掉。
      if (document.hasFocus() && document.activeElement === $("#findInput")) return;
      state.term = data.term;
      $("#findInput").value = data.term;
      lookup(data.term);
      return;
    }
    if (data.type === "index") {
      if (data.state === "done") toast("索引完成：" + ((data.result && data.result.total) || 0) + " 个文件");
      poll(0);
      return;
    }
    if (data.type === "switch") {
      const before = state.switches;
      state.switches = data.switches || state.switches;
      renderSwitches();
      renderRoots();
      renderStat();
      // 别的地方（比如命令方块那边）拨了开关，这边也要真的停 / 真的恢复
      renderMarks();
      if (!!before.web !== !!state.switches.web) {
        if (!state.switches.web) { dropGroupCards("web"); render(); }
        else lookup(state.term);
      } else if (!!before.fun !== !!state.switches.fun) {
        if (!state.switches.fun) { dropGroupCards("fun"); memeClose(); render(); }
        else lookup(state.term);
      } else if (!!before.manage !== !!state.switches.manage) {
        lookup(state.term);
      } else {
        render();
      }
      if (before.docked !== state.switches.docked) {
        nativeCall({ cmd: "dock", on: !!state.switches.docked, corner: state.switches.corner || "br" });
      }
      return;
    }
    if (data.type === "source") {
      state.sources = data.sources || state.sources;
      renderMarks();
      renderSwitches();
      return;
    }
  };
}

// 原生告诉页面：辅助功能有没有授权（没有就提示一条，点了跳系统设置）
window.__setAX = (ok) => {
  const hint = $("#axHint");
  if (!hint) return;
  hint.hidden = !!ok || sessionStorage.getItem("axHintOff") === "1";
};

// 原生那边划词命中后会调这个：把词塞进输入框并立刻查
window.__setTerm = (term) => {
  if (!term) return;
  const input = $("#findInput");
  if (input) input.value = term;
  state.scope = "local";
  lookup(term, { skeleton: true });
};

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  window.addEventListener("error", (event) => {
    const box = document.getElementById("toastLayer");
    if (!box || box.dataset.err) return;
    box.dataset.err = "1";
    toast("界面出错了：" + (event.message || "未知错误"), "bad");
  });
  wire();
  watchScroll();
  await poll(1);
  connect();

  setTimeout(() => poll(0), 1500);
  setInterval(() => poll(0), 6000);
}

boot();


/* 滚到底就把底部柔化去掉，不然最后一行永远发虚 */
function watchScroll() {
  const sc = $("#scroll");
  if (!sc) return;
  const update = () => {
    const atEnd = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 4;
    sc.classList.toggle("at-end", atEnd);
  };
  sc.addEventListener("scroll", update, { passive: true });
  window.__scrollUpdate = update;
  update();
}
