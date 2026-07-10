// 自选择器生成的离线冒烟（node --test，纯 stub DOM，无 jsdom / 无 node 构建链）。
// 验证 XPath 索引 + CSS id 快路径 + tag:nth-of-type 退化。真浏览器点选冒烟见 README。
//   运行：node --test extensions/picker/test/selector.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";

// 加载 selector.js（它挂到传入的 global 对象上）
const src = readFileSync(new URL("../selector.js", import.meta.url), "utf8");

function makeEl({ tag, id = "", classes = [], attrs = {}, children = [] }) {
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id,
    classList: classes,
    parentElement: null,
    children,
    getAttribute: (k) => attrs[k] ?? null,
  };
  for (const c of children) c.parentElement = el;
  return el;
}

function loadSelector(uniqueSelectors) {
  // 构造一个假 global，selector.js 会把 payipaSelector 挂上去
  const fakeWin = {
    CSS: { escape: (s) => s },
    document: { querySelectorAll: (sel) => ({ length: uniqueSelectors.has(sel) ? 1 : 0 }) },
  };
  const g = { window: fakeWin };
  // selector.js 以 (window||globalThis) 为 global；用 Function 注入 window
  new Function("window", src)(fakeWin);
  return fakeWin.payipaSelector;
}

test("XPath 带 tag[n] 索引（兄弟同 tag 时）", () => {
  const li1 = makeEl({ tag: "li" });
  const li2 = makeEl({ tag: "li" });
  const ul = makeEl({ tag: "ul", children: [li1, li2] });
  const body = makeEl({ tag: "body", children: [ul] });
  makeEl({ tag: "html", children: [body] });
  const sel = loadSelector(new Set());
  assert.equal(sel.xpath(li2), "/html/body/ul/li[2]");
});

test("XPath: 有 id 直接锚定", () => {
  const el = makeEl({ tag: "div", id: "main" });
  const sel = loadSelector(new Set());
  assert.equal(sel.xpath(el), '//*[@id="main"]');
});

test("CSS: id 唯一走快路径", () => {
  const el = makeEl({ tag: "div", id: "hero" });
  const sel = loadSelector(new Set(["#hero"]));
  assert.equal(sel.cssSelector(el), "#hero");
});

test("CSS: 稳定 class 唯一即用 tag.class", () => {
  const el = makeEl({ tag: "p", classes: ["price"] });
  const body = makeEl({ tag: "body", children: [el] });
  makeEl({ tag: "html", children: [body] });
  const sel = loadSelector(new Set(["p.price"]));
  assert.equal(sel.cssSelector(el), "p.price");
});

test("CSS: 随机哈希类被降权，退化到 nth-of-type 路径", () => {
  const p1 = makeEl({ tag: "p", classes: ["a1b2c3d4"] });
  const p2 = makeEl({ tag: "p", classes: ["a1b2c3d4"] });
  const div = makeEl({ tag: "div", classes: ["sc-1a2b3c"], children: [p1, p2] });
  const body = makeEl({ tag: "body", children: [div] });
  makeEl({ tag: "html", children: [body] });
  // 没有任何候选唯一 → 逐级拼接到根
  const sel = loadSelector(new Set());
  const out = sel.cssSelector(p2);
  assert.match(out, /p:nth-of-type\(2\)$/); // 末段用 nth-of-type 定位第二个 p
  assert.ok(!out.includes("a1b2c3d4"), "随机哈希类应被降权，不进选择器");
});
