"use strict";

window.FontRecognition = (() => {
  const IMAGE_LIMIT = 12 * 1024 * 1024;
  const state = {
    file: null,
    objectURL: "",
    text: "",
    resultText: "",
    results: [],
    languages: [],
    languagesLoaded: false,
    jobId: null,
    active: false,
    starting: false,
    ocrRunning: false,
    pollTimer: null,
  };
  let ui;
  let initialized = false;

  const $ = (selector) => document.querySelector(selector);
  const escapeHTML = (value) => ui.escapeHTML(value);

  function status(message, error = false) {
    const element = $("#recognition-status");
    if (!element) return;
    element.textContent = message;
    element.classList.toggle("error", error);
  }

  function guard(action) {
    return async (event) => {
      try {
        await action(event);
      } catch (error) {
        if (error.name === "AbortError") return;
        status(error.message || String(error), true);
        ui.toast(error.message || String(error));
      }
    };
  }

  async function upload(path, fields) {
    const form = new FormData();
    form.append("file", state.file);
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    const response = await fetch(path, { method: "POST", body: form });
    const body = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : `请求失败（${response.status}）`,
      );
    return body;
  }

  function syncControls() {
    const busy = state.active || state.starting || state.ocrRunning;
    if ($("#recognition-progress") && !state.active)
      $("#recognition-progress").hidden = true;
    if ($("#recognition-file")) $("#recognition-file").disabled = busy;
    if ($("#recognition-text")) $("#recognition-text").disabled = state.active;
    if ($("#recognition-language"))
      $("#recognition-language").disabled = busy || !state.languages.length;
    if ($("#recognition-ocr")) {
      $("#recognition-ocr").disabled =
        busy || !state.file || !state.languages.length;
      $("#recognition-ocr").textContent = state.ocrRunning
        ? "正在 OCR…"
        : "识别文字";
    }
    if ($("#recognition-match")) {
      $("#recognition-match").disabled = busy || !state.file;
      $("#recognition-match").hidden = state.active;
    }
    if ($("#recognition-cancel")) $("#recognition-cancel").hidden = !state.active;
    if ($("#recognition-drop"))
      $("#recognition-drop").classList.toggle("disabled", busy);
  }

  function renderResults() {
    const container = $("#recognition-results");
    if (!container) return;
    if (!state.results.length) {
      container.innerHTML = state.resultText
        ? '<div class="empty-state">没有找到可比较的字体。请检查文字，或确认字体支持这些字符。</div>'
        : '<div class="empty-state">匹配完成后，这里会显示最相似的本地字体。</div>';
      return;
    }
    container.innerHTML = state.results
      .map(
        (row) => `<article class="recognition-result">
          <div class="recognition-result-heading"><div><h3>${escapeHTML(row.family)}</h3><p>${escapeHTML(row.style || row.filename)}${row.face ? ` · 字面 ${row.face + 1}` : ""}</p></div><strong>${Number(row.score).toFixed(1)}<small> 相似度</small></strong></div>
          ${ui.specimenHTML(row, state.resultText, "recognition-specimen", row.face)}
          <div class="recognition-result-footer"><span class="badge">${escapeHTML(row.format)}</span><button class="button" data-recognition-detail="${row.id}" data-face="${row.face}">查看详情</button></div>
        </article>`,
      )
      .join("");
    ui.observePreviews(container);
    container.querySelectorAll("[data-recognition-detail]").forEach((button) => {
      button.onclick = () => {
        const id = button.dataset.recognitionDetail;
        const face = Number(button.dataset.face);
        ui.closeModal();
        void ui.openDetails(id, face);
      };
    });
  }

  function render() {
    ui.showModal(
      "图片识别字体",
      `<div class="recognition-layout">
        <section class="recognition-input-panel">
          <div id="recognition-drop" class="recognition-drop" role="button" tabindex="0" aria-label="粘贴、拖放或选择文字图片">
            <input id="recognition-file" type="file" accept="image/png,image/jpeg,image/webp" hidden />
            <img id="recognition-preview" alt="待识别文字图片" hidden />
            <div id="recognition-placeholder"><strong>粘贴图片，或点击选择</strong><span>也可以把 PNG、JPEG、WebP 拖到这里 · 最大 12 MiB</span></div>
          </div>
          <div id="recognition-filename" class="small-note"></div>
          <div class="recognition-ocr-row"><label for="recognition-language">OCR 语言</label><select id="recognition-language" class="field"></select><button id="recognition-ocr" class="button">识别文字</button></div>
          <label class="field-label" for="recognition-text">匹配文字 · 请核对 OCR 结果</label>
          <textarea id="recognition-text" class="field" maxlength="100" rows="3" placeholder="OCR 后可修改，也可以直接手动输入"></textarea>
          <div class="recognition-actions"><button id="recognition-match" class="button primary">开始全库匹配</button><button id="recognition-cancel" class="button" hidden>取消任务</button></div>
          <progress id="recognition-progress" hidden></progress>
          <div id="recognition-status" class="recognition-status" role="status">图片和文字只在本机 / NAS 处理，不会发送到第三方服务。</div>
        </section>
        <section class="recognition-results-panel"><div class="recognition-results-title"><h3>相似字体候选</h3><span>按字形轮廓启发式排序</span></div><div id="recognition-results" class="recognition-results"></div></section>
      </div>
      <p class="small-note recognition-hint">建议截取清晰、同一字体的文字区域。缩放和反色可处理；复杂背景、描边阴影、透视、混排及可变字体非默认轴会降低准确度。相似度不是确定性的字体鉴定结论。</p>`,
      true,
    );
    const textarea = $("#recognition-text");
    textarea.value = state.text;
    textarea.addEventListener("input", () => {
      state.text = textarea.value;
      state.results = [];
      state.resultText = "";
      renderResults();
    });
    if (state.file) {
      const preview = $("#recognition-preview");
      preview.src = state.objectURL;
      preview.hidden = false;
      $("#recognition-placeholder").hidden = true;
      $("#recognition-filename").textContent = state.file.name || "剪贴板图片";
    }
    $("#recognition-file").addEventListener("change", (event) => {
      if (event.target.files[0]) setImage(event.target.files[0]);
      event.target.value = "";
    });
    const drop = $("#recognition-drop");
    drop.addEventListener("click", () => {
      if (!state.active && !state.starting && !state.ocrRunning) $("#recognition-file").click();
    });
    drop.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        drop.click();
      }
    });
    for (const name of ["dragenter", "dragover"])
      drop.addEventListener(name, (event) => {
        event.preventDefault();
        if (!state.active && !state.starting && !state.ocrRunning) drop.classList.add("drag-active");
      });
    drop.addEventListener("dragleave", () => drop.classList.remove("drag-active"));
    drop.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      drop.classList.remove("drag-active");
      if (state.active || state.starting || state.ocrRunning) return;
      const file = [...event.dataTransfer.files].find((item) =>
        item.type.startsWith("image/"),
      );
      if (file) setImage(file);
      else status("请拖放图片文件", true);
    });
    $("#recognition-ocr").onclick = guard(runOCR);
    $("#recognition-match").onclick = guard(startMatching);
    $("#recognition-cancel").onclick = guard(cancelMatching);
    renderLanguages();
    renderResults();
    syncControls();
  }

  function setImage(file) {
    if (
      !["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
      file.size > IMAGE_LIMIT
    ) {
      status("请选择不超过 12 MiB 的 PNG、JPEG 或 WebP 图片", true);
      return;
    }
    if (state.objectURL) URL.revokeObjectURL(state.objectURL);
    state.file = file;
    state.objectURL = URL.createObjectURL(file);
    state.text = "";
    state.results = [];
    state.resultText = "";
    render();
    status("图片已就绪。先 OCR，或手动输入文字后直接匹配。");
  }

  function renderLanguages() {
    const select = $("#recognition-language");
    if (!select) return;
    if (!state.languages.length) {
      select.innerHTML = '<option value="">OCR 不可用</option>';
      return;
    }
    const installed = new Set(state.languages.map((item) => item.code));
    const options = [];
    for (const item of state.languages) {
      options.push({ code: item.code, label: item.label });
      if (item.code !== "eng" && installed.has("eng"))
        options.push({ code: `${item.code}+eng`, label: `${item.label} + English` });
    }
    const preferred = installed.has("chi_sim") && installed.has("eng")
      ? "chi_sim+eng"
      : state.languages[0].code;
    select.innerHTML = options
      .map(
        (item) => `<option value="${item.code}" ${item.code === preferred ? "selected" : ""}>${escapeHTML(item.label)}</option>`,
      )
      .join("");
  }

  async function loadLanguages() {
    if (state.languagesLoaded) return;
    state.languagesLoaded = true;
    try {
      const result = await ui.api("/api/recognition/languages");
      state.languages = result.languages;
      renderLanguages();
      if (!result.available)
        status("当前未安装 OCR；仍可手动输入文字进行字体匹配。", true);
    } catch (error) {
      state.languages = [];
      state.languagesLoaded = false;
      renderLanguages();
      status(`${error.message}；仍可手动输入文字进行匹配。`, true);
    }
    syncControls();
  }

  async function runOCR() {
    if (!state.file) throw new Error("请先粘贴或选择图片");
    state.ocrRunning = true;
    syncControls();
    status("正在本地识别图片文字…");
    try {
      const result = await upload("/api/recognition/ocr", {
        language: $("#recognition-language").value,
      });
      state.text = result.text;
      if ($("#recognition-text")) $("#recognition-text").value = result.text;
      state.results = [];
      state.resultText = "";
      renderResults();
      status(
        result.text
          ? `OCR 完成，置信度 ${result.confidence}%。请核对文字后开始匹配。`
          : "没有识别到文字，请手动输入或更换更清晰的截图。",
        !result.text,
      );
    } finally {
      state.ocrRunning = false;
      syncControls();
    }
  }

  function progress(job) {
    const element = $("#recognition-progress");
    if (!element) return;
    element.hidden = !state.active;
    if (job.total > 0) {
      element.max = job.total;
      element.value = job.processed;
    } else {
      element.removeAttribute("max");
      element.removeAttribute("value");
    }
    const counts = `${job.phase} · ${job.processed}/${job.total || "…"} · 已比较 ${job.matched} 个字面`;
    status(counts);
  }

  async function poll(jobId) {
    clearTimeout(state.pollTimer);
    if (
      !state.active ||
      state.jobId !== jobId ||
      !$("#modal").open ||
      !$("#recognition-progress")
    )
      return;
    try {
      const job = await ui.api(`/api/recognition/jobs/${jobId}`);
      if (state.jobId !== jobId) return;
      progress(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        state.active = false;
        state.jobId = null;
        state.results = job.results;
        if (job.status === "completed") {
          state.resultText = state.text;
          status(
            `匹配完成：比较 ${job.matched} 个字面；跳过 ${job.skipped_missing} 个缺字字面、${job.skipped_errors} 个异常。`,
          );
        } else if (job.status === "failed") {
          status(job.error || "识别失败", true);
        } else status("识别任务已取消");
        renderResults();
        syncControls();
        return;
      }
      state.pollTimer = setTimeout(() => void poll(jobId), 600);
    } catch (error) {
      const failedJob = state.jobId;
      state.active = false;
      state.jobId = null;
      if (failedJob)
        void ui
          .api(`/api/recognition/jobs/${failedJob}`, { method: "DELETE" })
          .catch(() => {});
      syncControls();
      status(error.message, true);
    }
  }

  async function startMatching() {
    const text = $("#recognition-text").value.trim();
    if (!state.file) throw new Error("请先粘贴或选择图片");
    if (!text) throw new Error("请填写需要匹配的文字");
    state.starting = true;
    syncControls();
    try {
      state.text = text;
      state.results = [];
      state.resultText = "";
      renderResults();
      const job = await upload("/api/recognition/jobs", { text });
      if (!$("#modal").open || !$("#recognition-progress")) {
        await ui.api(`/api/recognition/jobs/${job.id}`, { method: "DELETE" });
        return;
      }
      state.jobId = job.id;
      state.active = true;
      progress(job);
      await poll(job.id);
    } finally {
      state.starting = false;
      syncControls();
    }
  }

  async function cancelMatching() {
    if (!state.jobId) return;
    status("正在取消识别任务…");
    await ui.api(`/api/recognition/jobs/${state.jobId}`, { method: "DELETE" });
  }

  function onModalClose() {
    clearTimeout(state.pollTimer);
    if (state.active && state.jobId)
      void ui
        .api(`/api/recognition/jobs/${state.jobId}`, { method: "DELETE" })
        .catch(() => {});
    state.active = false;
    state.jobId = null;
  }

  async function show(context) {
    ui = context;
    if (!initialized) {
      initialized = true;
      document.addEventListener("paste", (event) => {
        if (!$("#modal").open || !$("#recognition-drop")) return;
        const item = [...(event.clipboardData?.items || [])].find(
          (entry) => entry.kind === "file" && entry.type.startsWith("image/"),
        );
        const file = item?.getAsFile();
        if (!file) return;
        event.preventDefault();
        if (!state.active && !state.starting && !state.ocrRunning) setImage(file);
      });
      $("#modal").addEventListener("close", onModalClose);
      window.addEventListener("beforeunload", () => {
        if (state.objectURL) URL.revokeObjectURL(state.objectURL);
      });
    }
    render();
    await loadLanguages();
  }

  return { show };
})();
