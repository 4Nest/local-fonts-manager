/* File traversal and upload state are independent of the dialog's lifetime. */
(function (root) {
  "use strict";

  const MAX_FILE_SIZE = 100 * 1024 * 1024;
  const FONT_EXTENSION = /\.(ttf|otf|ttc|otc|woff|woff2)$/i;

  function filesFromSelection(files) {
    return Array.from(files, (file) => ({
      file,
      path: file.webkitRelativePath || file.name,
    }));
  }

  async function collectEntries(entries, onProgress = () => {}) {
    const found = [];
    async function visit(entry, parent = "") {
      const path = parent ? `${parent}/${entry.name}` : entry.name;
      try {
        if (entry.isFile) {
          const file = await new Promise((resolve, reject) =>
            entry.file(resolve, reject),
          );
          found.push({ file, path });
          onProgress(found.length);
        } else if (entry.isDirectory) {
          const reader = entry.createReader();
          // Chromium returns directory entries in batches (often 100 at a time).
          // Keep the same reader until an empty batch, including nested folders.
          while (true) {
            const batch = await new Promise((resolve, reject) =>
              reader.readEntries(resolve, reject),
            );
            if (!batch.length) break;
            for (const child of batch) await visit(child, path);
          }
        }
      } catch (error) {
        found.push({ path, error: `无法读取：${error.message || error}` });
        onProgress(found.length);
      }
    }
    for (const entry of entries) await visit(entry);
    return found;
  }

  function filesFromDrop(transfer, onProgress) {
    // Capture entries/files synchronously: a drop's data store becomes protected
    // as soon as its event handler returns or first yields.
    const entries = [];
    const files = [];
    for (const item of Array.from(transfer.items || [])) {
      if (item.kind !== "file") continue;
      const entry = item.webkitGetAsEntry?.() || item.getAsEntry?.();
      if (entry) entries.push(entry);
      else {
        const file = item.getAsFile?.();
        if (file) files.push(file);
      }
    }
    if (!entries.length && !files.length)
      files.push(...Array.from(transfer.files || []));
    return collectEntries(entries, onProgress).then((found) => [
      ...found,
      ...filesFromSelection(files),
    ]);
  }

  class UploadQueue {
    constructor(onChange = () => {}) {
      this.items = [];
      this.running = false;
      this.onChange = onChange;
      this.nextId = 1;
    }

    add(descriptors) {
      if (this.running) throw new Error("请等待当前投递结束后再添加文件");
      for (const { file, path, error } of descriptors) {
        let reason = error || "";
        if (file && !FONT_EXTENSION.test(file.name)) reason = "非支持的字体格式";
        else if (file && file.size > MAX_FILE_SIZE)
          reason = "超过 100 MiB，请通过 SMB 投递";
        else if (file && file.size === 0) reason = "文件为空";
        this.items.push({
          id: this.nextId++,
          file: reason ? null : file,
          path: path || file?.name || "未知文件",
          name: file?.name,
          size: file?.size || 0,
          status: reason ? "skipped" : "pending",
          error: reason,
        });
      }
      this.onChange();
    }

    counts() {
      const counts = {
        pending: 0, uploading: 0, succeeded: 0,
        failed: 0, skipped: 0, cancelled: 0,
      };
      for (const item of this.items) counts[item.status]++;
      return counts;
    }

    cancelPending() {
      for (const item of this.items)
        if (item.status === "pending") item.status = "cancelled";
      this.onChange();
    }

    retryUnfinished() {
      if (this.running) return;
      for (const item of this.items)
        if (item.status === "failed" || item.status === "cancelled") {
          item.status = "pending";
          item.error = "";
        }
      this.onChange();
    }

    clear() {
      if (this.running) return;
      this.items = [];
      this.onChange();
    }

    async start(send) {
      if (this.running) return;
      this.running = true;
      this.onChange();
      try {
        // One request at a time bounds memory and avoids flooding the NAS.
        for (const item of this.items) {
          if (item.status !== "pending") continue;
          item.status = "uploading";
          this.onChange();
          try {
            item.result = await send(item.file, item);
            item.status = "succeeded";
            item.file = null;
          } catch (error) {
            item.status = "failed";
            item.error = error.message || String(error);
          }
          this.onChange();
        }
      } finally {
        this.running = false;
        this.onChange();
      }
    }
  }

  const api = { MAX_FILE_SIZE, filesFromSelection, collectEntries, filesFromDrop, UploadQueue };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FontImport = api;
})(globalThis);
