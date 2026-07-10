// payipa 点选取数 · service worker（协调：平台页武装 → 用户切到目标页 → 点选 → 结果回平台页）。
// MV3：不假设常驻，状态落 chrome.storage.session；事件驱动。
// 流程：
//   1) 平台页 content.js 发 {type:'arm', field}（sender.tab.id = 平台标签）→ 记 armed。
//   2) 用户切到目标网站标签（tabs.onActivated，且非平台标签）→ 给该标签发 startPicker。
//   3) 目标页点选后发 {type:'picked', css, xpath} → 转发回平台标签的 content.js（type:'result'）→ 平台页 postMessage 填字段。
"use strict";

const KEY = "payipa_armed"; // {field, platformTabId}

async function getArmed() {
  const o = await chrome.storage.session.get(KEY);
  return o[KEY] || null;
}
async function setArmed(v) {
  if (v) await chrome.storage.session.set({ [KEY]: v });
  else await chrome.storage.session.remove(KEY);
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  (async () => {
    if (msg.type === "arm") {
      const platformTabId = sender.tab && sender.tab.id;
      await setArmed({ field: msg.field, platformTabId });
      // 若当前已有另一个活动标签（目标页），立即在其上启动点选
      const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (active && active.id !== platformTabId) sendStartPicker(active.id, msg.field);
    } else if (msg.type === "picked") {
      const armed = await getArmed();
      await setArmed(null);
      if (armed && armed.platformTabId != null) {
        chrome.tabs
          .sendMessage(armed.platformTabId, { type: "result", field: msg.field, css: msg.css, xpath: msg.xpath })
          .catch(() => {});
      }
    } else if (msg.type === "pickCanceled") {
      const armed = await getArmed();
      await setArmed(null);
      if (armed && armed.platformTabId != null) {
        chrome.tabs.sendMessage(armed.platformTabId, { type: "canceled", field: msg.field }).catch(() => {});
      }
    }
  })();
  return false;
});

// 武装期间用户切到目标标签 → 启动点选（一次性：startPicker 发出后不清 armed，等 picked/cancel 才清）。
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const armed = await getArmed();
  if (armed && tabId !== armed.platformTabId) sendStartPicker(tabId, armed.field);
});

function sendStartPicker(tabId, field) {
  chrome.tabs.sendMessage(tabId, { type: "startPicker", field }).catch(() => {});
}
