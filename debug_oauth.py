"""
debug_oauth.py — Captures the raw OAuthCard and invoke activities from the bot.
Run this to see exactly what connectionName and tokenExchangeResource the bot sends.

Usage:
    python debug_oauth.py
"""

import sys, json, time
sys.path.insert(0, __file__.rsplit("\\", 1)[0])

import gevent.monkey
gevent.monkey.patch_all()

import websocket
from run import (
    fetch_directline_token, start_conversation, open_websocket,
    send_utterance, get_valid_token, load_profiles,
)

profiles = load_profiles()
aad_token = get_valid_token(profiles[0]["username"])
dl_token  = fetch_directline_token(aad_token)
conv      = start_conversation(dl_token)
ws        = open_websocket(conv.stream_url)

send_utterance(conv, "hi")

print()
print("Raw activities from bot (12s window):")
print("=" * 60)

ws.settimeout(12.0)
deadline = time.time() + 12
while time.time() < deadline:
    try:
        raw = ws.recv()
        if not raw:
            continue
        data = json.loads(raw)
        for act in data.get("activities", []):
            t    = act.get("type", "")
            name = act.get("name", "")
            if t in ("message", "invoke"):
                print(f"\ntype={t}  name={name}")
                # OAuthCard
                for att in act.get("attachments", []):
                    ct = att.get("contentType", "")
                    print(f"  attachment contentType={ct}")
                    if ct == "application/vnd.microsoft.card.oauth":
                        content = att.get("content", {})
                        print(f"  connectionName       = {content.get('connectionName')}")
                        ter = content.get("tokenExchangeResource", {})
                        print(f"  tokenExchangeResource.id  = {ter.get('id')}")
                        print(f"  tokenExchangeResource.uri = {ter.get('uri')}")
                # Invoke value
                if t == "invoke":
                    val = act.get("value", {})
                    print(f"  value.id             = {val.get('id')}")
                    print(f"  value.connectionName = {val.get('connectionName')}")
                    print(f"  value.uri            = {val.get('uri')}")
                # Message text
                if t == "message":
                    text = act.get("text", "").strip()
                    if text:
                        print(f"  text: {text[:200]}")
    except websocket.WebSocketTimeoutException:
        break

ws.close()
print()
print("=" * 60)
