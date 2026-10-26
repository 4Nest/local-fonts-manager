"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const samples = {
  zh: "山海之间，自有回响。",
  en: "The quick brown fox.\nAa Bb Cc 0123456789",
  ja: "ことばのかたち、心の風景。",
  subtitle: "每一段故事，都值得被看见。\nEvery story deserves to be seen.",
};
const state = {
  status: null,
  items: [],
  total: 0,
  offset: 0,
  limit: 24,
  view: "all",
  collection: null,
  tag: "",
  selected: new Map(),
  auto: true,
  size: 36,
  request: 0,
  detail: null,
  previewVersion: 0,
};
let searchAbort,
  pollTimer,
  toastTimer,
  lastFocus,
  detailRequest = 0;
const previewCache = new Map();
let previewQueue = [],
  previewActive = 0;
const observer = new IntersectionObserver(
  (entries) => {
    for (const entry of entries)
      if (entry.isIntersecting) {
        observer.unobserve(entry.target);
        previewQueue.push(entry.target);
      }
    drainPreviews();
  },
  { rootMargin: "120px" },
);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `请求失败（${response.status}）`,
    );
  }
  return response.json();
}
const post = (path, body) =>
  api(path, { method: "POST", body: JSON.stringify(body) });
function toast(message) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 3800);
}
function errorMessage(error) {
  if (error.name !== "AbortError") toast(error.message || String(error));
}
function run(action) {
  return async (event) => {
    try {
      await action(event);
    } catch (error) {
      errorMessage(error);
    }
  };
}
function bind(selector, event, action) {
  $(selector).addEventListener(event, run(action));
}
function number(value) {
  return Number(value).toLocaleString("zh-CN");
}
function date(value) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "未知";
}
function bytes(value) {
  return value > 1048576
    ? `${(value / 1048576).toFixed(1)} MiB`
    : `${Math.round(value / 1024)} KiB`;
}

async function loadStatus() {
  const status = await api("/api/status");
  state.status = status;
  $("#total-count").textContent = number(status.total);
  $("#favorite-count").textContent = number(status.favorites);
  $("#nas-dot").classList.toggle("online", status.connected);
  $("#nas-status").textContent = status.connected
    ? "NAS 字体库已连接"
    : "NAS 共享未连接";
  $("#root-path").textContent = status.root;
  $("#root-path").title = status.root;
  $("#index-info").textContent = status.indexed_at
    ? `索引更新于 ${date(status.indexed_at * 1000)} · ${status.index_source}`
    : "正在建立本地索引";
  $("#error-banner").hidden = !status.index_error && status.connected;
  $("#error-banner").textContent =
    status.index_error ||
    (!status.connected
      ? "NAS 共享未连接。仍可搜索已有索引；预览和下载需要重新连接字体目录。"
      : "");
  for (const [key, selector, label] of [
    ["vendor", "#vendor-filter", "全部厂商"],
    ["language", "#language-filter", "全部语言"],
    ["format", "#format-filter", "全部格式"],
  ]) {
    const select = $(selector),
      current = select.value;
    select.innerHTML =
      `<option value="">${label}</option>` +
      status.facets[key]
        .map(
          (item) =>
            `<option value="${escapeHTML(item.name)}">${escapeHTML(item.name)} (${number(item.count)})</option>`,
        )
        .join("");
    select.value = current;
  }
  $("#collections-nav").innerHTML = status.collections.length
    ? status.collections
        .map(
          (c) =>
            `<button class="nav-item ${state.collection === c.id ? "active" : ""}" data-collection="${c.id}">${icon("folder")}<span class="truncate">${escapeHTML(c.name)}</span><span class="nav-count">${number(c.count)}</span></button>`,
        )
        .join("")
    : '<p class="collection-hint">用选集收纳不同用途的字体</p>';
  $("#collection-filter").innerHTML =
    '<option value="">全部字体</option>' +
    status.collections
      .map(
        (c) =>
          `<option value="${c.id}">${escapeHTML(c.name)} (${c.count})</option>`,
      )
      .join("");
  $("#collection-filter").value = state.collection ?? "";
  $("#tag-filter").innerHTML =
    '<option value="">全部标签</option>' +
    status.tags
      .map(
        (t) =>
          `<option value="${escapeHTML(t.name)}">${escapeHTML(t.name)} (${t.count})</option>`,
      )
      .join("");
  $("#tag-filter").value = state.tag;
  $("#tag-filter").hidden = status.tags.length === 0;
  $("#language-nav").innerHTML = status.facets.language
    .slice(0, 6)
    .map(
      (l) =>
        `<button class="nav-item ${$("#language-filter").value === l.name ? "active" : ""}" data-language="${escapeHTML(l.name)}"><span class="language-dot"></span>${escapeHTML(l.name)}<span class="nav-count">${number(l.count)}</span></button>`,
    )
    .join("");
  $("#refresh-button").classList.toggle("busy", status.indexing);
  $("#refresh-button").innerHTML =
    icon("refresh") + (status.indexing ? "正在更新…" : "刷新目录");
  return status;
}

function updateHeading() {
  const collection = state.status?.collections.find(
    (c) => c.id === state.collection,
  );
  const title =
    collection?.name || (state.view === "favorites" ? "我的收藏" : "全部字体");
  $("#page-title").firstChild.textContent = title + " ";
  $("#breadcrumb-name").textContent = title;
  $("#heading-count").textContent = number(state.total);
  $("#page-description").textContent = collection
    ? "将合适的字形，留给合适的作品。"
    : state.view === "favorites"
      ? "喜欢的字形，随时都能找到。"
      : "为下一段文字，找到恰好的字形。";
  $$("#main-nav .nav-item").forEach((button) =>
    button.classList.toggle(
      "active",
      state.collection === null && button.dataset.view === state.view,
    ),
  );
  $$("#collections-nav .nav-item").forEach((button) =>
    button.classList.toggle(
      "active",
      Number(button.dataset.collection) === state.collection,
    ),
  );
  $("#collection-filter").value = state.collection ?? "";
  $("#manage-collection").hidden = state.collection === null;
}

