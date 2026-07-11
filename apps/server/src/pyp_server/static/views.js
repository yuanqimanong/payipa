/* 管理界面共用渲染助手（M5 真数据页）：从 /api/views/* 拉 JSON → 渲染表格 / 统计卡。
   与 dashboard.html 的单元格约定一致（badge/mono/list/bool/time），额外处理 401/403/空/错误态。 */
(function () {
  const BADGE = {
    active: "green", online: "green", success: "green", done: "green", sent: "green", approved: "green",
    running: "blue", queued: "blue", assigned: "blue", inflight: "blue", testing: "amber", draft: "amber",
    cooling: "amber", review: "amber", paused: "red", pending: "amber", disabled: "red", offline: "red",
    failed: "red", dead: "red", error: "red",
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function cell(row, col) {
    let v = row[col.key];
    if (col.type === "badge") {
      if (v == null || v === "") return "<td class='muted'>—</td>";
      const cls = BADGE[String(v).toLowerCase()] || "";
      const display = col.labels && col.labels[v] ? col.labels[v] : v;
      return `<td><span class="badge ${cls}"><span class="dot"></span>${esc(display)}</span></td>`;
    }
    if (col.type === "meter") {
      const ratio = Math.max(0, Math.min(1, Number(v) || 0));
      const percent = (ratio * 100).toFixed(1);
      const tone = ratio >= 0.95 ? "good" : (ratio >= 0.8 ? "warn" : "bad");
      return `<td><div class="health-meter ${tone}" title="${percent}%"><span style="width:${percent}%"></span></div>` +
        `<small class="meter-label">${percent}%</small></td>`;
    }
    if (col.type === "bool") return `<td>${v ? "✓" : "<span class='muted'>—</span>"}</td>`;
    if (col.type === "mono") return v ? `<td><code>${esc(v)}</code></td>` : "<td class='muted'>—</td>";
    if (col.type === "list") {
      const items = Array.isArray(v) ? v : [];
      return `<td>${items.length ? items.map((x) => `<span class="pill">${esc(x)}</span>`).join(" ") : "<span class='muted'>—</span>"}</td>`;
    }
    if (col.type === "time") return v ? `<td class="muted">${esc(String(v).replace("T", " "))}</td>` : "<td class='muted'>—</td>";
    if (v == null || v === "") return "<td class='muted'>—</td>";
    return `<td>${esc(v)}</td>`;
  }

  function renderTable(el, columns, rows) {
    const head = "<tr>" + columns.map((c) => `<th>${esc(c.label)}</th>`).join("") + "</tr>";
    const body = rows.length
      ? rows.map((r) => "<tr>" + columns.map((c) => cell(r, c)).join("") + "</tr>").join("")
      : `<tr><td colspan="${columns.length}"><div class="empty">暂无数据</div></td></tr>`;
    el.innerHTML = `<div class="table-wrap"><table class="tbl"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  }

  function renderStats(el, stats) {
    el.innerHTML = stats
      .map((s) => `<div class="stat"><div class="k">${esc(s.k)}</div><div class="v">${esc(s.v)}</div>` +
        (s.d ? `<div class="d ${esc(s.dir || "")}">${esc(s.d)}</div>` : "") + "</div>")
      .join("");
  }

  async function fetchJSON(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (resp.status === 401) throw new Error("未登录或会话过期，请重新登录");
    if (resp.status === 403) throw new Error("无权限查看（需相应角色，见角色权限页）");
    if (!resp.ok) throw new Error(`加载失败（HTTP ${resp.status}）`);
    return resp.json();
  }

  /* 一步到位：拉数据 → 渲染表格；失败在容器里显示错误条。columns 为 [{key,label,type?}]。 */
  async function loadTable(el, url, columns, pick) {
    try {
      const data = await fetchJSON(url);
      const rows = pick ? pick(data) : data;
      renderTable(el, columns, rows);
    } catch (e) {
      el.innerHTML = `<div class="empty err">${esc(e.message)}</div>`;
    }
  }

  window.PypViews = { renderTable, renderStats, fetchJSON, loadTable, cell, esc };
})();
