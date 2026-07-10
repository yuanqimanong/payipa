// payipa 点选取数 · content script（10 定案：content script + window.postMessage 桥）。
// 双角色（同一份脚本，跑在所有页）：
//   ① 平台配置页角色：收平台页 postMessage「开始点选」→ 通知 background 武装；收 background「结果」→ postMessage 回平台页填字段。
//   ② 目标网页角色：收 background「启动点选」→ 注入高亮覆盖层，点击生成 CSS/XPath → 回 background。
// 信任边界 = origin 校验（只认平台页 postMessage 的 payipa 协议）；平台↔插件只传数据，不传可执行规则（远程代码禁令）。
(function () {
  "use strict";

  const REQ = "payipa-picker-req"; // 平台页 → 插件（开始点选）
  const RES = "payipa-picker-res"; // 插件 → 平台页（点选结果 / 取消）

  // ── 角色①：平台配置页 postMessage 桥 ──────────────────────────────────────
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || ev.data.channel !== REQ) return; // 只认同源本页的请求
    const { type, field } = ev.data;
    if (type === "start") {
      chrome.runtime.sendMessage({ type: "arm", field: String(field || "") });
    }
  });

  // background 把点选结果/取消回递给平台页 → 转成 postMessage 供页面 JS 填字段
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "result") {
      window.postMessage({ channel: RES, type: "picked", field: msg.field, css: msg.css, xpath: msg.xpath }, window.origin);
    } else if (msg && msg.type === "canceled") {
      window.postMessage({ channel: RES, type: "canceled", field: msg.field }, window.origin);
    } else if (msg && msg.type === "startPicker") {
      startPicker(msg.field);
    }
  });

  // ── 角色②：目标网页点选覆盖层 ───────────────────────────────────────────────
  let active = null; // {field, box, tip, onMove, onClick, onKey, hovered}

  function makeBox() {
    const box = document.createElement("div");
    box.style.cssText =
      "position:fixed;z-index:2147483647;pointer-events:none;border:2px solid #2563eb;" +
      "background:rgba(37,99,235,.12);border-radius:2px;transition:all .03s ease;display:none";
    const tip = document.createElement("div");
    tip.style.cssText =
      "position:fixed;z-index:2147483647;pointer-events:none;top:8px;left:50%;transform:translateX(-50%);" +
      "background:#111827;color:#fff;font:12px/1.4 system-ui,sans-serif;padding:6px 12px;border-radius:6px;" +
      "box-shadow:0 2px 8px rgba(0,0,0,.3)";
    tip.textContent = "payipa 点选：点击要采集的元素，Esc 取消";
    document.documentElement.append(box, tip);
    return { box, tip };
  }

  function startPicker(field) {
    if (active) return; // 已在点选中
    const { box, tip } = makeBox();

    const onMove = (e) => {
      const el = document.elementFromPoint(e.clientX, e.clientY);
      if (!el || el === box || el === tip) return;
      active.hovered = el;
      const r = el.getBoundingClientRect();
      box.style.display = "block";
      box.style.left = r.left + "px";
      box.style.top = r.top + "px";
      box.style.width = r.width + "px";
      box.style.height = r.height + "px";
    };
    const onClick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const el = active.hovered || document.elementFromPoint(e.clientX, e.clientY);
      const css = window.payipaSelector.cssSelector(el);
      const xpath = window.payipaSelector.xpath(el);
      teardown();
      chrome.runtime.sendMessage({ type: "picked", field, css, xpath });
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        teardown();
        chrome.runtime.sendMessage({ type: "pickCanceled", field });
      }
    };

    function teardown() {
      document.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKey, true);
      box.remove();
      tip.remove();
      active = null;
    }

    active = { field, box, tip, teardown };
    document.addEventListener("mousemove", onMove, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
  }
})();