async function loadFonts() {
  searchAbort?.abort();
  searchAbort = new AbortController();
  const request = ++state.request;
  const params = new URLSearchParams({
    q: $("#search").value,
    vendor: $("#vendor-filter").value,
    language: $("#language-filter").value,
    format: $("#format-filter").value,
    limit: state.limit,
    offset: state.offset,
  });
  if (state.view === "favorites") params.set("favorite", "true");
  if (state.collection !== null) params.set("collection", state.collection);
  if (state.tag) params.set("tag", state.tag);
  const result = await api("/api/fonts?" + params, {
    signal: searchAbort.signal,
  });
  if (request !== state.request) return;
  if (state.offset && state.offset >= result.total) {
    state.offset = 0;
    return loadFonts();
  }
  state.items = result.items;
  state.total = result.total;
  $("#results-count").textContent = `${number(result.total)} 款字体`;
  $("#active-filter").textContent = state.auto ? "按语言自动试字" : "";
  $("#page-info").textContent = result.total
    ? `显示 ${state.offset + 1}–${Math.min(state.offset + state.limit, result.total)}，共 ${number(result.total)} 款`
    : "0 款字体";
  $("#page-number").textContent =
    `${Math.floor(state.offset / state.limit) + 1} / ${Math.max(1, Math.ceil(result.total / state.limit))}`;
  $("#prev-page").disabled = state.offset === 0;
  $("#next-page").disabled = state.offset + state.limit >= result.total;
  $("#empty-state").hidden = result.total > 0;
  updateHeading();
  renderCards();
}

function sampleFor(row) {
  if (!state.auto) return $("#preview-text").value.trim() || samples.zh;
  const language = row.language || "";
  if (language.includes("西文")) return samples.en;
  if (language.includes("日文")) return samples.ja;
  if (language.includes("泰文")) return "ตัวอักษรบอกเล่าเรื่องราว";
  if (language.includes("韩文")) return "글자에 담긴 아름다운 이야기";
  if (language.includes("阿拉伯")) return "جمال الحروف يحكي الحكايات";
  if (language.includes("天城")) return "अक्षरों की अपनी कहानी";
  return samples.zh;
}

function specimenHTML(row, text, extra = "", face = 0) {
  return `<div class="specimen ${extra}" data-font-id="${row.id}" data-face="${face}" data-text="${escapeHTML(text)}"><span class="specimen-text"></span><span class="preview-loading">正在加载真实字形…</span></div>`;
}
function renderCards() {
  observer.disconnect();
  previewQueue = previewQueue.filter((el) => el.isConnected);
  state.previewVersion++;
  $("#font-grid").innerHTML = state.items
    .map(
      (
        row,
      ) => `<article class="font-card ${state.selected.has(row.id) ? "selected" : ""}" data-id="${row.id}">
    <div class="font-card-top"><div class="card-name"><h2 class="font-title" title="${escapeHTML(row.family)}">${escapeHTML(row.family)}</h2><p class="font-subtitle" title="${escapeHTML(row.vendor)}">${escapeHTML(row.vendor)}</p></div><div class="card-actions"><input type="checkbox" data-select="${row.id}" aria-label="选择 ${escapeHTML(row.family)}" ${state.selected.has(row.id) ? "checked" : ""}><button class="icon-button star-button ${row.favorite ? "on" : ""}" data-favorite="${row.id}" aria-label="${row.favorite ? "取消收藏" : "收藏"} ${escapeHTML(row.family)}" aria-pressed="${row.favorite}">${icon("star")}</button></div></div>
    ${specimenHTML(row, sampleFor(row))}
    <div class="font-card-footer"><div class="font-badges"><span class="badge language">${escapeHTML(row.language)}</span><span class="badge">${escapeHTML(row.format)}</span>${row.tags
      .slice(0, 1)
      .map((t) => `<span class="badge">${escapeHTML(t)}</span>`)
      .join(
        "",
      )}</div><button class="detail-button" data-detail="${row.id}">查看详情</button></div></article>`,
    )
    .join("");
  $$("#font-grid .specimen").forEach((el) => {
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", "预览字体详情");
    observer.observe(el);
  });
  syncSelection();
}
async function drainPreviews() {
  while (previewActive < 3 && previewQueue.length) {
    const el = previewQueue.shift();
    if (!el.isConnected) continue;
    previewActive++;
    loadPreview(el).finally(() => {
      previewActive--;
      drainPreviews();
    });
  }
}
async function loadPreview(el) {
  const { fontId, text, face } = el.dataset;
  const key = `${fontId}:${face}:${text}`;
  try {
    let task = previewCache.get(key);
    if (!task) {
      task = (async () => {
        const params = new URLSearchParams({ text, face });
        const response = await fetch(
          `/api/fonts/${fontId}/preview.woff2?${params}`,
        );
        if (!response.ok)
          throw new Error(
            (await response.json().catch(() => ({}))).detail ||
              "字体预览不可用",
          );
        const missing = JSON.parse(
          response.headers.get("X-Missing-Characters") || "[]",
        );
        const raw = await response.arrayBuffer();
        if (
          ![...text].some(
            (character) =>
              !/\s/u.test(character) && !missing.includes(character),
          )
        ) {
          return { font: null, family: "", missing };
        }
        const family =
          "preview_" + fontId + "_" + Math.random().toString(36).slice(2);
        const font = new FontFace(family, raw);
        await font.load();
        document.fonts.add(font);
        return { font, family, missing };
      })();
      previewCache.set(key, task);
      task.catch(() => previewCache.delete(key));
      if (previewCache.size > 120) {
        const oldest = previewCache.keys().next().value;
        previewCache
          .get(oldest)
          .then((value) => value.font && document.fonts.delete(value.font))
          .catch(() => {});
        previewCache.delete(oldest);
      }
    }
    const { family, missing } = await task;
    if (!el.isConnected) return;
    if (family) el.style.fontFamily = `"${family}"`;
    const target = el.querySelector(".specimen-text");
    target.replaceChildren();
    // Explicit placeholders prevent silent browser font fallback from misrepresenting coverage.
    let supported = "";
    for (const character of text) {
      if (missing.includes(character)) {
        if (supported) {
          target.append(document.createTextNode(supported));
          supported = "";
        }
        const span = document.createElement("span");
        span.className = "missing-glyph";
        span.textContent = "□";
        span.title = `缺字：${character}`;
        target.append(span);
      } else supported += character;
    }
    if (supported) target.append(document.createTextNode(supported));
    if (missing.length) {
      const warning = document.createElement("span");
      warning.className = "preview-warning";
      warning.textContent = `缺 ${missing.length} 字`;
      warning.title = "不支持：" + missing.join(" ");
      el.append(warning);
    }
    el.classList.add("loaded");
  } catch (error) {
    if (el.isConnected) {
      el.classList.add("failed");
      el.querySelector(".preview-loading").textContent =
        "此字体无法预览 · 查看详情";
      el.title = error.message;
    }
  }
}
function observePreviews(root) {
  root.querySelectorAll(".specimen").forEach((el) => observer.observe(el));
}
function syncSelection() {
  $("#selection-count").textContent = state.selected.size;
  $("#selection-bar").hidden = state.selected.size === 0;
  $("#select-page").textContent =
    state.items.length && state.items.every((row) => state.selected.has(row.id))
      ? "取消本页"
      : "选择本页";
  $$("#font-grid .font-card").forEach((card) => {
    const checked = state.selected.has(card.dataset.id);
    card.classList.toggle("selected", checked);
    card.querySelector("input").checked = checked;
  });
}
async function afterOrganize(message) {
  await loadStatus();
  await loadFonts();
  if (message) toast(message);
}
async function toggleFavorite(id) {
  const row =
    state.items.find((r) => r.id === id) ||
    (state.detail?.id === id ? state.detail : null);
  const favorite = !row.favorite;
  await post("/api/annotations", { font_ids: [id], favorite });
  if (state.detail?.id === id) state.detail.favorite = favorite;
  await afterOrganize(favorite ? "已收藏" : "已取消收藏");
}

