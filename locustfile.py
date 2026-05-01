"""
locustfile.py — Copilot Studio load test scenarios.

Two scenarios aligned to official CS guidance:
  - BalanceCheckUser   → utterances/check_account_balance.csv
  - MakePaymentUser    → utterances/make_payment.csv

Run:
    locust -f locustfile.py,ui.py
    Open http://localhost:8089
    Fill in parameters and click Start.

    When Start is clicked, auth runs automatically for any profile
    without a valid token. Watch the terminal for the sign-in prompt —
    open the URL shown, enter the code, and sign in. The test starts
    automatically once all profiles are authenticated.
"""

import csv
import itertools
import random
import sys
import threading
import time
import logging
from pathlib import Path

from locust import User, events, task
from locust.exception import StopUser

import gevent.monkey
gevent.monkey.patch_all()

from auth import get_valid_token, load_profiles, authenticate_profile, load_token, is_token_valid
from directline import (
    fetch_directline_token,
    start_conversation,
    open_websocket,
    send_utterance,
    read_response,
    close_websocket,
)
from config import test_config
from startup import run_startup_sequence

log = logging.getLogger(__name__)

UTTERANCES_DIR = Path(__file__).parent / "utterances"


# ── Startup sequence — runs when Locust initialises ───────────────────────────

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """
    Runs once when Locust starts (before the web UI opens).
    Fires the jazzy terminal sequence: title crawl → credential scan →
    profile status → rocket auth for any profiles that need sign-in →
    bomb countdown → ready message.
    Skipped automatically in --headless mode.
    """
    profiles = load_profiles()
    run_startup_sequence(environment, profiles)


# ── Headless auth guard — runs when test starts in --headless mode ────────────

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """
    Headless-only: verifies all profiles have valid tokens before spawning users.
    In interactive mode the startup sequence already handled auth — this is a no-op.
    """
    headless = "--headless" in sys.argv or "-headless" in sys.argv
    if not headless:
        return

    profiles = load_profiles()
    for profile in profiles:
        token_data = load_token(profile["username"])
        if not token_data or not is_token_valid(token_data):
            print(
                f"\n[Auth] No valid token for {profile['username']}."
                " Pre-authenticate all profiles before running headless.\n"
            )
            environment.runner.quit()
            return

    print("\n[Auth] All profiles have valid tokens — proceeding.\n")


# ── CSV loader — thread-safe queue per scenario ───────────────────────────────

def _load_csv_cycle(filename: str) -> itertools.cycle:
    path = UTTERANCES_DIR / filename
    with open(path, newline="") as f:
        rows = [r["utterance"] for r in csv.DictReader(f)]
    if not rows:
        raise RuntimeError(f"Utterances file is empty: {path}")
    return itertools.cycle(rows)


_balance_utterances  = _load_csv_cycle("check_account_balance.csv")
_payment_utterances  = _load_csv_cycle("make_payment.csv")
_utterance_lock      = threading.Lock()


def _next_utterance(cycle: itertools.cycle) -> str:
    with _utterance_lock:
        return next(cycle)


# ── Profile round-robin ───────────────────────────────────────────────────────

_profiles      = load_profiles()
_profile_cycle = itertools.cycle(_profiles)
_profile_lock  = threading.Lock()


def _next_profile() -> dict:
    with _profile_lock:
        return next(_profile_cycle)


# ── Metric helper ─────────────────────────────────────────────────────────────

def _fire_metric(environment, name: str, latency_ms: float, error: Exception = None):
    environment.events.request.fire(
        request_type="CopilotStudio",
        name=name,
        response_time=latency_ms,
        response_length=0,
        exception=error,
    )


# ── Base user class ───────────────────────────────────────────────────────────

class CopilotBaseUser(User):
    """
    Base class for all Copilot Studio load test users.
    Handles auth, DirectLine setup, WebSocket lifecycle, and error handling.
    Subclasses define the utterance cycle and scenario name.
    """
    abstract = True

    utterance_cycle = None   # set in subclass
    scenario_name   = ""     # set in subclass

    def on_start(self):
        self.profile     = _next_profile()
        self.conversation = None
        self.ws           = None

        # Step 1: get cached AAD token — raises RuntimeError if not authenticated
        try:
            self.aad_token = get_valid_token(self.profile["username"])
        except RuntimeError as e:
            log.error("Auth failed for %s: %s", self.profile["username"], e)
            raise StopUser()

        # Step 2: fetch DirectLine token
        try:
            self.dl_token = fetch_directline_token(self.aad_token)
        except Exception as e:
            log.error("DirectLine token fetch failed: %s", e)
            _fire_metric(self.environment, "Fetch Token", 0, error=e)
            raise StopUser()

        # Step 3: start conversation
        try:
            self.conversation = start_conversation(self.dl_token)
        except Exception as e:
            log.error("Start conversation failed: %s", e)
            _fire_metric(self.environment, "Start Conversation", 0, error=e)
            raise StopUser()

        # Step 4: open WebSocket
        try:
            self.ws = open_websocket(self.conversation.stream_url)
        except Exception as e:
            log.error("WebSocket open failed: %s", e)
            _fire_metric(self.environment, "Open WebSocket", 0, error=e)
            raise StopUser()

        log.info(
            "User started | profile=%s | conversation=%s",
            self.profile["username"],
            self.conversation.id,
        )

    def on_stop(self):
        if self.ws:
            close_websocket(self.ws)

    def _send_and_measure(self):
        """
        Sends one utterance and measures end-to-end Copilot response latency.
        On any error: fires error metric and raises StopUser (official CS guidance).
        """
        utterance = _next_utterance(self.utterance_cycle)

        # Send utterance
        try:
            activity_id, _ = send_utterance(self.conversation, utterance)
        except Exception as e:
            log.error("Send utterance failed: %s", e)
            _fire_metric(self.environment, "Send Utterance", 0, error=e)
            raise StopUser()

        # Read response frames
        frame_timeout = test_config.get("frame_timeout", 10.0)
        try:
            response = read_response(self.ws, activity_id, frame_timeout=frame_timeout)
        except Exception as e:
            log.error("Read response failed: %s", e)
            _fire_metric(self.environment, "Copilot Response", 0, error=e)
            raise StopUser()

        # Zero reply frames = bot did not respond = treat as error
        if response.timed_out:
            log.warning("No bot reply received for activity %s", activity_id)
            _fire_metric(
                self.environment,
                "Copilot Response",
                response.latency_ms,
                error=Exception("No bot reply received"),
            )
            raise StopUser()

        # Record successful latency
        _fire_metric(self.environment, f"Copilot Response — {self.scenario_name}", response.latency_ms)
        log.debug(
            "Response | scenario=%s | latency=%.0fms | replies=%d",
            self.scenario_name,
            response.latency_ms,
            len(response.activities),
        )

        # Realistic think time — official CS guidance: 30–60 seconds between turns
        think_min = test_config.get("think_min", 30)
        think_max = test_config.get("think_max", 60)
        time.sleep(random.randint(think_min, think_max))


# ── Scenario: Balance Check ───────────────────────────────────────────────────

class BalanceCheckUser(CopilotBaseUser):
    utterance_cycle = _balance_utterances
    scenario_name   = "Balance Check"
    weight          = 1

    @task
    def check_balance(self):
        self._send_and_measure()


# ── Scenario: Make Payment ────────────────────────────────────────────────────

class MakePaymentUser(CopilotBaseUser):
    utterance_cycle = _payment_utterances
    scenario_name   = "Make Payment"
    weight          = 1

    @task
    def make_payment(self):
        self._send_and_measure()
