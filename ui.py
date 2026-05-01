"""
ui.py — Extends Locust's web UI with Copilot Studio test parameters.

Injects a custom parameter panel into the Locust start form.
Parameters are POSTed to /cs-config before the test starts,
then stored in config.test_config for User classes to read.

Requires Locust >= 2.10.0.
Import this file from locustfile.py is NOT needed — Locust loads it
automatically when passed via -f flag alongside locustfile.py:
    locust -f locustfile.py,ui.py
"""

import csv
import os
from pathlib import Path

from flask import request, jsonify
from locust import events

from config import test_config

PROFILES_CSV = Path(__file__).parent / "profiles" / "profiles.csv"


def _load_profile_names() -> list[str]:
    if not PROFILES_CSV.exists():
        return []
    with open(PROFILES_CSV, newline="") as f:
        return [r["username"] for r in csv.DictReader(f)]


def _profile_options_html(profiles: list[str]) -> str:
    return "".join(f'<option value="{p}">{p}</option>' for p in profiles)


# ── Register UI extension ─────────────────────────────────────────────────────

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Called once when Locust starts. Adds /cs-config endpoint and injects HTML."""

    if not hasattr(environment, "web_ui") or environment.web_ui is None:
        return  # headless / CLI mode — skip UI

    app = environment.web_ui.app

    # ── /cs-config endpoint — receives custom params from the form ────────────
    @app.route("/cs-config", methods=["POST"])
    def set_cs_config():
        data = request.get_json(force=True, silent=True) or {}

        test_config["frame_timeout"]  = float(data.get("frame_timeout", 10))
        test_config["think_min"]      = int(data.get("think_min", 30))
        test_config["think_max"]      = int(data.get("think_max", 60))
        test_config["p95_target_ms"]  = int(data.get("p95_target_ms", 2000))
        test_config["max_error_rate"] = float(data.get("max_error_rate", 0.5))

        # Override env vars for DirectLine connection if provided
        if data.get("dl_secret"):
            os.environ["CS_DIRECTLINE_SECRET"] = data["dl_secret"]
        if data.get("token_endpoint"):
            os.environ["CS_TOKEN_ENDPOINT"] = data["token_endpoint"]

        return jsonify({"status": "ok", "config": test_config})

    # ── /cs-profiles endpoint — returns current profile list ─────────────────
    @app.route("/cs-profiles", methods=["GET"])
    def get_profiles():
        return jsonify({"profiles": _load_profile_names()})

    # ── Inject custom HTML into Locust start form ─────────────────────────────
    profiles       = _load_profile_names()
    profile_opts   = _profile_options_html(profiles)
    dl_secret      = os.getenv("CS_DIRECTLINE_SECRET", "")
    token_endpoint = os.getenv("CS_TOKEN_ENDPOINT", "")

    custom_html = f"""
<style>
  #cs-config-panel {{
    background: #f8f9fa;
    border: 1px solid #dee2e6;
    border-radius: 6px;
    padding: 20px 24px;
    margin-bottom: 20px;
    font-family: inherit;
  }}
  #cs-config-panel h3 {{
    margin-top: 0;
    margin-bottom: 16px;
    font-size: 15px;
    color: #333;
    border-bottom: 1px solid #dee2e6;
    padding-bottom: 8px;
  }}
  .cs-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 24px;
  }}
  .cs-field label {{
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: #555;
    margin-bottom: 4px;
  }}
  .cs-field input, .cs-field select {{
    width: 100%;
    padding: 6px 8px;
    border: 1px solid #ced4da;
    border-radius: 4px;
    font-size: 13px;
    box-sizing: border-box;
  }}
  .cs-hint {{
    font-size: 11px;
    color: #888;
    margin-top: 2px;
  }}
  .cs-section-title {{
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    color: #888;
    margin: 14px 0 8px;
    letter-spacing: 0.5px;
  }}
</style>