function showModal(title, html, wide = false) {
  lastFocus = document.activeElement;
  $("#modal-title").textContent = title;
  $("#modal-content").innerHTML = html;
  $("#modal").classList.toggle("modal-wide", wide);
  if (!$("#modal").open) $("#modal").showModal();
}
function closeModal() {
  $("#modal").close();
  lastFocus?.focus();
}
function closeDrawer() {
  ++detailRequest;
  $("#detail-drawer").hidden = true;
  $("#drawer-overlay").hidden = true;
  state.detail = null;
  lastFocus?.focus();
}
async function openDetails(id, face = 0) {
  const request = ++detailRequest;
  lastFocus = document.activeElement;
  $("#detail-drawer").hidden = false;
  $("#drawer-overlay").hidden = false;
  $("#detail-content").innerHTML =
    '<div class="loading-state">正在读取字体信息…</div>';
  $("#close-drawer").focus();
  try {
    const row =
      state.items.find((r) => r.id === id) || state.selected.get(id) || {};
    const params = new URLSearchParams({ face, text: sampleFor(row) });
    const detail = await api(`/api/fonts/${id}?${params}`);
    if (request !== detailRequest) return;
    state.detail = detail;
    const missing = detail.missing_characters;
    $("#detail-content").innerHTML =
      `<h2 class="detail-family">${escapeHTML(detail.family_name || detail.family)}</h2><p class="detail-filename">${escapeHTML(detail.filename)}</p>
      ${detail.face_count > 1 ? `<label class="field-label">字体合集中的字面<select id="face-select" class="field">${Array.from({ length: Math.min(1000, detail.face_count) }, (_, i) => `<option value="${i}" ${i === face ? "selected" : ""}>字面 ${i + 1}</option>`).join("")}</select></label>` : ""}
      ${specimenHTML(detail, sampleFor(detail), "detail-sample", face)}
      <div class="coverage-status ${missing.length ? "missing" : ""}">${missing.length ? `试字缺字：${escapeHTML(missing.join(" "))}（以 □ 标示）` : "当前试字字符均有收录"}</div>
      <div class="detail-actions"><button id="detail-favorite" class="button">${icon("star")}${detail.favorite ? "取消收藏" : "收藏字体"}</button><a class="button" href="/api/fonts/${id}/download">${icon("download")}下载原文件</a></div>
      <dl class="detail-meta">${[
        ["厂商", detail.vendor],
        ["语言", detail.language],
        ["字面", detail.style],
        ["字重", detail.weight],
        ["格式", `${detail.format}${detail.variable ? " · 可变字体" : ""}`],
        [
          "字符 / 字形",
          `${number(detail.character_count)} / ${number(detail.glyph_count)}`,
        ],
        ["文件大小", bytes(detail.size_bytes)],
        ["版本", detail.version],
        ["PostScript", detail.postscript],
        ["设计师", detail.designer],
      ]
        .map(([k, v]) => `<dt>${k}</dt><dd>${escapeHTML(v || "—")}</dd>`)
        .join("")}</dl>
      ${detail.axes.length ? `<p class="small-note">可变轴：${detail.axes.map((a) => `${escapeHTML(a.tag)} ${a.min}–${a.max}（默认 ${a.default}）`).join("，")}</p>` : ""}
      <section class="detail-section"><h3>整理字体</h3><label class="field-label" for="detail-tags">标签 · 用逗号分隔</label><input class="field" id="detail-tags" placeholder="例如：字幕、标题、手写" value="${escapeHTML(detail.tags.join("，"))}"><label class="field-label" for="detail-note">备注</label><textarea class="field" id="detail-note" maxlength="4000" placeholder="记录适用场景、搭配或使用注意…">${escapeHTML(detail.note)}</textarea><button id="save-detail" class="button primary">保存整理</button>
      <h3 style="margin-top:26px">所属选集</h3><div id="detail-collections">${detail.collections.map((c) => `<span class="collection-chip">${escapeHTML(c.name)}<button data-remove-member="${c.id}" aria-label="移出 ${escapeHTML(c.name)}">×</button></span>`).join("") || '<p class="small-note">尚未加入选集</p>'}</div><button id="detail-add-collection" class="button">${icon("plus")}加入选集</button></section>
      <section class="detail-section"><h3>原文件位置</h3><div class="detail-path">${escapeHTML(detail.path)}</div><p>收藏、标签与选集保存在管理器中；下载和删除直接针对此文件。</p></section>
      <section class="detail-section detail-danger"><h3>删除字体</h3><p>从 NAS 永久删除此字体文件，并移除对应的收藏、标签和选集关系。</p><button id="detail-delete" class="button danger-button">删除原文件</button></section>
      ${detail.copyright || detail.license ? `<section class="detail-section"><h3>版权与授权信息</h3><p>${escapeHTML(detail.copyright || "")}</p><p>${escapeHTML(detail.license || "字体未附内嵌授权说明，请查看原始授权文件。")}</p></section>` : ""}`;
    observePreviews($("#detail-content"));
    $("#detail-delete").onclick = () => confirmDeleteFont(detail);
    $("#detail-favorite").onclick = run(async () => {
      await toggleFavorite(id);
      await openDetails(id, face);
    });
    $("#save-detail").onclick = run(async () => {
      await post("/api/annotations", {
        font_ids: [id],
        tags: $("#detail-tags").value.split(/[,，]/),
        note: $("#detail-note").value,
      });
      await afterOrganize("整理已保存");
    });
    $("#detail-add-collection").onclick = () => organizeModal([id]);
    $("#face-select")?.addEventListener(
      "change",
      run((e) => openDetails(id, Number(e.target.value))),
    );
    $$("#detail-collections [data-remove-member]").forEach(
      (button) =>
        (button.onclick = run(async () => {
          await post("/api/annotations", {
            font_ids: [id],
            collection_id: Number(button.dataset.removeMember),
            remove_collection: true,
          });
          await loadStatus();
          await openDetails(id, face);
          await loadFonts();
          toast("已移出选集");
        })),
    );
  } catch (error) {
    if (request !== detailRequest) return;
    $("#detail-content").innerHTML =
      `<div class="error-banner">${escapeHTML(error.message)}</div><p class="small-note">字体可能已移动、共享断开，或字体本身不受支持。可先刷新目录再试。</p>`;
  }
}

