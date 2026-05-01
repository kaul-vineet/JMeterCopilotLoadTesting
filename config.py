"""
config.py — Shared test configuration dict.
Populated by ui.py when the user fills in the Locust start form.
Read by locustfile.py User classes via test_config.get(key, default).
"""

test_config: dict = {
    "frame_timeout": 10.0,
    "think_min":     30,
    "think_max":     60,
    "p95_target_ms": 2000,
    "max_error_rate": 0.5,
}
