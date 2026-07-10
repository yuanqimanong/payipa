// payipa 点选取数：自选择器生成（自研，借鉴 @medv/finder 思路，不引 node 构建链 / 无外部依赖）。
// 产出「尽量短且唯一」的 CSS 选择器 + 元素的绝对 XPath。逻辑全部内置（遵守商店远程代码禁令）。
(function (global) {
  "use strict";

  // 属性稳定性加权（借鉴旧 Xpath-Tool 思路）：id / data-testid 高、class 中、随机短哈希类降权。
  const BAD_CLASS = /^(?:[a-z]+-)?[a-f0-9]{5,}$|^(?:css|sc|jsx|_)-|\d{4,}/i;

  function isUnique(sel, root) {
    try {
      return (root || global.document).querySelectorAll(sel).length === 1;
    } catch {
      return false;
    }
  }

  function cssEscape(v) {
    const c = global.CSS;
    return c && c.escape ? c.escape(v) : String(v).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }

  function goodClasses(el) {
    return Array.from(el.classList || []).filter((c) => c && !BAD_CLASS.test(c));
  }

  // 单节点候选选择器（优先 id → 稳定属性 → tag.class → tag），返回最短唯一者或 tag:nth-of-type。
  function segment(el) {
    const tag = el.tagName.toLowerCase();
    if (el.id && isUnique("#" + cssEscape(el.id))) return "#" + cssEscape(el.id);
    for (const attr of ["data-testid", "data-test", "name", "aria-label"]) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) {
        const sel = `${tag}[${attr}="${cssEscape(v)}"]`;
        if (isUnique(sel)) return sel;
      }
    }
    const classes = goodClasses(el);
    if (classes.length) {
      const sel = tag + "." + classes.map(cssEscape).join(".");
      if (isUnique(sel)) return sel;
    }
    // 退化到 tag:nth-of-type（相对父节点稳定定位）
    const parent = el.parentElement;
    if (!parent) return tag;
    const sameTag = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
    if (sameTag.length === 1) return tag;
    return `${tag}:nth-of-type(${sameTag.indexOf(el) + 1})`;
  }

  // 从 el 向上拼接路径，直到唯一或到 body。产出最短唯一 CSS 选择器。
  function cssSelector(el) {
    if (!el || el.nodeType !== 1) return ""; // nodeType 判元素（跨 iframe/realm 比 instanceof 稳）
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== "html") {
      const seg = segment(cur);
      parts.unshift(seg);
      const candidate = parts.join(" > ");
      if (seg.startsWith("#") || isUnique(candidate)) return candidate;
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  // 绝对 XPath（带 tag[n] 索引），逻辑简单稳定。
  function xpath(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return `//*[@id="${el.id}"]`;
    const segs = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
      const tag = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (!parent) {
        segs.unshift("/" + tag);
        break;
      }
      const sameTag = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
      const idx = sameTag.length > 1 ? `[${sameTag.indexOf(cur) + 1}]` : "";
      segs.unshift(`/${tag}${idx}`);
      cur = parent;
    }
    return segs.join("");
  }

  global.payipaSelector = { cssSelector, xpath };
})(typeof window !== "undefined" ? window : globalThis);