function confirmDeleteFont(detail) {
  showModal(
    "永久删除字体",
    `<p>确定删除 <strong>${escapeHTML(detail.family_name || detail.family)}</strong> 吗？</p><div class="detail-path">${escapeHTML(detail.path)}</div><p class="small-note">此操作会永久删除 NAS 上的原文件，并移除它的收藏、标签和选集关系，无法撤销。字体合集中的全部字面会一起删除。</p><div class="modal-actions"><button id="cancel-delete" class="button">取消</button><button id="confirm-delete" class="button danger-button">永久删除</button></div>`,
  );
  $("#cancel-delete").onclick = closeModal;
  $("#confirm-delete").onclick = run(async () => {
    $("#confirm-delete").disabled = true;
    $("#confirm-delete").textContent = "正在删除…";
    try {
      const result = await api(`/api/fonts/${detail.id}`, { method: "DELETE" });
      closeModal();
      closeDrawer();
      state.selected.delete(detail.id);
      syncSelection();
      await afterOrganize(result.warning || "字体已永久删除");
    } catch (error) {
      $("#confirm-delete").disabled = false;
      $("#confirm-delete").textContent = "永久删除";
      throw error;
    }
  });
}

function newCollection() {
  showModal(
    "新建选集",
    '<form id="collection-form" class="modal-form"><label>选集名称<input id="collection-name" class="field" required maxlength="60" placeholder="例如：字幕正文、标题候选"></label><p class="small-note">选集按用途整理字体，同一款字体可以放进多个选集。</p><div class="modal-actions"><button type="submit" class="button primary">创建选集</button></div></form>',
  );
  $("#collection-form").onsubmit = run(async (e) => {
    e.preventDefault();
    await post("/api/collections", { name: $("#collection-name").value });
    closeModal();
    await loadStatus();
    toast("选集已创建");
  });
  $("#collection-name").focus();
}
function organizeModal(ids) {
  showModal(
    `整理 ${ids.length} 款字体`,
    `<form id="organize-form" class="modal-form"><label>加入选集<select class="field" id="organize-collection"><option value="">暂不加入选集</option>${state.status.collections.map((c) => `<option value="${c.id}">${escapeHTML(c.name)}</option>`).join("")}</select></label><label>或新建选集<input class="field" id="organize-new" maxlength="60" placeholder="输入新选集名称"></label><label>替换标签（留空保留原标签）<input class="field" id="organize-tags" placeholder="字幕、标题、手写"></label><div class="modal-actions">${state.collection !== null ? '<button type="button" id="remove-selected" class="button">移出当前选集</button>' : ""}<button type="submit" class="button primary">保存整理</button></div></form>`,
  );
  $("#organize-form").onsubmit = run(async (e) => {
    e.preventDefault();
    const body = { font_ids: ids };
    const name = $("#organize-new").value.trim();
    if (name) {
      const collection = await post("/api/collections", { name });
      body.collection_id = collection.id;
    } else if ($("#organize-collection").value)
      body.collection_id = Number($("#organize-collection").value);
    if ($("#organize-tags").value.trim())
      body.tags = $("#organize-tags").value.split(/[,，]/);
    if (!body.collection_id && !body.tags)
      throw new Error("请选择选集或填写标签");
    await post("/api/annotations", body);
    closeModal();
    await afterOrganize("整理已保存");
    if (state.detail) await openDetails(state.detail.id, state.detail.face);
  });
  $("#remove-selected")?.addEventListener(
    "click",
    run(async () => {
      await post("/api/annotations", {
        font_ids: ids,
        collection_id: state.collection,
        remove_collection: true,
      });
      closeModal();
      state.selected.clear();
      await afterOrganize("已移出当前选集");
    }),
  );
}
async function compareFonts() {
  const rows = [...state.selected.values()];
  if (rows.length < 2 || rows.length > 6)
    throw new Error("请选择 2–6 款字体进行对比");
  // Compare all faces with the same text so differences in coverage remain visible.
  const text = $("#preview-text").value.trim() || samples.zh;
  showModal(
    `字形对比 · ${rows.length} 款`,
    `<p class="small-note">使用同一段文字和字号。缺字显示为 □，不会用系统字体补字。</p>${rows.map((row) => `<div class="compare-row"><div class="compare-heading"><strong>${escapeHTML(row.family)}</strong><span>${escapeHTML(row.vendor)} / ${escapeHTML(row.language)}</span></div>${specimenHTML(row, text)}</div>`).join("")}`,
    true,
  );
  observePreviews($("#modal-content"));
}
async function showInbox() {
  showModal("入库动态", '<div class="loading-state">正在读取 NAS 状态…</div>');
  const result = await api("/api/inbox"),
    worker = result.worker;
  const updated = worker.updated_at ? Date.parse(worker.updated_at) : NaN;
  const stale = !Number.isFinite(updated) || Date.now() - updated > 180000;
  $("#modal-content").innerHTML =
    `<div class="inbox-summary"><strong>${escapeHTML(worker.phase)}</strong><p>最后更新：${date(worker.updated_at)}${stale ? " · 状态较旧，请检查入库容器是否运行" : ""}</p><p>入库服务索引：${worker.library_files === undefined ? "未知" : number(worker.library_files) + " 个文件"}</p></div><h3 class="field-label">待入库项目 · ${result.pending.length}</h3>${result.pending.length ? `<ul class="pending-list">${result.pending.map((item) => `<li>${escapeHTML(item.name)}${item.directory ? " /" : ""}</li>`).join("")}</ul>` : '<p class="small-note">当前没有待入库项目。</p>'}<p class="small-note">新字体复制稳定后，由现有 NAS 自动入库服务检查、去重和分类。完成后点击“刷新目录”同步到字库。</p><div class="detail-path">${escapeHTML(result.inbox)}</div>`;
}
const uploadLabels = {
  pending: "等待投递",
  uploading: "投递中",
  succeeded: "已投递",
  failed: "投递失败",
  skipped: "已跳过",
  cancelled: "已取消",
};
let uploadScanning = false,
  uploadScanned = 0,
  uploadRenderFrame = 0;
