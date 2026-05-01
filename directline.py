"""
directline.py — DirectLine HTTP + WebSocket helpers.
All network calls to DirectLine live here, keeping locustfile.py clean.
"""

import os
import json
import time
import logging
from dataclasses import dataclass
from typing import Optional

import requests
import gevent.monkey

# Must patch before importing websocket to ensure gevent-compatible TLS sockets
gevent.monkey.patch_all()
import websocket

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DIRECTLINE_BASE   = "https://directline.botframework.com"
DL_SECRET         = os.getenv("CS_DIRECTLINE_SECRET", "")
TOKEN_ENDPOINT    = os.getenv("CS_TOKEN_ENDPOINT", "")
ENDPOINT_NEEDS_AUTH = os.getenv("CS_TOKEN_ENDPOINT_REQUIRES_AUTH", "false").lower() == "true"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Conversation:
    id: str
    token: str
    stream_url: str


@dataclass
class Response:
    activities: list[dict]
    latency_ms: float
    timed_out: bool


# ── DirectLine token ──────────────────────────────────────────────────────────

def fetch_directline_token(aad_token: Optional[str] = None) -> str:
    """
    Returns a DirectLine conversation token.
    Uses token endpoint if configured, otherwise DirectLine secret.
    aad_token: required only when CS_TOKEN_ENDPOINT_REQUIRES_AUTH=true
    """
    if TOKEN_ENDPOINT:
        headers = {}
        if ENDPOINT_NEEDS_AUTH and aad_token:
            headers["Authorization"] = f"Bearer {aad_token}"
        resp = requests.get(TOKEN_ENDPOINT, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()["token"]

    if DL_SECRET:
        headers = {"Authorization": f"Bearer {DL_SECRET}"}
        resp = requests.post(
            f"{DIRECTLINE_BASE}/v3/directline/tokens/generate",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    raise RuntimeError(
        "Neither CS_DIRECTLINE_SECRET nor CS_TOKEN_ENDPOINT is configured in .env"
    )


# ── Conversation ──────────────────────────────────────────────────────────────

def start_conversation(dl_token: str) -> Conversation:
    """Starts a DirectLine conversation. Returns Conversation dataclass."""
    headers = {
        "Authorization": f"Bearer {dl_token}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations",
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return Conversation(
        id=data["conversationId"],
        token=data["token"],
        stream_url=data["streamUrl"],
    )


def open_websocket(stream_url: str) -> websocket.WebSocket:
    """Opens a TLS WebSocket to the DirectLine stream URL."""
    ws = websocket.WebSocket(sslopt={"check_hostname": True})
    ws.connect(stream_url, timeout=20)
    return ws


# ── Utterance ─────────────────────────────────────────────────────────────────

def send_utterance(
    conversation: Conversation,
    utterance: str,
) -> tuple[str, float]:
    """
    POSTs a user message to the conversation.
    Returns (activity_id, send_time_epoch).
    activity_id is used to match bot reply frames via replyToId.
    """
    headers = {
        "Authorization": f"Bearer {conversation.token}",
        "Content-Type":  "application/json",
    }
    body = {
        "locale": "en-US",
        "type":   "message",
        "from":   {"id": "load-test-user"},
        "text":   utterance,
    }
    send_time = time.time()
    resp = requests.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
        headers=headers,
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    activity_id = resp.json()["id"]
    return activity_id, send_time


# ── Response reading ──────────────────────────────────────────────────────────

def read_response(
    ws: websocket.WebSocket,
    activity_id: str,
    frame_timeout: float = 10.0,
) -> Response:
    """
    Reads WebSocket frames until no new bot reply arrives within frame_timeout seconds.
    Only counts frames where replyToId == activity_id and role == "bot".

    Returns Response with matched activities, latency_ms from send time, and timed_out flag.
    Official CS guidance: timeout-based detection is the correct approach.
    """
    matched_activities = []
    last_match_time: Optional[float] = None
    start_time = time.time()

    ws.settimeout(frame_timeout)

    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            # No new frame within timeout — treat as end of response
            break
        except websocket.WebSocketConnectionClosedException:
            log.warning("WebSocket connection closed by DirectLine")
            break

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # DirectLine wraps activities in { "activities": [...] }
        activities = data.get("activities", [])
        for activity in activities:
            if _is_bot_reply(activity, activity_id):
                matched_activities.append(activity)
                last_match_time = time.time()
                log.debug("Bot reply received: %s", activity.get("text", "")[:80])

    end_time = last_match_time or time.time()
    latency_ms = (end_time - start_time) * 1000
    timed_out = len(matched_activities) == 0

    return Response(
        activities=matched_activities,
        latency_ms=latency_ms,
        timed_out=timed_out,
    )


def _is_bot_reply(activity: dict, sent_activity_id: str) -> bool:
    """
    True if the activity is a bot message in reply to our sent utterance.
    Filters out typing indicators, events, and unrelated messages.
    """
    if activity.get("type") != "message":
        return False
    if activity.get("from", {}).get("role") != "bot":
        return False
    if activity.get("replyToId") != sent_activity_id:
        return False
    return True


# ── WebSocket close ───────────────────────────────────────────────────────────

def close_websocket(ws: websocket.WebSocket):
    try:
        ws.close()
    except Exception:
        pass
