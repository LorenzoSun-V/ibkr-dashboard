const $ = (s) => document.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const state = {
  meta: null,
  account: store.get("account", ""),     // "" = 全部账户
  range: store.get("range", "ytd"),
  custom: store.get("custom", { start: "", end: "" }),
  view: "month",                          // month | year
  metric: store.get("metric", "pnl"),     // pnl | ret
  cursor: null,                           // 当前查看的月份 Date(yyyy, mm, 1)
  selected: null,                         // 选中的日期 "YYYY-MM-DD"
  daily: new Map(),
  months: new Map(),
  years: new Map(),
};

// ---------- 格式化 ----------
function fmtMoney(v, { sign = true, compact = true } = {}) {
  if (v == null || isNaN(v)) return "--";
  const s = sign && v > 0 ? "+" : v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (compact && a >= 1e8) return `${s}${(a / 1e8).toFixed(2)} 亿`;
  if (compact && a >= 1e4) return `${s}${(a / 1e4).toFixed(2)} 万`;
  if (compact && a >= 1e3) return `${s}${(a / 1e3).toFixed(2)}K`;
  return `${s}${a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtFull(v, sign = true) {
  if (v == null || isNaN(v)) return "--";
  const s = sign && v > 0 ? "+" : "";
  return s + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(v) {
  if (v == null || isNaN(v)) return "--";
  return `${v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const accountName = (id) => {
  const a = state.meta?.accounts.find((x) => x.id === id);
  return a?.alias ? `${a.alias} (${id})` : id;
};

// ---------- API ----------
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}
const acctParam = () => (state.account ? `accounts=${encodeURIComponent(state.account)}` : "");

async function loadMeta() {
  state.meta = await api("/api/meta");
  const m = state.meta;
  document.querySelectorAll(".ccy").forEach((el) => (el.textContent = m.display_currency));
  $("#updated").textContent = m.last_date ? `数据更新至：${m.last_date.replaceAll("-", ".")}` : "暂无数据";
  const lf = m.last_fetch;
  $("#footInfo").textContent = lf ? `上次拉取 ${lf.at.replace("T", " ")} ${lf.ok ? "✓" : "✗"}` : "";

  const banner = $("#banner");
  if (m.demo) {
    banner.textContent = "演示模式：当前显示的是模拟数据。";
  } else if (!m.configured) {
    banner.textContent = "尚未配置 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID，请参照 README 填写 .env 后重启服务。";
  } else if (!m.last_date) {
    banner.textContent = "还没有数据，点击右上角刷新按钮从 IBKR 拉取。";
  } else if (lf && !lf.ok) {
    banner.textContent = `上次拉取失败：${lf.message}`;
  } else banner.textContent = "";
  banner.classList.toggle("hidden", !banner.textContent);
  $("#refreshBtn").classList.toggle("spinning", m.fetching);

  if (state.account && !m.accounts.some((a) => a.id === state.account)) state.account = "";
  const chips = [{ id: "", label: "全部账户" }, ...m.accounts.map((a) => ({ id: a.id, label: a.alias || a.id }))];
  $("#accountChips").innerHTML = chips
    .map((c) => `<button data-acct="${c.id}" class="${c.id === state.account ? "active" : ""}">${c.label}</button>`)
    .join("");
  $("#accountChips").classList.toggle("hidden", m.accounts.length < 2);
}

async function loadData() {
  const q = acctParam();
  const [daily, months, years] = await Promise.all([
    api(`/api/daily?${q}`),
    api(`/api/periods?by=month&${q}`),
    api(`/api/periods?by=year&${q}`),
  ]);
  state.daily = new Map(daily.rows.map((r) => [r.date, r]));
  state.months = new Map(months.map((r) => [r.period, r]));
  state.years = new Map(years.map((r) => [r.period, r]));
  if (daily.fx_missing.length) {
    const b = $("#banner");
    b.textContent = `缺少汇率：${daily.fx_missing.join(", ")}，请在 Flex Query 中勾选 Conversion Rates。`;
    b.classList.remove("hidden");
  }
  if (!state.cursor) {
    const last = state.meta.last_date ? new Date(state.meta.last_date + "T00:00") : new Date();
    state.cursor = new Date(last.getFullYear(), last.getMonth(), 1);
  }
}

// ---------- 区间汇总 ----------
function rangeBounds() {
  const last = state.meta?.last_date ? new Date(state.meta.last_date + "T00:00") : new Date();
  const d = new Date(last);
  switch (state.range) {
    case "1m": d.setMonth(d.getMonth() - 1); return [iso(d), iso(last)];
    case "6m": d.setMonth(d.getMonth() - 6); return [iso(d), iso(last)];
    case "1y": d.setFullYear(d.getFullYear() - 1); return [iso(d), iso(last)];
    case "ytd": return [`${last.getFullYear()}-01-01`, iso(last)];
    case "custom": return [state.custom.start || "", state.custom.end || ""];
    default: return ["", ""];
  }
}

async function renderSummary() {
  document.querySelectorAll("#rangeChips button").forEach((b) => b.classList.toggle("active", b.dataset.range === state.range));
  $("#customRange").classList.toggle("hidden", state.range !== "custom");
  $("#customStart").value = state.custom.start;
  $("#customEnd").value = state.custom.end;

  let [start, end] = rangeBounds();
  // "近 N 月" 的起点当天不算：区间盈亏从起点的下一个交易日开始
  if (["1m", "6m", "1y"].includes(state.range)) {
    const s = new Date(start + "T00:00"); s.setDate(s.getDate() + 1); start = iso(s);
  }
  const q = [acctParam(), start && `start=${start}`, end && `end=${end}`].filter(Boolean).join("&");
  const s = await api(`/api/summary?${q}`);

  const set = (id, html, c = "") => { const el = $(id); el.innerHTML = html; el.className = c; };
  if (!s) {
    $("#rangeLabel").textContent = "该区间没有数据";
    ["#sumPnl", "#sumRet", "#winDays", "#lossDays", "#bestDay", "#worstDay", "#netFlow", "#endNav"].forEach((id) => set(id, "--"));
    $("#spark").innerHTML = "";
    return;
  }
  $("#rangeLabel").textContent = `${s.start} ~ ${s.end} · ${s.days} 个交易日`;
  set("#sumPnl", fmtFull(s.pnl), "big " + cls(s.pnl));
  set("#sumRet", fmtPct(s.twr), "mid " + cls(s.twr));
  set("#winDays", `${s.win_days} <small>${((s.win_days / s.days) * 100).toFixed(0)}%</small>`);
  set("#lossDays", `${s.loss_days}`);
  set("#bestDay", `${fmtMoney(s.best.pnl)} <small>${s.best.date.slice(5)}</small>`, cls(s.best.pnl));
  set("#worstDay", `${fmtMoney(s.worst.pnl)} <small>${s.worst.date.slice(5)}</small>`, cls(s.worst.pnl));
  set("#netFlow", fmtMoney(s.net_flow));
  set("#endNav", fmtMoney(s.end_nav, { sign: false }));
  renderSpark(s.start, s.end);
}

function renderSpark(start, end) {
  const svg = $("#spark");
  const rows = [...state.daily.values()].filter((r) => r.date >= start && r.date <= end);
  if (rows.length < 2) { svg.innerHTML = ""; return; }
  const W = 500, H = 56;
  // 收益：累计盈亏；收益率：累计 TWR
  let acc = 0, g = 1;
  const pts = [0, ...rows.map((r) => (state.metric === "ret" ? (g *= 1 + (r.ret || 0)) - 1 : (acc += r.pnl)))];
  const min = Math.min(...pts), max = Math.max(...pts), span = max - min || 1;
  const x = (i) => (i / (pts.length - 1)) * W;
  const y = (v) => H - 4 - ((v - min) / span) * (H - 8);
  const line = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const color = pts[pts.length - 1] >= 0 ? "var(--up)" : "var(--down)";
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="var(--line)" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>
    <path d="${line}L${W},${H}L0,${H}Z" fill="${color}" opacity=".12"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.8" vector-effect="non-scaling-stroke"/>`;
}

// ---------- 日历 ----------
function cellStyle(v, maxAbs) {
  if (v == null || v === 0) return "";
  const t = Math.min(Math.abs(v) / (maxAbs || 1), 1);
  const alpha = (0.14 + 0.3 * Math.sqrt(t)).toFixed(3);
  return `background: rgba(var(${v > 0 ? "--up-bg" : "--down-bg"}), ${alpha})`;
}
const cellValue = (r) => (state.metric === "pnl" ? fmtMoney(r.pnl) : fmtPct(r.ret));
const metricOf = (r) => (state.metric === "pnl" ? r.pnl : r.ret);

function renderCalendar() {
  document.querySelectorAll("#viewSeg button").forEach((b) => b.classList.toggle("active", b.dataset.v === state.view));
  document.querySelectorAll("#metricSeg button").forEach((b) => b.classList.toggle("active", b.dataset.v === state.metric));
  const y = state.cursor.getFullYear(), m = state.cursor.getMonth();
  const cal = $("#calendar");

  if (state.view === "month") {
    $("#periodText").textContent = `${y}/${pad(m + 1)}`;
    $("#monthInput").value = `${y}-${pad(m + 1)}`;
    $("#monthInput").classList.remove("hidden");
    $("#yearInput").classList.add("hidden");

    const first = new Date(y, m, 1).getDay();
    const days = new Date(y, m + 1, 0).getDate();
    const rows = [];
    for (let d = 1; d <= days; d++) rows.push(state.daily.get(`${y}-${pad(m + 1)}-${pad(d)}`));
    const maxAbs = Math.max(0, ...rows.filter(Boolean).map((r) => Math.abs(metricOf(r) || 0)));
    const today = iso(new Date());

    let html = "日一二三四五六".split("").map((w) => `<div class="wd">${w}</div>`).join("");
    for (let i = 0; i < first; i++) html += `<div class="cell empty"></div>`;
    for (let d = 1; d <= days; d++) {
      const key = `${y}-${pad(m + 1)}-${pad(d)}`;
      const r = rows[d - 1];
      const c = ["cell", r && "has", key === today && "today", key === state.selected && "sel"].filter(Boolean).join(" ");
      html += r
        ? `<div class="${c}" data-date="${key}" style="${cellStyle(metricOf(r), maxAbs)}"><span class="d">${pad(d)}</span><span class="v ${cls(metricOf(r))}">${cellValue(r)}</span></div>`
        : `<div class="${c}"><span class="d">${pad(d)}</span></div>`;
    }
    cal.innerHTML = `<div class="grid">${html}</div>`;
    renderTotal(state.months.get(`${y}-${pad(m + 1)}`), "本月");
  } else {
    $("#periodText").textContent = `${y}`;
    const years = [...state.years.keys()];
    const opts = new Set([...years, String(y)]);
    $("#yearInput").innerHTML = [...opts].sort().map((v) => `<option ${v == y ? "selected" : ""}>${v}</option>`).join("");
    $("#yearInput").classList.remove("hidden");
    $("#monthInput").classList.add("hidden");

    const months = Array.from({ length: 12 }, (_, i) => state.months.get(`${y}-${pad(i + 1)}`));
    const maxAbs = Math.max(0, ...months.filter(Boolean).map((r) => Math.abs(metricOf(r) || 0)));
    cal.innerHTML = `<div class="year-grid">${months.map((r, i) => r
      ? `<div class="cell has" data-month="${i}" style="${cellStyle(metricOf(r), maxAbs)}"><span class="d">${i + 1}月</span><span class="v ${cls(metricOf(r))}">${cellValue(r)}</span></div>`
      : `<div class="cell" data-month="${i}"><span class="d">${i + 1}月</span></div>`).join("")}</div>`;
    renderTotal(state.years.get(String(y)), "全年");
  }
  renderDayDetail();
}

function renderTotal(p, label) {
  $("#periodTotal").innerHTML = p
    ? `<span>${label}盈亏 <b class="${cls(p.pnl)}">${fmtFull(p.pnl)}</b></span><span>收益率 <b class="${cls(p.ret)}">${fmtPct(p.ret)}</b></span>`
    : `<span class="label">${label}暂无数据</span>`;
}

function renderDayDetail() {
  const box = $("#dayDetail");
  const r = state.selected && state.view === "month" && state.daily.get(state.selected);
  if (!r) { box.classList.add("hidden"); return; }
  const accts = Object.entries(r.accounts);
  box.innerHTML = `
    <h3>${state.selected}</h3>
    <div class="row"><span>当日盈亏</span><b class="${cls(r.pnl)}">${fmtFull(r.pnl)}</b></div>
    <div class="row"><span>当日收益率</span><b class="${cls(r.ret)}">${fmtPct(r.ret)}</b></div>
    <div class="row"><span>总资产</span><b>${fmtFull(r.nav, false)}</b></div>
    ${r.flow ? `<div class="row"><span>当日净入金（已剔除）</span><b>${fmtFull(r.flow)}</b></div>` : ""}
    ${accts.length > 1 ? accts.map(([id, a]) => `
      <div class="row"><span>${accountName(id)}</span><span><b class="${cls(a.pnl)}">${fmtFull(a.pnl)}</b> <small class="${cls(a.ret)}">${fmtPct(a.ret)}</small></span></div>`).join("") : ""}`;
  box.classList.remove("hidden");
}

// ---------- 事件 ----------
function bind() {
  $("#accountChips").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.account = b.dataset.acct; store.set("account", state.account);
    document.querySelectorAll("#accountChips button").forEach((x) => x.classList.toggle("active", x === b));
    await loadData(); renderCalendar(); renderSummary();
  });
  $("#rangeChips").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.range = b.dataset.range; store.set("range", state.range);
    if (state.range === "custom" && !state.custom.start) {
      const [s, en] = [state.meta.first_date || "", state.meta.last_date || ""];
      state.custom = { start: s, end: en };
    }
    renderSummary();
  });
  $("#customApply").addEventListener("click", () => {
    state.custom = { start: $("#customStart").value, end: $("#customEnd").value };
    store.set("custom", state.custom); renderSummary();
  });
  $("#viewSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.view = b.dataset.v; renderCalendar();
  });
  $("#metricSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.metric = b.dataset.v; store.set("metric", state.metric); renderCalendar(); renderSummary();
  });
  const shift = (n) => {
    const c = state.cursor;
    state.cursor = state.view === "month" ? new Date(c.getFullYear(), c.getMonth() + n, 1) : new Date(c.getFullYear() + n, c.getMonth(), 1);
    renderCalendar();
  };
  $("#prevBtn").addEventListener("click", () => shift(-1));
  $("#nextBtn").addEventListener("click", () => shift(1));
  $("#monthInput").addEventListener("change", (e) => {
    const [y, m] = e.target.value.split("-").map(Number);
    if (y && m) { state.cursor = new Date(y, m - 1, 1); renderCalendar(); }
  });
  $("#yearInput").addEventListener("change", (e) => {
    state.cursor = new Date(Number(e.target.value), state.cursor.getMonth(), 1); renderCalendar();
  });
  $("#calendar").addEventListener("click", (e) => {
    const day = e.target.closest("[data-date]");
    if (day) { state.selected = state.selected === day.dataset.date ? null : day.dataset.date; renderCalendar(); return; }
    const mon = e.target.closest("[data-month]");
    if (mon) { state.cursor = new Date(state.cursor.getFullYear(), Number(mon.dataset.month), 1); state.view = "month"; renderCalendar(); }
  });
  // 手机上左右滑动切换月份/年份
  let x0 = null;
  $("#calendar").addEventListener("touchstart", (e) => (x0 = e.touches[0].clientX), { passive: true });
  $("#calendar").addEventListener("touchend", (e) => {
    if (x0 == null) return;
    const dx = e.changedTouches[0].clientX - x0; x0 = null;
    if (Math.abs(dx) > 60) shift(dx < 0 ? 1 : -1);
  });

  const redUp = store.get("redUp", false);
  $("#redUp").checked = redUp; document.body.classList.toggle("red-up", redUp);
  $("#redUp").addEventListener("change", (e) => { store.set("redUp", e.target.checked); document.body.classList.toggle("red-up", e.target.checked); });

  $("#refreshBtn").addEventListener("click", async () => {
    try {
      await api("/api/refresh", { method: "POST" });
      $("#refreshBtn").classList.add("spinning");
      pollFetch();
    } catch (err) { alert(err.message); }
  });
}