const uploadQueue = new FontImport.UploadQueue(() => {
  cancelAnimationFrame(uploadRenderFrame);
  uploadRenderFrame = requestAnimationFrame(renderUpload);
});

function renderUpload() {
  const counts = uploadQueue.counts();
  const busy = uploadScanning || uploadQueue.running;
  const button = $("#upload-button");
  const buttonText = uploadScanning
    ? "正在读取文件夹…"
    : uploadQueue.running
      ? `投递中 ${number(counts.succeeded)}/${number(uploadQueue.items.length - counts.skipped)}`
      : counts.pending
        ? `待投递 ${number(counts.pending)} 个文件`
        : uploadQueue.items.length
          ? "查看投递结果"
          : "添加字体";
  button.innerHTML = `${icon("upload")}${buttonText}`;
  if (!$("#upload-panel")) return;
  $("#upload-zone").classList.toggle("busy", busy);
  $("#upload-choose-files").disabled = busy;
  $("#upload-choose-folder").disabled = busy;
  $("#upload-clear").disabled = busy || !uploadQueue.items.length;
  $("#upload-submit").disabled = busy || !counts.pending;
  $("#upload-submit").textContent = counts.pending
    ? `投递 ${number(counts.pending)} 个文件`
    : "投递到待入库";
  $("#upload-cancel").hidden = !counts.pending;
  $("#upload-cancel").disabled = uploadScanning;
  $("#upload-retry").hidden = !counts.failed && !counts.cancelled;
  $("#upload-retry").disabled = busy;
  $("#upload-report").disabled = !uploadQueue.items.length;
  const current = uploadQueue.items.find((item) => item.status === "uploading");
  $("#upload-progress").textContent = uploadScanning
    ? `正在读取文件夹及子目录，已找到 ${number(uploadScanned)} 个文件…`
    : current
      ? `正在投递：${current.path}`
      : counts.pending
        ? `已准备 ${number(counts.pending)} 个文件，点击下方按钮开始投递。`
        : uploadQueue.items.length
          ? "本轮投递已结束。已投递的文件仍需等待 NAS 检查、去重和入库。"
          : "可以一次拖入多个文件夹，也可以选择单个字体文件。";
  $("#upload-counts").textContent = uploadQueue.items.length
    ? `已投递 ${number(counts.succeeded)} · 失败 ${number(counts.failed)} · 跳过 ${number(counts.skipped)} · 取消 ${number(counts.cancelled)} · 等待 ${number(counts.pending)}`
    : "";
  const progress = $("#upload-meter");
  progress.hidden = !uploadQueue.items.length;
  progress.max = uploadQueue.items.length;
  progress.value =
    counts.succeeded + counts.failed + counts.skipped + counts.cancelled;
  $("#upload-details").hidden = !uploadQueue.items.length;
  const priority = {
    uploading: 0,
    failed: 1,
    pending: 2,
    cancelled: 3,
    skipped: 4,
    succeeded: 5,
  };
  const visible = [...uploadQueue.items]
    .sort((a, b) => priority[a.status] - priority[b.status] || a.id - b.id)
    .slice(0, 80);
  $("#upload-list").innerHTML = visible
    .map(
      (item) =>
        `<li class="upload-item ${item.status}"><span class="upload-item-path">${escapeHTML(item.path)}${item.error ? `<small>${escapeHTML(item.error)}</small>` : ""}</span><span class="upload-item-status">${uploadLabels[item.status]}</span></li>`,
    )
    .join("");
  $("#upload-list-note").textContent =
    uploadQueue.items.length > visible.length
      ? `显示 ${visible.length} 条，优先显示投递中及失败项；下载结果可查看全部 ${number(uploadQueue.items.length)} 条路径和原因。`
      : "";
}

async function addUploadSelection(readFiles) {
  if (uploadScanning || uploadQueue.running) {
    toast("请等待当前读取或投递结束后再添加文件");
    return;
  }
  uploadScanning = true;
  uploadScanned = 0;
  renderUpload();
  try {
    const descriptors = await readFiles((count) => {
      uploadScanned = count;
      cancelAnimationFrame(uploadRenderFrame);
      uploadRenderFrame = requestAnimationFrame(renderUpload);
    });
    uploadQueue.add(descriptors);
    if (!descriptors.length)
      toast("没有读到文件；请检查文件夹是否为空，或改用“选择文件夹”");
  } catch (error) {
    errorMessage(error);
  } finally {
    uploadScanning = false;
    renderUpload();
  }
}

