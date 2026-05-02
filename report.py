"""
report.py — Generates a self-contained HTML performance report from a detail CSV.

Usage:
    python report.py                          # latest detail_*.csv in report/
    python report.py report/detail_xyz.csv   # specific file
"""

import sys
import math
import base64
import argparse
from pathlib import Path
from datetime import datetime
from itertools import combinations

REPORT_DIR     = Path(__file__).parent / "report"
P95_TARGET_MS  = 2000

# ── CSS and JS as plain strings (no f-string escaping needed) ─────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#1e293b;background:#f8fafc}
.wrap{max-width:1200px;margin:0 auto;padding:24px}
.hdr{background:#0f172a;color:#fff;padding:20px 28px;border-radius:10px;margin-bottom:24px;display:flex;align-items:center;gap:28px;flex-wrap:wrap}
.hdr h1{font-size:16px;font-weight:700;flex:1;letter-spacing:.3px}
.stat{text-align:center;min-width:80px}
.stat .v{font-size:20px;font-weight:700}
.stat .l{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;margin-top:2px}
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
.dl{text-align:center;margin:28px 0 12px}
.dl a{display:inline-block;padding:9px 22px;background:#0f172a;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px}
.dl a:hover{background:#1e293b}
footer{text-align:center;color:#cbd5e1;font-size:11px;padding:20px 0}
"""

_JS = """
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


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_report(csv_path: Path, p95_target: int = P95_TARGET_MS) -> Path:
    _require_deps()
    import pandas as pd
    import plotly.graph_objects as go

    # ── Load ──────────────────────────────────────────────────────────────────
    df = pd.read_csv(csv_path)
    df["response_ms"] = pd.to_numeric(df["response_ms"], errors="coerce").fillna(0)
    df["timed_out"]   = pd.to_numeric(df["timed_out"], errors="coerce").fillna(0).astype(int)
    df_ok = df[df["timed_out"] == 0].copy()

    has_ts = "utterance_sent_at" in df.columns
    if has_ts:
        df["sent_dt"] = pd.to_datetime(df["utterance_sent_at"], utc=True, errors="coerce")
        df_ok = df_ok.copy()
        df_ok["sent_dt"] = df.loc[df_ok.index, "sent_dt"]

    # ── Top-level aggregates ──────────────────────────────────────────────────
    total_reqs  = len(df)
    total_tout  = int(df["timed_out"].sum())
    error_rate  = total_tout / max(1, total_reqs) * 100
    overall_p95 = _pct(df_ok["response_ms"], 0.95)
    passed      = overall_p95 <= p95_target

    if has_ts and not df["sent_dt"].isna().all():
        t_start      = df["sent_dt"].min()
        t_end        = df["sent_dt"].max()
        duration_s   = int((t_end - t_start).total_seconds())
        test_date_str = t_start.strftime("%Y-%m-%d %H:%M UTC")
        duration_str  = f"{duration_s // 60}m {duration_s % 60}s"
    else:
        test_date_str = csv_path.stem.replace("detail_", "")
        duration_str  = "—"

    # ── Per-profile stats ─────────────────────────────────────────────────────
    profiles = []
    for scenario, grp in df.groupby("scenario"):
        ms = grp[grp["timed_out"] == 0]["response_ms"]
        tout = int(grp["timed_out"].sum())
        reqs = len(grp)
        p95v = _pct(ms, 0.95)
        profiles.append(dict(
            scenario=scenario, requests=reqs,
            p50=_pct(ms, 0.50), p95=p95v, p99=_pct(ms, 0.99),
            timeouts=tout, error_pct=f"{tout/max(1,reqs)*100:.1f}%",
            ok=p95v <= p95_target,
        ))

    # ── Per-utterance stats (Options B + C) ───────────────────────────────────
    utterances = []
    for utt, grp in df.groupby("utterance"):
        ms   = grp[grp["timed_out"] == 0]["response_ms"]
        tout = int(grp["timed_out"].sum())
        reqs = len(grp)

        # Option C — MAD anomaly count
        anomalies = 0
        if len(ms) >= 4:
            med = ms.median()
            mad = (ms - med).abs().median()
            if mad > 0:
                anomalies = int((ms > med + 3 * mad).sum())

        # Option C — log-normal p99.9 projection (needs ≥10 observations)
        p999 = None
        log_v = ms[ms > 0].apply(math.log)
        if len(log_v) >= 10:
            mu, sigma = log_v.mean(), log_v.std()
            if sigma > 0:
                p999 = int(math.exp(mu + 3.09 * sigma))

        utterances.append(dict(
            utterance=str(utt),
            scenarios=", ".join(sorted(grp["scenario"].unique())),
            requests=reqs,
            p50=_pct(ms, 0.50), p95=_pct(ms, 0.95), p99=_pct(ms, 0.99),
            timeouts=tout, timeout_pct=f"{tout/max(1,reqs)*100:.1f}%",
            anomalies=anomalies, p999=p999,
        ))
    utterances.sort(key=lambda x: x["p95"], reverse=True)

    # ── Chart: box/whisker per profile ────────────────────────────────────────
    box_fig = go.Figure()
    for scenario, grp in df_ok.groupby("scenario"):
        box_fig.add_trace(go.Box(
            y=grp["response_ms"].tolist(), name=scenario,
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

    # ── Chart: latency heatmap ────────────────────────────────────────────────
    heatmap_section = ""
    if has_ts and "sent_dt" in df_ok.columns and not df_ok["sent_dt"].isna().all():
        t0   = df_ok["sent_dt"].min()
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
            margin=dict(t=20, b=40, l=260),
        )
        hm_html = hm_fig.to_html(include_plotlyjs=False, full_html=False)
        heatmap_section = (
            '<h2>Latency Heatmap</h2>'
            '<p class="legend">Median response time per 30-second window — '
            'rising colour signals degradation over time</p>'
            f'<div class="chart">{hm_html}</div>'
        )

    # ── Option C — profile comparison table ───────────────────────────────────
    comparison_section = ""
    if len(profiles) >= 2:
        medians = {
            p["scenario"]: _pct(df_ok[df_ok["scenario"] == p["scenario"]]["response_ms"], 0.50)
            for p in profiles
        }
        rows = []
        for a, b in combinations(medians.keys(), 2):
            ma, mb = medians[a], medians[b]
            if ma > 0:
                diff = (mb - ma) / ma * 100
                badge = (
                    f'<span style="color:#ef4444">{abs(diff):.1f}% slower</span>'
                    if diff > 0
                    else f'<span style="color:#22c55e">{abs(diff):.1f}% faster</span>'
                )
                rows.append(
                    f"<tr><td>{a}</td><td>{b}</td><td>{badge}</td>"
                    f"<td>{ma:,}ms</td><td>{mb:,}ms</td></tr>"
                )
        if rows:
            comparison_section = (
                '<h2>Profile Comparison</h2>'
                '<p class="legend">Median response time difference between profiles</p>'
                '<table><thead><tr>'
                '<th>Profile A</th><th>Profile B</th><th>Difference</th>'
                '<th>A median</th><th>B median</th>'
                '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
            )

    # ── Assemble HTML rows ────────────────────────────────────────────────────
    def _profile_row(p):
        rs  = ' style="background:#fef2f2"' if not p["ok"] else ""
        p95s = ' style="color:#ef4444;font-weight:700"' if not p["ok"] else ""
        return (
            f'<tr{rs}><td>{p["scenario"]}</td><td>{p["requests"]:,}</td>'
            f'<td>{p["p50"]:,}</td><td{p95s}>{p["p95"]:,}</td>'
            f'<td>{p["p99"]:,}</td><td>{p["timeouts"]}</td><td>{p["error_pct"]}</td></tr>'
        )

    def _utt_row(u):
        dot  = ' <span title="MAD anomaly" style="color:#ef4444;font-size:10px">●</span>' if u["anomalies"] else ""
        p999 = f'{u["p999"]:,}' if u["p999"] else "—"
        p95s = ' style="color:#ef4444;font-weight:700"' if u["p95"] > p95_target else ""
        ts   = ' style="color:#ef4444"' if u["timeouts"] > 0 else ""
        lbl  = u["utterance"][:80] + ("…" if len(u["utterance"]) > 80 else "")
        return (
            f'<tr><td>{lbl}{dot}</td><td>{u["scenarios"]}</td>'
            f'<td>{u["requests"]:,}</td><td>{u["p50"]:,}</td>'
            f'<td{p95s}>{u["p95"]:,}</td><td>{u["p99"]:,}</td>'
            f'<td{ts}>{u["timeouts"]} ({u["timeout_pct"]})</td>'
            f'<td style="color:#64748b">{p999}</td></tr>'
        )

    profile_rows = "".join(_profile_row(p) for p in profiles)
    utt_rows     = "".join(_utt_row(u) for u in utterances)

    pass_badge = (
        '<span style="background:#22c55e;color:#fff;padding:4px 14px;border-radius:4px;font-weight:700">PASS</span>'
        if passed else
        '<span style="background:#ef4444;color:#fff;padding:4px 14px;border-radius:4px;font-weight:700">FAIL</span>'
    )
    csv_b64 = base64.b64encode(csv_path.read_bytes()).decode()

    # ── Final HTML ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Copilot Load Test — {test_date_str}</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="wrap">

  <div class="hdr">
    <h1>COPILOT STUDIO &nbsp;&middot;&nbsp; LOAD TEST REPORT</h1>
    <div class="stat"><div class="v">{test_date_str}</div><div class="l">Test date</div></div>
    <div class="stat"><div class="v">{total_reqs:,}</div><div class="l">Requests</div></div>
    <div class="stat"><div class="v">{duration_str}</div><div class="l">Duration</div></div>
    <div class="stat"><div class="v">{error_rate:.1f}%</div><div class="l">Error rate</div></div>
    <div class="stat"><div class="v">{overall_p95:,}ms</div><div class="l">p95</div></div>
    <div class="stat">{pass_badge}<div class="l" style="margin-top:4px">vs {p95_target:,}ms</div></div>
  </div>

  <h2>Profile Summary</h2>
  <table>
    <thead><tr>
      <th>Profile</th><th>Requests</th><th>p50 ms</th><th>p95 ms</th>
      <th>p99 ms</th><th>Timeouts</th><th>Error %</th>
    </tr></thead>
    <tbody>{profile_rows}</tbody>
  </table>

  <h2>Response Time Distribution</h2>
  <div class="chart">{box_html}</div>

  {heatmap_section}

  <h2>Per-Utterance Detail</h2>
  <p class="legend">
    Sorted by p95 &nbsp;&middot;&nbsp;
    <span class="red">&#9679;</span> = MAD anomaly (response &gt; median&nbsp;+&nbsp;3&times;MAD) &nbsp;&middot;&nbsp;
    p99.9 proj = log-normal projection (requires &ge;10 samples)
  </p>
  <table>
    <thead><tr>
      <th>Utterance</th><th>Profile</th><th>Requests</th>
      <th>p50 ms</th><th>p95 ms</th><th>p99 ms</th>
      <th>Timeouts</th><th>p99.9 proj ms</th>
    </tr></thead>
    <tbody>{utt_rows}</tbody>
  </table>

  {comparison_section}

  <div class="dl">
    <a href="data:text/csv;base64,{csv_b64}" download="{csv_path.name}">
      &darr;&nbsp; Download raw CSV
    </a>
  </div>

  <footer>
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
    &nbsp;&middot;&nbsp; Copilot Studio Load Test
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
    args = parser.parse_args()

    if args.csv:
        _csv = Path(args.csv)
    else:
        _csvs = sorted(REPORT_DIR.glob("detail_*.csv"), key=lambda p: p.stat().st_mtime)
        if not _csvs:
            print("No detail_*.csv found in report/  —  run a test first.")
            sys.exit(1)
        _csv = _csvs[-1]

    print(f"\n  Generating report from: {_csv}")
    try:
        _out = generate_report(_csv)
        print(f"  Report saved to:        {_out}\n")
    except ImportError as _e:
        print(f"\n  {_e}\n")
        sys.exit(1)