async function pollFetch() {
  await new Promise((r) => setTimeout(r, 3000));
  await loadMeta();
  if (state.meta.fetching) return pollFetch();
  await loadData(); renderCalendar(); renderSummary();
}

// 支持通过 URL 参数打开指定视图，例如 ?view=year&metric=ret&range=1y&day=2026-09-11
function applyUrlParams() {
  const p = new URLSearchParams(location.search);
  if (["month", "year"].includes(p.get("view"))) state.view = p.get("view");
  if (["pnl", "ret"].includes(p.get("metric"))) state.metric = p.get("metric");
  if (["1m", "6m", "ytd", "1y", "all"].includes(p.get("range"))) state.range = p.get("range");
  const day = p.get("day");
  if (/^\d{4}-\d{2}-\d{2}$/.test(day || "")) {
    state.selected = day;
    state.cursor = new Date(+day.slice(0, 4), +day.slice(5, 7) - 1, 1);
  }
}

(async function init() {
  bind();
  applyUrlParams();
  try {
    await loadMeta();
    await loadData();
    renderCalendar();
    await renderSummary();
    if (state.meta.fetching) pollFetch();
  } catch (err) {
    $("#banner").textContent = `加载失败：${err.message}`;
    $("#banner").classList.remove("hidden");
  }
})();