async function submitUploads() {
  await uploadQueue.start(async (file) => {
    const data = new FormData();
    // Each request gets its own server-side batch; matching names in different
    // directories remain separate and are deduplicated by the ingestion worker.
    data.append("file", file, file.name);
    const response = await fetch("/api/inbox/upload", {
      method: "POST",
      body: data,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(
        typeof result.detail === "string"
          ? result.detail
          : `上传失败（${response.status}）`,
      );
    return result;
  });
  renderUpload();
  const counts = uploadQueue.counts();
  toast(
    `已投递 ${number(counts.succeeded)} 个文件，等待 NAS 入库${counts.failed ? `；${number(counts.failed)} 个失败可重试` : ""}`,
  );
}

function downloadUploadReport() {
  const lines = [
    "字体投递结果（已投递不代表已入库）",
    new Date().toLocaleString("zh-CN"),
    "",
    ...uploadQueue.items.map(
      (item) =>
        `${uploadLabels[item.status]}\t${item.path}${item.error ? `\t${item.error}` : ""}`,
    ),
  ];
  const url = URL.createObjectURL(
    new Blob(["\ufeff" + lines.join("\n")], {
      type: "text/plain;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "字体投递结果.txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function showUpload() {
  showModal(
    "添加字体",
    `<section id="upload-panel"><div id="upload-zone" class="upload-zone">${icon("upload")}<strong>拖入字体文件或整个文件夹</strong><small>自动读取所有子目录 · 单个文件不超过 100 MiB</small><div class="upload-pickers"><button class="button" id="upload-choose-files" type="button">选择文件</button><button class="button" id="upload-choose-folder" type="button">选择文件夹</button></div><input id="upload-files" type="file" multiple accept=".ttf,.otf,.ttc,.otc,.woff,.woff2" hidden><input id="upload-folder" type="file" multiple webkitdirectory hidden></div><p class="small-note">TTF / OTF / TTC / OTC 会由 NAS 自动检查、去重和分类；WOFF / WOFF2 会进入待检查区。其他文件及超过 100 MiB 的文件会跳过并列出原因。</p><div id="upload-progress" class="upload-progress" role="status" aria-live="polite"></div><progress id="upload-meter" class="upload-meter" aria-label="文件投递进度" hidden></progress><p id="upload-counts" class="small-note"></p><details id="upload-details" class="upload-details" hidden><summary>查看文件及投递结果</summary><ul id="upload-list" class="upload-list"></ul><p id="upload-list-note" class="small-note"></p></details><p class="small-note">关闭弹窗后仍会继续投递，可从顶部按钮查看进度。请保持此网页打开；已投递仅表示进入待入库，入库完成后再刷新目录。</p><div class="upload-utilities"><button id="upload-report" class="text-button" type="button">下载结果</button><button id="upload-clear" class="text-button" type="button">清空列表</button></div><div class="modal-actions upload-actions"><button id="upload-cancel" class="button" type="button" hidden>取消未投递项</button><button id="upload-retry" class="button" type="button" hidden>重试未成功项</button><button class="button primary" type="button" id="upload-submit">投递到待入库</button></div></section>`,
  );
  $("#upload-choose-files").onclick = () => $("#upload-files").click();
  $("#upload-choose-folder").onclick = () => $("#upload-folder").click();
  for (const selector of ["#upload-files", "#upload-folder"])
    $(selector).onchange = (event) => {
      const descriptors = FontImport.filesFromSelection(event.target.files);
      event.target.value = "";
      void addUploadSelection(() => descriptors);
    };
  $("#upload-submit").onclick = run(submitUploads);
  $("#upload-cancel").onclick = () => uploadQueue.cancelPending();
  $("#upload-retry").onclick = run(async () => {
    uploadQueue.retryUnfinished();
    await submitUploads();
  });
  $("#upload-clear").onclick = () => uploadQueue.clear();
  $("#upload-report").onclick = downloadUploadReport;
  const zone = $("#upload-zone");
  let dragDepth = 0;
  zone.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    if (!uploadQueue.running && !uploadScanning) {
      dragDepth++;
      zone.classList.add("drag-active");
    }
  });
  zone.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) zone.classList.remove("drag-active");
  });
  zone.addEventListener("drop", () => {
    dragDepth = 0;
    zone.classList.remove("drag-active");
  });
  renderUpload();
}

// Dropping on the workspace also opens the importer, instead of navigating away.
for (const eventName of ["dragover", "drop"])
  document.addEventListener(eventName, (event) => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    if (event.target.closest?.("#recognition-drop")) return;
    event.preventDefault();
    if (eventName === "dragover") {
      event.dataTransfer.dropEffect =
        uploadQueue.running || uploadScanning ? "none" : "copy";
      return;
    }
    if (!$("#upload-panel") || !$("#modal").open) showUpload();
    void addUploadSelection((onProgress) =>
      FontImport.filesFromDrop(event.dataTransfer, onProgress),
    );
  });
