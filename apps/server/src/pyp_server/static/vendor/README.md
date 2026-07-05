# Vendored 前端静态资源（无 CDN，离线/内网友好）

SSR 栈（06 定案）的前端库以 vendor 静态文件随包分发，不依赖 CDN。

| 库 | 版本 | 文件 | 来源 |
|---|---|---|---|
| Tabulator | 6.3.1 | `tabulator/tabulator.min.{js,css}` | jsdelivr `tabulator-tables@6.3` |
| htmx | 2.0.x | `htmx/htmx.min.js` | jsdelivr `htmx.org@2.0` |

## 更新流程（06 §5 遗留）

```bash
V=6.3.1
curl -sL -o tabulator/tabulator.min.css "https://cdn.jsdelivr.net/npm/tabulator-tables@${V}/dist/css/tabulator.min.css"
curl -sL -o tabulator/tabulator.min.js  "https://cdn.jsdelivr.net/npm/tabulator-tables@${V}/dist/js/tabulator.min.js"
curl -sL -o htmx/htmx.min.js            "https://cdn.jsdelivr.net/npm/htmx.org@2.0/dist/htmx.min.js"
```

更新后本表登记版本；升级前查 changelog（Tabulator 6.x / htmx 2.x 均承诺稳定）。
