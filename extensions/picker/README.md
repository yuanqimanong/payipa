# payipa 点选取数（浏览器插件）

在目标网页上**点选元素**，自动生成 CSS/XPath 回填到 payipa 数据源配置页。对应 [docs/10-浏览器插件.md](../../docs/10-浏览器插件.md) 定案。

## 架构（10 §4）

**B「平台 UI 页 ↔ 扩展浏览器内直通」**：`content script + window.postMessage` 桥，**零服务器新增面、无配对流程**。
信任边界 = origin 校验；平台 ↔ 插件**只传数据**（选择器结果），不传可执行规则（遵守商店远程代码禁令）。

```
配置页(◎按钮) --postMessage(start)--> content.js --runtime--> background(service worker)
                                                                     |  用户切到目标网站标签
                                                          tabs.sendMessage(startPicker)
                                                                     v
目标页 content.js: 高亮覆盖层 → 点击 → selector.js 生成 css/xpath --runtime(picked)--> background
                                                                     |
配置页 <--postMessage(picked)-- content.js <--tabs.sendMessage(result)-- background --> 填入 field_css
```

## 组成

| 文件 | 作用 |
|---|---|
| `manifest.json` | MV3；content_scripts 注入所有页；service worker 协调 |
| `selector.js` | 自研选择器生成（最短唯一 CSS + 绝对 XPath；随机哈希类降权）。无外部依赖、不引 node 构建链 |
| `content.js` | 双角色：平台页 postMessage 桥 / 目标页点选覆盖层（高亮 + 点击 + Esc 取消）|
| `background.js` | service worker：武装 → 用户切目标标签启动点选 → 结果回平台标签 |
| `test/selector.test.mjs` | 选择器算法离线冒烟（`node --test`，纯 stub DOM，无需浏览器）|

## 装载（开发者模式，未上架商店 · 10 §4-4）

1. Chrome/Edge 打开 `chrome://extensions`，开「开发者模式」。
2. 「加载已解压的扩展程序」→ 选本目录 `extensions/picker`。
3. 打开 payipa 配置页（`/sources/new`）与目标网站（各一个标签）。
4. 配置页某字段点「◎」→ 切到目标网站标签 → 页面出现「点选模式」提示 → 点击要采集的元素。
5. 生成的 CSS 选择器自动回填到该字段（Esc 取消）。

## 测试

- **算法离线冒烟**（无需浏览器）：`node --test extensions/picker/test/selector.test.mjs`
- **真浏览器端到端**：按上「装载」步骤手动点选（本插件是前端制品，不入 Python CI/uv workspace，见 10 §4-2）。

## 约束

- v1 仅 Chromium 系（Chrome/Edge 同一份代码）；Firefox 复用同一 postMessage 桥、留待真实需求（10 §4-3）。
- 逻辑全部内置（远程代码禁令）；平台与插件只传数据，绝不下发可执行规则/脚本。
- 正式分发（商店上架 vs 企业策略）产品化阶段定（10 §5）。