window.addEventListener("beforeunload", (event) => {
  if (uploadScanning || uploadQueue.running || uploadQueue.counts().pending) {
    event.preventDefault();
    event.returnValue = "";
  }
});
function showMCP() {
  const config = JSON.stringify(
    {
      mcpServers: {
        fonts: {
          url: `${location.origin}/mcp`,
          headers: { Authorization: "Bearer ${FONT_MANAGER_API_TOKEN}" },
        },
      },
    },
    null,
    2,
  );
  showModal(
    "连接 MCP 客户端",
    `<p class="mcp-intro">支持 Streamable HTTP 的 MCP 客户端可以搜索字体、检测缺字、查看样张，并共用收藏、标签及选集。按客户端文档合并以下配置；如果配置格式不同，请使用相同的 MCP 地址和 Bearer Token。</p><pre id="mcp-config">${escapeHTML(config)}</pre><p class="small-note">在字库“设置”中复制 API Token，并设置为 FONT_MANAGER_API_TOKEN，或直接替换配置中的占位符。重置 Token 后，请同步更新客户端。</p><div class="mcp-tools"><span>字体搜索</span><span>真实样张</span><span>缺字检测</span><span>收藏与标签</span><span>选集整理</span><span>入库状态</span></div><p class="small-note">连接后可让客户端：“找几款支持简体中文的字体，检查‘山海之间’是否缺字，做样张对比，再加入字幕候选选集。”</p><div class="modal-actions"><button class="button primary" id="copy-mcp">复制配置</button></div>`,
  );
  $("#copy-mcp").onclick = run(async () => {
    await copyText(config);
    toast("MCP 配置已复制");
  });
}
async function copyText(value) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (_) {
      /* LAN HTTP / clipboard permission fallback. */
    }
  }
  const focus = document.activeElement;
  const field = document.createElement("textarea");
  field.value = value;
  field.style.cssText = "position:fixed;opacity:0;left:0;top:0";
  // Put the field inside the open dialog so it remains focusable.
  (document.querySelector("dialog[open]") || document.body).append(field);
  field.focus();
  field.select();
  field.setSelectionRange(0, field.value.length);
  const copied = document.execCommand("copy");
  field.remove();
  focus?.focus();
  if (!copied) throw new Error("浏览器未允许复制，请选中文本手动复制");
}
let savedAPIToken = "";
function setPage(page, push = true) {
  const settings = page === "settings";
  $("#library-page").hidden = settings;
  $("#settings-page").hidden = !settings;
  $("#settings-button").classList.toggle("active", settings);
  document.title = settings ? "设置 · 字库" : "字库 · Local Fonts Manager";
  if (settings) {
    $("#selection-bar").hidden = true;
    $$("#main-nav .nav-item, #collections-nav .nav-item").forEach((button) =>
      button.classList.remove("active"),
    );
  } else {
    updateHeading();
    syncSelection();
  }
  const path = settings ? "/settings" : "/";
  if (push && location.pathname !== path) history.pushState({}, "", path);
}
function renderAPIToken(token, message = "") {
  savedAPIToken = token;
  $("#api-token").value = token;
  $("#api-token").type = "password";
  $("#toggle-token").textContent = "显示";
  $("#toggle-token").setAttribute("aria-pressed", "false");
  $("#copy-token").disabled = false;
  $("#token-status").textContent = message;
}
async function showSettings(push = true) {
  setPage("settings", push);
  const settings = await api("/api/settings");
  renderAPIToken(settings.api_token);
  $("#settings-root").textContent = settings.root;
  $("#mcp-endpoint").value = location.origin + "/mcp";
}
bind("#settings-button", "click", () => showSettings());
bind("#back-to-library", "click", () => setPage("library"));
bind("#settings-mcp-config", "click", showMCP);
bind("#toggle-token", "click", () => {
  const show = $("#api-token").type === "password";
  $("#api-token").type = show ? "text" : "password";
  $("#toggle-token").textContent = show ? "隐藏" : "显示";
  $("#toggle-token").setAttribute("aria-pressed", String(show));
});
bind("#api-token", "input", () => {
  const changed = $("#api-token").value.trim() !== savedAPIToken;
  $("#copy-token").disabled = changed;
  $("#token-status").textContent = changed ? "尚未保存" : "";
});
bind("#copy-token", "click", async () => {
  await copyText(savedAPIToken);
  toast("API Token 已复制");
});
bind("#copy-endpoint", "click", async () => {
  await copyText($("#mcp-endpoint").value);
  toast("MCP 地址已复制");
});
bind("#api-token-form", "submit", async (event) => {
  event.preventDefault();
  $("#save-token").disabled = true;
  try {
    const result = await api("/api/settings/api-token", {
      method: "PUT",
      body: JSON.stringify({ token: $("#api-token").value.trim() }),
    });
    renderAPIToken(result.api_token, "已保存，请同步更新客户端");
    toast("API Token 已保存");
  } finally {
    $("#save-token").disabled = false;
  }
});
bind("#reset-token", "click", async () => {
  $("#reset-token").disabled = true;
  try {
    const result = await post("/api/settings/api-token/reset", {});
    renderAPIToken(result.api_token, "已重置，旧 Token 已失效");
    toast("API Token 已重置");
  } finally {
    $("#reset-token").disabled = false;
  }
});
window.addEventListener(
  "popstate",
  run(async () => {
    if (location.pathname === "/settings") await showSettings(false);
    else setPage("library", false);
  }),
);
async function resetFilters() {
  $("#search").value = "";
  $("#vendor-filter").value = "";
  $("#language-filter").value = "";
  $("#format-filter").value = "";
  state.tag = "";
  state.offset = 0;
  await loadStatus();
  await loadFonts();
}
function debounce(action, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => action(...args).catch(errorMessage), delay);
  };
}

bind("#main-nav", "click", async (e) => {
  const button = e.target.closest("[data-view]");
  if (!button) return;
  setPage("library");
  state.view = button.dataset.view;
  state.collection = null;
  state.offset = 0;
  await loadFonts();
});
bind("#collections-nav", "click", async (e) => {
  const button = e.target.closest("[data-collection]");
  if (!button) return;
  setPage("library");
  state.collection = Number(button.dataset.collection);
  state.view = "all";
  state.offset = 0;
  await loadFonts();
});
bind("#language-nav", "click", async (e) => {
  const button = e.target.closest("[data-language]");
  if (!button) return;
  setPage("library");
  $("#language-filter").value = button.dataset.language;
  state.offset = 0;
  await loadStatus();
  await loadFonts();
});
$("#search").addEventListener(
  "input",
  debounce(async () => {
    state.offset = 0;
    await loadFonts();
  }, 220),
);
for (const selector of ["#vendor-filter", "#language-filter", "#format-filter"])
  bind(selector, "change", async () => {
    state.offset = 0;
    await loadStatus();
    await loadFonts();
  });
for (const selector of ["#clear-filters", "#empty-reset"])
  bind(selector, "click", resetFilters);
