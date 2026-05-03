"""
report.py — Generates a self-contained HTML performance report from a detail CSV.

Usage:
    python report.py                          # latest detail_*.csv in report/
    python report.py report/detail_xyz.csv   # specific file
    python report.py --browse                 # interactive file picker (requires gum)
"""

import sys
import math
import base64
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from itertools import combinations

REPORT_DIR     = Path(__file__).parent / "report"
P95_TARGET_MS  = 2000

# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#1e293b;background:#f8fafc}
.wrap{max-width:1280px;margin:0 auto;padding:24px}
.hdr{background:#0f172a;color:#fff;padding:20px 28px;border-radius:10px;margin-bottom:24px;display:flex;align-items:center;gap:28px;flex-wrap:wrap}
.hdr h1{font-size:16px;font-weight:700;flex:1;letter-spacing:.3px}
.stat{text-align:center;min-width:80px}
.stat .v{font-size:20px;font-weight:700}
.stat .l{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;margin-top:2px}

/* ── Tabs ── */
.tabs{display:flex;gap:0;border-bottom:2px solid #e2e8f0;margin-bottom:24px}
.tab-btn{padding:10px 22px;font-size:13px;font-weight:600;color:#64748b;background:none;border:none;
         border-bottom:2px solid transparent;margin-bottom:-2px;cursor:pointer;letter-spacing:.2px;
         transition:color .15s,border-color .15s}
.tab-btn:hover{color:#0f172a}
.tab-btn.active{color:#0f172a;border-bottom-color:#0f172a}
.tab-pane{display:none}
.tab-pane.active{display:block}

h2{font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin:28px 0 8px}
.legend{font-size:12px;color:#94a3b8;margin-bottom:10px}
.legend .red{color:#ef4444}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:4px}
th{background:#f1f5f9;color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:9px 14px;text-align:left;cursor:pointer;user-select:none;white-space:nowrap}
th:after{content:' ⇅';opacity:.3}
th:hover{background:#e2e8f0}
td{padding:9px 14px;border-top:1px solid #f1f5f9;vertical-align:top}
tr:hover td{background:#f8fafc}
.chart{background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:4px;padding:4px}
.pill{display:inline-block;background:#e2e8f0;color:#475569;font-size:11px;padding:2px 7px;border-radius:10px;margin:1px 2px 1px 0}
.notes-box{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#78350f;white-space:pre-wrap}
.dl{text-align:center;margin:28px 0 12px}
.dl a{display:inline-block;padding:9px 22px;background:#0f172a;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px}
.dl a:hover{background:#1e293b}
footer{text-align:center;color:#cbd5e1;font-size:11px;padding:20px 0}
"""

# ── JS ────────────────────────────────────────────────────────────────────────

_JS = """
// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(target).classList.add('active');
  });
});

// Sortable tables
document.querySelectorAll('th').forEach(th => {
  th.addEventListener('click', () => {
    const tbl = th.closest('table');
    const idx = [...th.parentNode.children].indexOf(th);
    const asc = th.dataset.asc !== '1';
    th.dataset.asc = asc ? '1' : '';
    const rows = [...tbl.querySelectorAll('tbody tr')];
    rows.sort((a, b) => {
      const av = a.cells[idx].textContent.trim();
      const bv = b.cells[idx].textContent.trim();
      const an = parseFloat(av.replace(/[^0-9.-]/g, ''));
      const bn = parseFloat(bv.replace(/[^0-9.-]/g, ''));
      if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(r => tbl.querySelector('tbody').appendChild(r));
  });
});
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_deps():
    missing = []
    try:
        import pandas  # noqa: F401
    except ImportError:
        missing.append("pandas")
    try:
        import plotly  # noqa: F401
    except ImportError:
        missing.append("plotly")
    if missing:
        raise ImportError(
            f"HTML report requires: {', '.join(missing)}  —  "
            f"run: pip install {' '.join(missing)}"
        )


def _pct(series, p: float) -> int:
    if len(series) == 0:
        return 0
    return int(series.quantile(p))


def _pills(items) -> str:
    return "".join(f'<span class="pill">{i}</span>' for i in sorted(items))


def _gfile_pick(start: str = ".") -> str:
    """Use gum file browser to pick a CSV. Returns path or ''."""
    try:
        r = subprocess.run(["gum", "file", start],
                           text=True, stdout=subprocess.PIPE, encoding="utf-8")
        return r.stdout.strip()
    except FileNotFoundError:
        return ""


def _find_knee(values: list) -> int:
    """Perpendicular-distance knee detection. Returns index into values, or -1."""
    n = len(values)
    if n < 3:
        return -1
    x0, y0 = 0.0, float(values[0])
    x1, y1 = float(n - 1), float(values[n - 1])
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return -1
    distances = [
        abs(dy * i - dx * float(values[i]) + x1 * y0 - y1 * x0) / length
        for i in range(n)
    ]
    return distances.index(max(distances))


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_report(csv_path: Path, p95_target: int = P95_TARGET_MS,
                    notes: str = "",
                    response_timeout: float = 30.0,
                    silence_timeout: float = 15.0,
                    events_csv: "Path | None" = None) -> Path:
    _require_deps()
    import pandas as pd
    import plotly.graph_objects as go

    # ── Load ──────────────────────────────────────────────────────────────────
    df = pd.read_csv(csv_path)
    df["response_ms"] = pd.to_numeric(df["response_ms"], errors="coerce").fillna(0)
    df["timed_out"]   = pd.to_numeric(df["timed_out"],   errors="coerce").fillna(0).astype(int)

    # Normalise column names
    if "profile" not in df.columns:
        df["profile"] = df.get("scenario", "unknown")
    if "scenario" not in df.columns:
        df["scenario"] = "default"

    # Strip spawn number (#N) for summary-level grouping
    df["base_profile"] = df["profile"].str.replace(r'\s*#\d+$', '', regex=True).str.strip()

    has_user_count = "user_count" in df.columns
    if has_user_count:
        df["user_count"] = pd.to_numeric(df["user_count"], errors="coerce").fillna(0).astype(int)

    df_ok = df[df["timed_out"] == 0].copy()

    has_ts = "utterance_sent_at" in df.columns
    if has_ts:
        df["sent_dt"]    = pd.to_datetime(df["utterance_sent_at"], utc=True, errors="coerce")
        df_ok["sent_dt"] = df.loc[df_ok.index, "sent_dt"]

    # ── Top-level aggregates ──────────────────────────────────────────────────
    total_reqs  = len(df)
    total_tout  = int(df["timed_out"].sum())
    error_rate  = total_tout / max(1, total_reqs) * 100
    overall_p95 = _pct(df_ok["response_ms"], 0.95)
    passed      = overall_p95 <= p95_target

    if has_ts and not df["sent_dt"].isna().all():
        t_start       = df["sent_dt"].min()
        t_end         = df["sent_dt"].max()
        duration_s    = int((t_end - t_start).total_seconds())
        test_date_str = t_start.strftime("%Y-%m-%d %H:%M UTC")
        duration_str  = f"{duration_s // 60}m {duration_s % 60}s"
    else:
        test_date_str = csv_path.stem.replace("detail_", "")
        duration_str  = "—"

    # ── Ramp steps (Tab 1) — one row per 60-second window ────────────────────
    ramp_steps_html = ""
    if has_ts and not df["sent_dt"].isna().all():
        RAMP_WINDOW = 60.0
        t0_ramp = df["sent_dt"].min()
        df["_ramp_idx"] = ((df["sent_dt"] - t0_ramp).dt.total_seconds() // RAMP_WINDOW).astype(int)

        # Load events CSV (optional) — group by ramp (1-based)
        events_by_ramp: dict = {}
        _eff_events_csv = events_csv
        if _eff_events_csv is None:
            # Auto-derive: events_YYYYMMDD_HHMMSS.csv alongside detail_YYYYMMDD_HHMMSS.csv
            _stem = csv_path.stem.replace("detail_", "events_")
            _candidate = csv_path.parent / f"{_stem}.csv"
            if _candidate.exists():
                _eff_events_csv = _candidate
        if _eff_events_csv is not None and Path(_eff_events_csv).exists():
            try:
                import csv as _csv_mod
                with open(_eff_events_csv, newline="", encoding="utf-8") as _ef:
                    for _row in _csv_mod.DictReader(_ef):
                        _r = int(_row.get("ramp", 0))
                        events_by_ramp.setdefault(_r, []).append(_row)
            except Exception:
                pass

        # Build per-ramp 429 count from events
        ramp_429: dict = {}
        for _r, _evs in events_by_ramp.items():
            ramp_429[_r] = sum(1 for e in _evs if e.get("event_type") == "rate_limit")

        step_data = []
        for ridx, grp in df.groupby("_ramp_idx"):
            ms_grp = grp[grp["timed_out"] == 0]["response_ms"]
            tout   = int(grp["timed_out"].sum())
            count  = len(grp)
            last_u = int(grp["user_count"].iloc[-1]) if has_user_count else 0
            rps_v  = count / RAMP_WINDOW
            p50v   = _pct(ms_grp, 0.50)
            p95v   = _pct(ms_grp, 0.95)
            p99v   = _pct(ms_grp, 0.99)
            ramp_num = int(ridx) + 1
            step_data.append(dict(
                ramp=ramp_num, users=last_u, count=count, rps=rps_v,
                p50=p50v, p95=p95v, p99=p99v, tout=tout,
                rate_limited=ramp_429.get(ramp_num, 0),
                events=events_by_ramp.get(ramp_num, []),
            ))

        # Knee detection on p95 series
        _DIRECTLINE_RPS_CAP = 133.0
        knee_ramp = -1
        if len(step_data) >= 3:
            knee_idx = _find_knee([s["rps"] for s in step_data])
            if knee_idx >= 0 and step_data[knee_idx]["rps"] >= _DIRECTLINE_RPS_CAP * 0.75:
                knee_ramp = step_data[knee_idx]["ramp"]

        step_rows = []
        for s in step_data:
            is_knee   = (s["ramp"] == knee_ramp)
            past_knee = (knee_ramp >= 0 and s["ramp"] > knee_ramp)
            if is_knee:
                row_style = ' style="background:#fefce8;border-left:3px solid #eab308"'
            elif past_knee:
                row_style = ' style="background:#fff7f7"'
            else:
                row_style = ""
            p95s = ' style="color:#ef4444;font-weight:700"' if s["p95"] > p95_target else (
                   ' style="color:#eab308;font-weight:700"' if is_knee else "")
            toc  = f'<span style="color:#ef4444">{s["tout"]}</span>' if s["tout"] else "0"
            rlc  = f'<span style="color:#ef4444;font-weight:700">{s["rate_limited"]}</span>' if s["rate_limited"] else "0"
            knee_label = " ◀ knee" if is_knee else ("  !" if past_knee else "")
            step_rows.append(
                f'<tr{row_style}>'
                f'<td><strong>{s["ramp"]}</strong>{knee_label}</td>'
                f'<td>{s["users"]}</td>'
                f'<td>{s["count"]:,}</td>'
                f'<td>{s["rps"]:.2f}</td>'
                f'<td>{s["p50"]:,}</td><td{p95s}>{s["p95"]:,}</td><td>{s["p99"]:,}</td>'
                f'<td>{toc}</td>'
                f'<td>{rlc}</td>'
                f'</tr>'
            )
            # Event sub-rows
            for ev in s["events"]:
                icon = ev.get("icon", "")
                msg  = ev.get("message", "")
                ts_e = ev.get("timestamp", "")
                etype = ev.get("event_type", "")
                if etype == "rate_limit":
                    ec = "color:#ef4444"
                elif etype == "cpu_warn":
                    ec = "color:#f59e0b"
                elif etype == "timeout":
                    ec = "color:#ef4444"
                else:
                    ec = "color:#94a3b8"
                step_rows.append(
                    f'<tr><td colspan="9" style="padding:2px 14px 2px 28px;font-size:12px;{ec}">'
                    f'{ts_e}&nbsp;&nbsp;{icon}&nbsp;&nbsp;{msg}</td></tr>'
                )
        if step_rows:
            ramp_steps_html = (
                '<h2>Ramp Steps</h2>'
                '<p class="legend">'
                'One row per 60-second window &nbsp;&middot;&nbsp;'
                'USERS = last user count in that window &nbsp;&middot;&nbsp;'
                'RPS / p50 / p95 / p99 are for that window only &nbsp;&middot;&nbsp;'
                '<span style="color:#eab308;font-weight:600">◀ knee</span> = RPS plateaus here — adding users stops increasing throughput'
                '</p>'
                '<table><thead><tr>'
                '<th>Ramp</th><th>Users</th><th>Requests</th><th>RPS</th>'
                '<th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>T/O</th><th>Throttle</th>'
                '</tr></thead><tbody>' + "".join(step_rows) + '</tbody></table>'
            )

    # ── Profile Summary ───────────────────────────────────────────────────────
    profile_rows_data = []
    for profile, grp in df.groupby("base_profile"):
        ms       = grp[grp["timed_out"] == 0]["response_ms"]
        tout     = int(grp["timed_out"].sum())
        reqs     = len(grp)
        p95v     = _pct(ms, 0.95)
        scenarios = sorted(grp["scenario"].unique().tolist())
        profile_rows_data.append(dict(
            profile=profile, scenarios=scenarios, requests=reqs,
            p50=_pct(ms, 0.50), p95=p95v, p99=_pct(ms, 0.99),
            timeouts=tout, error_pct=f"{tout/max(1,reqs)*100:.1f}%",
            ok=p95v <= p95_target,
        ))

    # ── Scenario Breakdown ────────────────────────────────────────────────────
    scenario_rows_data = []
    for scenario, grp in df.groupby("scenario"):
        ms       = grp[grp["timed_out"] == 0]["response_ms"]
        tout     = int(grp["timed_out"].sum())
        reqs     = len(grp)
        p95v     = _pct(ms, 0.95)
        profiles_for_scenario = sorted(grp["base_profile"].unique().tolist())
        scenario_rows_data.append(dict(
            scenario=scenario, profiles=profiles_for_scenario, requests=reqs,
            p50=_pct(ms, 0.50), p95=p95v, p99=_pct(ms, 0.99),
            timeouts=tout, error_pct=f"{tout/max(1,reqs)*100:.1f}%",
            ok=p95v <= p95_target,
        ))

    # ── Per-utterance stats ───────────────────────────────────────────────────
    utterances = []
    for (utt, scenario), grp in df.groupby(["utterance", "scenario"]):
        ms       = grp[grp["timed_out"] == 0]["response_ms"]
        tout     = int(grp["timed_out"].sum())
        reqs     = len(grp)
        profiles_for_utt = sorted(grp["base_profile"].unique().tolist())

        anomalies = 0
        if len(ms) >= 4:
            med = ms.median()
            mad = (ms - med).abs().median()
            if mad > 0:
                anomalies = int((ms > med + 3 * mad).sum())

        p999 = None
        log_v = ms[ms > 0].apply(math.log)
        if len(log_v) >= 10:
            mu, sigma = log_v.mean(), log_v.std()
            if sigma > 0:
                p999 = int(math.exp(mu + 3.09 * sigma))

        utterances.append(dict(
            utterance=str(utt), scenario=scenario,
            profiles=profiles_for_utt,
            requests=reqs,
            p50=_pct(ms, 0.50), p95=_pct(ms, 0.95), p99=_pct(ms, 0.99),
            timeouts=tout, timeout_pct=f"{tout/max(1,reqs)*100:.1f}%",
            anomalies=anomalies, p999=p999,
        ))
    utterances.sort(key=lambda x: x["p95"], reverse=True)

    # ── Chart: box/whisker per profile ────────────────────────────────────────
    box_fig = go.Figure()
    for profile, grp in df_ok.groupby("base_profile"):
        box_fig.add_trace(go.Box(
            y=grp["response_ms"].tolist(), name=profile,
            boxpoints="outliers", marker_size=4,
        ))
    box_fig.add_hline(
        y=p95_target, line_dash="dash", line_color="#ef4444",
        annotation_text=f"p95 target ({p95_target:,}ms)",
        annotation_position="top right",
    )
    box_fig.update_layout(
        yaxis_title="Response time (ms)", template="plotly_white",
        height=380, margin=dict(t=20, b=40),
    )
    box_html = box_fig.to_html(include_plotlyjs=True, full_html=False)

    # ── Chart: box/whisker per scenario ───────────────────────────────────────
    scen_fig = go.Figure()
    for scenario, grp in df_ok.groupby("scenario"):
        scen_fig.add_trace(go.Box(
            y=grp["response_ms"].tolist(), name=scenario,
            boxpoints="outliers", marker_size=4,
        ))
    scen_fig.add_hline(
        y=p95_target, line_dash="dash", line_color="#ef4444",
        annotation_text=f"p95 target ({p95_target:,}ms)",
        annotation_position="top right",
    )
    scen_fig.update_layout(
        yaxis_title="Response time (ms)", template="plotly_white",
        height=380, margin=dict(t=20, b=40),
    )
    scen_html = scen_fig.to_html(include_plotlyjs=False, full_html=False)

    # ── Chart: latency heatmap ────────────────────────────────────────────────
    heatmap_html = ""
    if has_ts and "sent_dt" in df_ok.columns and not df_ok["sent_dt"].isna().all():
        t0    = df_ok["sent_dt"].min()
        df_hm = df_ok.copy()
        df_hm["bucket"]    = ((df_hm["sent_dt"] - t0).dt.total_seconds() // 30).astype(int)
        df_hm["utt_short"] = df_hm["utterance"].str[:40]
        pivot = (
            df_hm.groupby(["utt_short", "bucket"])["response_ms"]
            .median()
            .unstack(fill_value=0)
        )
        hm_fig = go.Figure(go.Heatmap(
            z=pivot.values.tolist(),
            x=[f"+{int(c * 30)}s" for c in pivot.columns],
            y=pivot.index.tolist(),
            colorscale="RdYlGn_r",
            colorbar=dict(title="ms"),
        ))
        hm_fig.update_layout(
            xaxis_title="Time into test", template="plotly_white",
            height=max(300, len(pivot.index) * 28 + 80),
            margin=dict(t=20, b=40, l=280),
        )
        hm_html_inner = hm_fig.to_html(include_plotlyjs=False, full_html=False)
        heatmap_html = (
            '<h2>Latency Heatmap — Utterance × Time</h2>'
            '<p class="legend">Median response time per 30-second window — '
            'rising colour signals degradation over time</p>'
            f'<div class="chart">{hm_html_inner}</div>'
        )

    # ── Profile comparison ────────────────────────────────────────────────────
    comparison_html = ""
    if len(profile_rows_data) >= 2:
        medians = {
            p["profile"]: _pct(
                df_ok[df_ok["profile"] == p["profile"]]["response_ms"], 0.50
            )
            for p in profile_rows_data
        }
        rows = []
        for a, b in combinations(medians.keys(), 2):
            ma, mb = medians[a], medians[b]
            if ma > 0:
                diff  = (mb - ma) / ma * 100
                badge = (
                    f'<span style="color:#ef4444">{abs(diff):.1f}% slower</span>'
                    if diff > 0 else
                    f'<span style="color:#22c55e">{abs(diff):.1f}% faster</span>'
                )
                rows.append(
                    f"<tr><td>{a}</td><td>{b}</td><td>{badge}</td>"
                    f"<td>{ma:,}ms</td><td>{mb:,}ms</td></tr>"
                )
        if rows:
            comparison_html = (
                '<h2>Profile Comparison</h2>'
                '<p class="legend">Median response time difference between profiles (p50)</p>'
                '<table><thead><tr>'
                '<th>Profile A</th><th>Profile B</th><th>Difference</th>'
                '<th>A median</th><th>B median</th>'
                '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
            )

    # ── Assemble HTML row builders ────────────────────────────────────────────
    def _profile_row(p):
        rs   = ' style="background:#fef2f2"' if not p["ok"] else ""
        p95s = ' style="color:#ef4444;font-weight:700"' if not p["ok"] else ""
        return (
            f'<tr{rs}>'
            f'<td><strong>{p["profile"]}</strong></td>'
            f'<td>{_pills(p["scenarios"])}</td>'
            f'<td>{p["requests"]:,}</td>'
            f'<td>{p["p50"]:,}</td>'
            f'<td{p95s}>{p["p95"]:,}</td>'
            f'<td>{p["p99"]:,}</td>'
            f'<td>{p["timeouts"]}</td>'
            f'<td>{p["error_pct"]}</td>'
            f'</tr>'
        )

    def _scenario_row(s):
        rs   = ' style="background:#fef2f2"' if not s["ok"] else ""
        p95s = ' style="color:#ef4444;font-weight:700"' if not s["ok"] else ""
        return (
            f'<tr{rs}>'
            f'<td><strong>{s["scenario"]}</strong></td>'
            f'<td>{_pills(s["profiles"])}</td>'
            f'<td>{s["requests"]:,}</td>'
            f'<td>{s["p50"]:,}</td>'
            f'<td{p95s}>{s["p95"]:,}</td>'
            f'<td>{s["p99"]:,}</td>'
            f'<td>{s["timeouts"]}</td>'
            f'<td>{s["error_pct"]}</td>'
            f'</tr>'
        )

    def _utt_row(u):
        dot  = ' <span title="MAD anomaly" style="color:#ef4444;font-size:10px">&#9679;</span>' if u["anomalies"] else ""
        p999 = f'{u["p999"]:,}' if u["p999"] else "—"
        p95s = ' style="color:#ef4444;font-weight:700"' if u["p95"] > p95_target else ""
        ts   = ' style="color:#ef4444"' if u["timeouts"] > 0 else ""
        lbl  = u["utterance"][:80] + ("…" if len(u["utterance"]) > 80 else "")
        return (
            f'<tr>'
            f'<td>{lbl}{dot}</td>'
            f'<td>{u["scenario"]}</td>'
            f'<td>{_pills(u["profiles"])}</td>'
            f'<td>{u["requests"]:,}</td>'
            f'<td>{u["p50"]:,}</td>'
            f'<td{p95s}>{u["p95"]:,}</td>'
            f'<td>{u["p99"]:,}</td>'
            f'<td{ts}>{u["timeouts"]} ({u["timeout_pct"]})</td>'
            f'<td style="color:#64748b">{p999}</td>'
            f'</tr>'
        )

    profile_rows_html  = "".join(_profile_row(p) for p in profile_rows_data)
    scenario_rows_html = "".join(_scenario_row(s) for s in scenario_rows_data)
    utt_rows_html      = "".join(_utt_row(u) for u in utterances)

    pass_badge = (
        '<span style="background:#22c55e;color:#fff;padding:4px 14px;border-radius:4px;font-weight:700">PASS</span>'
        if passed else
        '<span style="background:#ef4444;color:#fff;padding:4px 14px;border-radius:4px;font-weight:700">FAIL</span>'
    )
    csv_b64 = base64.b64encode(csv_path.read_bytes()).decode()

    notes_html = (
        f'<div class="notes-box"><strong>Test notes:</strong> {notes}</div>'
        if notes else ""
    )

    # ── Final HTML with tabs ──────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>GRUNTMASTER 6000 — {test_date_str}</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="wrap">

  <!-- Header stats bar -->
  <div class="hdr">
    <h1>GRUNTMASTER 6000 &nbsp;&middot;&nbsp; LOAD TEST REPORT</h1>
    <div class="stat"><div class="v">{test_date_str}</div><div class="l">Test date</div></div>
    <div class="stat"><div class="v">{total_reqs:,}</div><div class="l">Requests</div></div>
    <div class="stat"><div class="v">{duration_str}</div><div class="l">Duration</div></div>
    <div class="stat"><div class="v">{error_rate:.1f}%</div><div class="l">Error rate</div></div>
    <div class="stat"><div class="v">{overall_p95:,}ms</div><div class="l">p95</div></div>
    <div class="stat">{pass_badge}<div class="l" style="margin-top:4px">vs {p95_target:,}ms target</div></div>
    <div class="stat"><div class="v">{int(response_timeout)}s</div><div class="l">Reply timeout</div></div>
    <div class="stat"><div class="v">{int(silence_timeout)}s</div><div class="l">Silence window</div></div>
  </div>

  {notes_html}

  <!-- Tab bar -->
  <div class="tabs">
    <button class="tab-btn active" data-tab="tab-summary">Summary</button>
    <button class="tab-btn" data-tab="tab-distribution">Response Time Distribution</button>
    <button class="tab-btn" data-tab="tab-utterances">Utterance Analysis</button>
  </div>

  <!-- ── Tab 1: Summary ─────────────────────────────────────────────────── -->
  <div id="tab-summary" class="tab-pane active">

    {ramp_steps_html}

    <h2>Profile Summary</h2>
    <p class="legend">One row per user account — shows which scenarios each profile ran</p>
    <table>
      <thead><tr>
        <th>Profile</th><th>Scenarios</th><th>Requests</th>
        <th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>Timeouts</th><th>Error %</th>
      </tr></thead>
      <tbody>{profile_rows_html}</tbody>
    </table>

    <h2>Scenario Breakdown</h2>
    <p class="legend">One row per CSV script — shows which profile ran each scenario</p>
    <table>
      <thead><tr>
        <th>Scenario</th><th>Profiles</th><th>Requests</th>
        <th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>Timeouts</th><th>Error %</th>
      </tr></thead>
      <tbody>{scenario_rows_html}</tbody>
    </table>

    {comparison_html}

    <div class="dl">
      <a href="data:text/csv;base64,{csv_b64}" download="{csv_path.name}">
        &darr;&nbsp; Download raw CSV
      </a>
    </div>
  </div>

  <!-- ── Tab 2: Response Time Distribution ──────────────────────────────── -->
  <div id="tab-distribution" class="tab-pane">
    <h2>Response Time Distribution — by Profile</h2>
    <p class="legend">Each box = one user account &nbsp;&middot;&nbsp; red dashed line = p95 target</p>
    <div class="chart">{box_html}</div>

    <h2>Response Time Distribution — by Scenario</h2>
    <p class="legend">Each box = one CSV script &nbsp;&middot;&nbsp; red dashed line = p95 target</p>
    <div class="chart">{scen_html}</div>

    {heatmap_html}
  </div>

  <!-- ── Tab 3: Utterance Analysis ──────────────────────────────────────── -->
  <div id="tab-utterances" class="tab-pane">
    <h2>Per-Utterance Detail</h2>
    <p class="legend">
      Grouped by utterance + scenario &nbsp;&middot;&nbsp; sorted by p95 descending &nbsp;&middot;&nbsp;
      <span class="red">&#9679;</span> = MAD anomaly (response &gt; median&nbsp;+&nbsp;3&times;MAD) &nbsp;&middot;&nbsp;
      p99.9 proj = log-normal projection (requires &ge;10 samples)
    </p>
    <table>
      <thead><tr>
        <th>Utterance</th><th>Scenario</th><th>Profile(s)</th><th>Requests</th>
        <th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>Timeouts</th><th>p99.9 proj ms</th>
      </tr></thead>
      <tbody>{utt_rows_html}</tbody>
    </table>
  </div>

  <footer>
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
    &nbsp;&middot;&nbsp; GRUNTMASTER 6000
  </footer>
</div>
<script>{_JS}</script>
</body>
</html>"""

    out = csv_path.parent / (csv_path.stem.replace("detail_", "report_") + ".html")
    out.write_text(html, encoding="utf-8")
    return out


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate HTML report from a Copilot load test detail CSV"
    )
    parser.add_argument(
        "csv", nargs="?",
        help="Path to detail CSV (default: latest in report/)",
    )
    parser.add_argument(
        "--browse", action="store_true",
        help="Open gum file browser to pick a CSV",
    )
    args = parser.parse_args()

    if args.browse:
        picked = _gfile_pick(str(REPORT_DIR))
        if not picked:
            print("No file selected.")
            sys.exit(1)
        _csv = Path(picked)
    elif args.csv:
        _csv = Path(args.csv)
    else:
        _csvs = sorted(REPORT_DIR.glob("detail_*.csv"), key=lambda p: p.stat().st_mtime)
        if not _csvs:
            print("No detail_*.csv found in report/  —  run a test first.")
            sys.exit(1)
        _csv = _csvs[-1]

    # Auto-derive events CSV from detail CSV name
    _events = _csv.parent / _csv.name.replace("detail_", "events_")
    if not _events.exists():
        _events = None

    print(f"\n  Generating report from: {_csv}")
    if _events:
        print(f"  Events log:             {_events}")
    try:
        _out = generate_report(_csv, events_csv=_events)
        print(f"  Report saved to:        {_out}\n")
    except ImportError as _e:
        print(f"\n  {_e}\n")
        sys.exit(1)
