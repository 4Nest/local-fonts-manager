const test = require("node:test");
const assert = require("node:assert/strict");
const {
  MAX_FILE_SIZE,
  filesFromSelection,
  collectEntries,
  filesFromDrop,
  UploadQueue,
} = require("../font_manager/static/upload-queue.js");

const font = (name, size = 12) => ({ name, size });
const entryFile = (name) => ({
  name, isFile: true, isDirectory: false,
  file(resolve) { resolve(font(name)); },
});
function directory(name, children, batchSize = 100) {
  return {
    name, isFile: false, isDirectory: true,
    createReader() {
      let offset = 0;
      return {
        readEntries(resolve) {
          const batch = children.slice(offset, offset + batchSize);
          offset += batchSize;
          resolve(batch);
        },
      };
    },
  };
}

test("递归读取同一目录的所有分页，并保留子目录中的同名文件", async () => {
  const entries = Array.from({ length: 205 }, (_, i) => entryFile(`${i}.ttf`));
  entries.push(directory("A", [entryFile("same.ttf")]));
  entries.push(directory("B", [entryFile("same.ttf")]));
  const result = await collectEntries([directory("字体", entries)]);
  assert.equal(result.length, 207);
  assert.equal(result[204].path, "字体/204.ttf");
  assert.equal(result[205].path, "字体/A/same.ttf");
  assert.equal(result[206].path, "字体/B/same.ttf");
  assert.equal(result[205].file.name, "same.ttf");
  assert.equal(result[206].file.name, "same.ttf");
});

test("目录读取失败保留原路径和原因，不丢弃其他目录", async () => {
  const result = await collectEntries([
    { name: "无权限", isDirectory: true, createReader() {
      return { readEntries(_, reject) { reject(new Error("denied")); } };
    } },
    directory("可读", [entryFile("a.otf")]),
  ]);
  assert.equal(result[0].path, "无权限");
  assert.match(result[0].error, /denied/);
  assert.equal(result[1].path, "可读/a.otf");
});

test("drop 在事件返回前获取全部文件句柄，兼容不支持目录 API 的普通文件", async () => {
  let protectedStore = false;
  const capture = (entry) => {
    assert.equal(protectedStore, false);
    return entry;
  };
  const promise = filesFromDrop({ items: [
    { kind: "file", webkitGetAsEntry: () => capture(directory("文件夹", [entryFile("a.ttf")])) },
    { kind: "file", getAsFile: () => capture(font("b.otf")) },
  ] });
  protectedStore = true;
  assert.deepEqual((await promise).map((item) => item.path), ["文件夹/a.ttf", "b.otf"]);
  assert.equal((await filesFromDrop({ files: [font("fallback.ttf")] }))[0].path, "fallback.ttf");
});

test("文件夹选择保留相对路径，混合内容逐项过滤并报告大小和读取错误", () => {
  const selected = font("same.TTF");
  selected.webkitRelativePath = "本地/A/same.TTF";
  const queue = new UploadQueue();
  queue.add(filesFromSelection([
    selected, font("readme.txt"), font("large.otf", MAX_FILE_SIZE + 1),
    font("exact.ttf", MAX_FILE_SIZE), font("empty.ttf", 0), font("web.woff2"),
  ]));
  queue.add([{ path: "不能读取/字体.ttf", error: "无法读取" }]);
  assert.equal(queue.items[0].path, "本地/A/same.TTF");
  assert.equal(queue.items[0].name, "same.TTF");
  assert.equal(queue.counts().pending, 3);
  assert.equal(queue.counts().skipped, 4);
  assert.match(queue.items[1].error, /格式/);
  assert.match(queue.items[2].error, /100 MiB/);
  assert.match(queue.items[4].error, /为空/);
});

test("顺序投递同名文件，失败后继续其他文件，重试只发送未成功项", async () => {
  const queue = new UploadQueue();
  queue.add([
    { path: "A/same.ttf", file: font("same.ttf") },
    { path: "B/same.ttf", file: font("same.ttf") },
    { path: "C/other.otf", file: font("other.otf") },
  ]);
  let active = 0;
  const sent = [];
  await queue.start(async (file, item) => {
    assert.equal(++active, 1);
    await Promise.resolve();
    sent.push([file.name, item.path]);
    active--;
    if (item.path.startsWith("B/")) throw new Error("429 请重试");
    return { accepted: true };
  });
  assert.deepEqual(sent, [["same.ttf", "A/same.ttf"], ["same.ttf", "B/same.ttf"], ["other.otf", "C/other.otf"]]);
  assert.equal(queue.counts().succeeded, 2);
  assert.equal(queue.counts().failed, 1);
  assert.match(queue.items[1].error, /429/);
  queue.retryUnfinished();
  const retries = [];
  await queue.start(async (_, item) => retries.push(item.path));
  assert.deepEqual(retries, ["B/same.ttf"]);
  assert.equal(queue.counts().succeeded, 3);
});

test("取消只影响尚未发出的文件，在途请求完成；继续时不会重复投递成功文件", async () => {
  const queue = new UploadQueue();
  queue.add(filesFromSelection([font("a.ttf"), font("b.ttf"), font("c.ttf")]));
  let finishFirst;
  const sent = [];
  const running = queue.start((file) => {
    sent.push(file.name);
    return new Promise((resolve) => { finishFirst = resolve; });
  });
  assert.equal(queue.running, true);
  queue.cancelPending();
  queue.clear(); // An active upload cannot accidentally lose its state.
  assert.equal(queue.items.length, 3);
  assert.equal(queue.counts().cancelled, 2);
  assert.equal(queue.counts().uploading, 1);
  finishFirst();
  await running;
  assert.equal(queue.running, false);
  assert.deepEqual(sent, ["a.ttf"]);
  queue.retryUnfinished();
  await queue.start(async (file) => sent.push(file.name));
  assert.deepEqual(sent, ["a.ttf", "b.ttf", "c.ttf"]);
  assert.equal(queue.counts().succeeded, 3);
});