bind("#prev-page", "click", async () => {
  state.offset = Math.max(0, state.offset - state.limit);
  await loadFonts();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
bind("#next-page", "click", async () => {
  state.offset += state.limit;
  await loadFonts();
  window.scrollTo({ top: 0, behavior: "smooth" });
});
bind("#font-grid", "click", async (e) => {
  const star = e.target.closest("[data-favorite]");
  if (star) {
    await toggleFavorite(star.dataset.favorite);
    return;
  }
  const select = e.target.closest("[data-select]");
  if (select) {
    const id = select.dataset.select;
    if (select.checked) {
      if (state.selected.size >= 200) throw new Error("最多同时选择 200 款");
      state.selected.set(
        id,
        state.items.find((r) => r.id === id),
      );
    } else state.selected.delete(id);
    syncSelection();
    return;
  }
  const detail =
    e.target.closest("[data-detail]") || e.target.closest(".specimen");
  if (detail) await openDetails(detail.dataset.detail || detail.dataset.fontId);
});
bind("#font-grid", "keydown", async (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".specimen")) {
    e.preventDefault();
    await openDetails(e.target.dataset.fontId);
  }
});
bind("#select-page", "click", () => {
  const all = state.items.every((r) => state.selected.has(r.id));
  for (const row of state.items) {
    if (all) state.selected.delete(row.id);
    else if (state.selected.size < 200) state.selected.set(row.id, row);
  }
  syncSelection();
});
bind("#clear-selection", "click", () => {
  state.selected.clear();
  syncSelection();
});
bind("#compare-button", "click", compareFonts);
bind("#batch-favorite", "click", async () => {
  await post("/api/annotations", {
    font_ids: [...state.selected.keys()],
    favorite: true,
  });
  await afterOrganize("已收藏所选字体");
});
bind("#batch-organize", "click", () =>
  organizeModal([...state.selected.keys()]),
);
bind("#add-collection", "click", newCollection);
bind("#mobile-new-collection", "click", newCollection);
bind("#collection-filter", "change", async (e) => {
  state.collection = e.target.value ? Number(e.target.value) : null;
  state.view = "all";
  state.offset = 0;
  await loadFonts();
});
bind("#tag-filter", "change", async (e) => {
  state.tag = e.target.value;
  state.offset = 0;
  await loadFonts();
});
bind("#manage-collection", "click", () => {
  const collection = state.status.collections.find(
    (c) => c.id === state.collection,
  );
  if (!collection) return;
  showModal(
    "选集设置",
    `<p class="mcp-intro">${escapeHTML(collection.name)} · ${collection.count} 款字体</p><p class="small-note">删除选集会移除成员关联，字体文件、收藏、标签和备注都会保留。</p><div class="modal-actions"><button id="delete-current-collection" class="button danger-text">删除这个选集</button></div>`,
  );
  $("#delete-current-collection").onclick = run(async () => {
    await api("/api/collections/" + collection.id, { method: "DELETE" });
    state.collection = null;
    state.offset = 0;
    closeModal();
    await afterOrganize("选集已删除，字体文件已保留");
  });
});
bind("#inbox-button", "click", showInbox);
bind("#upload-button", "click", showUpload);
bind("#recognition-button", "click", () =>
  FontRecognition.show({
    api,
    showModal,
    closeModal,
    specimenHTML,
    observePreviews,
    openDetails,
    escapeHTML,
    toast,
  }),
);
bind("#close-modal", "click", closeModal);
bind("#close-drawer", "click", closeDrawer);
bind("#drawer-overlay", "click", closeDrawer);
bind("#grid-view", "click", () => {
  $("#font-grid").classList.remove("list");
  $("#grid-view").classList.add("active");
  $("#list-view").classList.remove("active");
});
bind("#list-view", "click", () => {
  $("#font-grid").classList.add("list");
  $("#list-view").classList.add("active");
  $("#grid-view").classList.remove("active");
});
bind("#font-size", "input", (e) => {
  state.size = Number(e.target.value);
  document.documentElement.style.setProperty(
    "--sample-size",
    state.size + "px",
  );
  $("#font-size-value").textContent = state.size + " px";
});
bind("#light-preview", "click", () => {
  document.body.classList.remove("preview-dark");
  $("#light-preview").classList.add("active");
  $("#dark-preview").classList.remove("active");
});
bind("#dark-preview", "click", () => {
  document.body.classList.add("preview-dark");
  $("#dark-preview").classList.add("active");
  $("#light-preview").classList.remove("active");
});
const updatePreview = debounce(async () => {
  renderCards();
  $("#active-filter").textContent = state.auto ? "按语言自动试字" : "";
}, 350);
$("#preview-text").addEventListener("input", () => {
  state.auto = false;
  $$(".preset").forEach((b) => b.classList.remove("active"));
  updatePreview();
});
$$(".preset").forEach(
  (button) =>
    (button.onclick = () => {
      state.auto = button.dataset.sample === "auto";
      $("#preview-text").value = samples[button.dataset.sample] || samples.zh;
      $$(".preset").forEach((b) => b.classList.toggle("active", b === button));
      updatePreview();
    }),
);
bind("#reset-preview", "click", () => {
  $('.preset[data-sample="auto"]').click();
});
bind("#refresh-button", "click", () =>
  showModal(
    "更新字体目录",
    '<p class="small-note">快速刷新读取 NAS 现有目录。直接在文件夹里增删过字体时，选择完整扫描；扫描完成前继续保留当前索引和全部整理记录。</p><div class="modal-actions"><button id="scan-refresh" class="button">完整扫描</button><button id="catalog-refresh" class="button primary">快速刷新</button></div>',
  ),
);
$("#modal-content").addEventListener(
  "click",
  run(async (e) => {
    if (!["scan-refresh", "catalog-refresh"].includes(e.target.id)) return;
    const source = e.target.id === "scan-refresh" ? "scan" : "catalog";
    await post("/api/refresh", { source });
    closeModal();
    toast("正在后台更新目录");
    await loadStatus();
    beginPolling();
  }),
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#detail-drawer").hidden && !$("#modal").open)
    closeDrawer();
  if (
    e.key === "/" &&
    !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) &&
    !$("#modal").open &&
    !$("#library-page").hidden
  ) {
    e.preventDefault();
    $("#search").focus();
  }
  if (e.key === "Tab" && !$("#detail-drawer").hidden && !$("#modal").open) {
    const focusable = $$(
      "#detail-drawer button, #detail-drawer input, #detail-drawer textarea, #detail-drawer select, #detail-drawer a",
    ).filter((el) => !el.disabled);
    const first = focusable[0],
      last = focusable.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});
function beginPolling() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const before = state.status?.indexed_at;
      const status = await loadStatus();
      if (status.indexed_at !== before || (!state.total && status.total))
        await loadFonts();
      if (status.indexing) beginPolling();
    } catch (error) {
      errorMessage(error);
    }
  }, 1800);
}
async function initialize() {
  await loadStatus();
  await loadFonts();
  if (location.pathname === "/settings") await showSettings(false);
  if (state.status.indexing || !state.status.total) beginPolling();
}
initialize().catch((error) => {
  $("#error-banner").hidden = false;
  $("#error-banner").textContent = error.message;
  toast(error.message);
});