<div id="cs-config-panel">
  <h3>Copilot Studio Test Configuration</h3>

  <div class="cs-section-title">DirectLine Connection</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>DirectLine Secret</label>
      <input type="password" id="cs-dl-secret" value="{dl_secret}" placeholder="From .env if blank">
      <div class="cs-hint">Leave blank to use .env value</div>
    </div>
    <div class="cs-field">
      <label>Token Endpoint URL</label>
      <input type="text" id="cs-token-endpoint" value="{token_endpoint}" placeholder="Or use Token Endpoint">
      <div class="cs-hint">Alternative to DirectLine Secret</div>
    </div>
  </div>

  <div class="cs-section-title">Timing</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Response Timeout (seconds)</label>
      <input type="number" id="cs-frame-timeout" value="10" min="5" max="60">
      <div class="cs-hint">Max wait for each bot frame</div>
    </div>
    <div class="cs-field">
      <label>Think Time Min (seconds)</label>
      <input type="number" id="cs-think-min" value="30" min="5" max="300">
      <div class="cs-hint">Official guidance: 30–60s between turns</div>
    </div>
    <div class="cs-field">
      <label>Think Time Max (seconds)</label>
      <input type="number" id="cs-think-max" value="60" min="5" max="300">
    </div>
  </div>

  <div class="cs-section-title">Success Criteria</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>95th Percentile Target (ms)</label>
      <input type="number" id="cs-p95" value="2000" min="100">
      <div class="cs-hint">Baseline: 2000ms per CS guidance</div>
    </div>
    <div class="cs-field">
      <label>Max Error Rate (%)</label>
      <input type="number" id="cs-error-rate" value="0.5" min="0" max="100" step="0.1">
      <div class="cs-hint">Baseline: 0.5% per CS guidance</div>
    </div>
  </div>

  <div class="cs-section-title">Profiles</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Active Profile Set</label>
      <select id="cs-profiles">
        <option value="all">All profiles ({len(profiles)} loaded)</option>
        {profile_opts}
      </select>
      <div class="cs-hint">{len(profiles)} profile(s) found in profiles/profiles.csv</div>
    </div>
  </div>

  <div style="margin-top:14px; padding:10px 12px; background:#fff3cd; border:1px solid #ffc107; border-radius:4px; font-size:12px; color:#856404;">
    <strong>Sign-in:</strong> If any profile needs authentication, a sign-in prompt will appear
    in the terminal where you ran <code>locust</code>. Open the URL shown there and enter the code.
    The test will start automatically once sign-in is complete.
  </div>
</div>

<script>
(function() {{
  // Intercept the Locust start form submission
  // Post our config first, then allow the normal swarm start
  var originalFetch = window.fetch;

  document.addEventListener('DOMContentLoaded', function() {{
    var startForm = document.querySelector('form[action*="swarm"]') ||
                    document.getElementById('start-form') ||
                    document.querySelector('form');

    if (!startForm) return;

    startForm.addEventListener('submit', function(e) {{
      e.preventDefault();
      e.stopImmediatePropagation();

      var config = {{
        dl_secret:      document.getElementById('cs-dl-secret').value,
        token_endpoint: document.getElementById('cs-token-endpoint').value,
        frame_timeout:  parseFloat(document.getElementById('cs-frame-timeout').value),
        think_min:      parseInt(document.getElementById('cs-think-min').value),
        think_max:      parseInt(document.getElementById('cs-think-max').value),
        p95_target_ms:  parseInt(document.getElementById('cs-p95').value),
        max_error_rate: parseFloat(document.getElementById('cs-error-rate').value),
      }};

      fetch('/cs-config', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(config)
      }}).then(function() {{
        startForm.submit();
      }}).catch(function(err) {{
        console.error('Failed to save CS config:', err);
        startForm.submit(); // submit anyway with defaults
      }});
    }}, true);
  }});
}})();
</script>
"""

    @environment.web_ui.app.after_request
    def inject_cs_panel(response):
        if (
            response.content_type.startswith("text/html")
            and b"id=\"start-form\"" in response.data
            or b"new-test" in response.data
        ):
            html = response.get_data(as_text=True)
            # Inject panel just before the first <form> tag on the start page
            html = html.replace(
                '<div class="container">',
                '<div class="container">' + custom_html,
                1,
            )
            response.set_data(html)
        return response
