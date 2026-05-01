"""
test_connection.py — Standalone connection test.
Verifies DirectLine credentials, OAuthCard SSO exchange, and real bot response.

Usage:
    python test_connection.py
"""

import sys
sys.path.insert(0, __file__.rsplit("\\", 1)[0])

import gevent.monkey
gevent.monkey.patch_all()

from run import (
    fetch_directline_token,
    start_conversation,
    open_websocket,
    send_utterance,
    read_response,
    close_websocket,
    get_valid_token,
    load_profiles,
    _user_auth_required,
    DL_SECRET,
    TOKEN_ENDPOINT,
    ENDPOINT_NEEDS_AUTH,
    AGENT_APP_ID,
)

OK   = "\033[92mOK\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(label, fn):
    try:
        result = fn()
        print(f"  {OK}  {label}")
        return result
    except Exception as e:
        print(f"  {FAIL}  {label}")
        print(f"       {e}")
        sys.exit(1)


print()
print("=" * 60)
print("  COPILOT STUDIO — CONNECTION TEST")
print("=" * 60)
print()

print("  Config")
print(f"  DirectLine Secret : {'(set)' if DL_SECRET else '(not set)'}")
print(f"  Token Endpoint    : {TOKEN_ENDPOINT or '(not set)'}")
print(f"  Endpoint auth     : {ENDPOINT_NEEDS_AUTH}")
print(f"  Bot Client ID     : {AGENT_APP_ID or '(not set)'}")
print(f"  Auth required     : {_user_auth_required()}")
print()

# ── AAD token (needed for OAuthCard SSO exchange) ────────────────────────────
aad_token = None
if _user_auth_required():
    profiles = load_profiles()
    if not profiles:
        print(f"  {FAIL}  No profiles configured — run python run.py to set up profiles")
        sys.exit(1)
    username = profiles[0]["username"]
    print(f"  ...  Getting AAD token for {username}")
    aad_token = check("AAD token", lambda: get_valid_token(username))

# ── DirectLine ────────────────────────────────────────────────────────────────
dl_token     = check("Fetch DirectLine token", lambda: fetch_directline_token(aad_token))
conversation = check("Start conversation",      lambda: start_conversation(dl_token))
ws           = check("Open WebSocket",          lambda: open_websocket(conversation.stream_url))

activity_id, _ = check("Send 'hi'", lambda: send_utterance(conversation, "hi"))

print(f"  ...  Waiting for bot reply (15s timeout) + OAuthCard exchange if needed ...")
response = read_response(
    ws, activity_id,
    frame_timeout=15.0,
    conversation=conversation,
    aad_token=aad_token,
)
close_websocket(ws)

if response.timed_out:
    print(f"  {FAIL}  Bot did not reply within 15s")
    sys.exit(1)

first_text = next((a.get("text", "").strip() for a in response.activities if a.get("text")), "")

# Check if bot is still asking for sign-in
if "sign in" in first_text.lower() or "i'll need you to sign in" in first_text.lower():
    print(f"  {FAIL}  Bot is still requesting sign-in — OAuthCard exchange did not complete")
    print(f"       Bot said: {first_text[:200]}")
    print()
    print("  Possible causes:")
    print("  - AGENT_APP_ID is wrong or not set")
    print("  - The saved token scope does not match the bot's OAuth connection")
    print("  - Bot's Token Exchange URL is not configured")
    sys.exit(1)

print(f"  {OK}  Bot replied  ({response.latency_ms:.0f}ms)")
print()
print("  Bot said:")
for a in response.activities:
    text = a.get("text", "").strip()
    if text:
        print(f"    {text[:200]}")
print()

WARN = "\033[93mWARN\033[0m"
if "usage limit" in first_text.lower() or "currently unavailable" in first_text.lower():
    print(f"  {WARN}  Auth OK — bot replied but reports a capacity/usage limit.")
    print()
    print("  This is a Copilot Studio quota issue, not an auth problem.")
    print("  Fix: Power Platform Admin Center > Capacity > add message capacity to the environment.")
    print()
else:
    print("  All checks passed.")
print()
