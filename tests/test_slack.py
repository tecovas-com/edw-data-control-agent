"""Tests for the Slack integration.

Most tests are pure (signature HMAC, block composition, delete logic against a
fake WebClient). The `test_live_*` tests below ACTUALLY post to Slack — they
require SLACK_BOT_TOKEN and SLACK_CHANNEL and ERROR (fail loud) if those are
unset. They clean up after themselves. Point SLACK_CHANNEL at a test channel.

    pytest tests/test_slack.py -k "not live"   # pure tests only
    SLACK_BOT_TOKEN=xoxb-... SLACK_CHANNEL=C... pytest tests/test_slack.py
"""
import hashlib
import hmac
import os

import pytest

from src.slack import SlackClient, build_alert_blocks, verify_slack_signature
from stubs import FakeWebClient

SECRET = "unit-signing-secret-for-hmac-test"
NOW = 1_700_000_000.0  # fixed unix time; injected, never read from the clock


def _sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


# --- verify_slack_signature --------------------------------------------------

def test_signature_accepts_valid():
    body = b'{"hello":"world"}'
    ts = str(int(NOW))
    assert verify_slack_signature(body, ts, _sign(body, ts), SECRET, now=NOW) is True


def test_signature_rejects_wrong_secret():
    body = b'{"hello":"world"}'
    ts = str(int(NOW))
    sig = _sign(body, ts, secret="wrong")
    assert verify_slack_signature(body, ts, sig, SECRET, now=NOW) is False


def test_signature_rejects_replay_older_than_five_minutes():
    body = b'{"hello":"world"}'
    ts = str(int(NOW) - 60 * 6)
    assert verify_slack_signature(body, ts, _sign(body, ts), SECRET, now=NOW) is False


def test_signature_rejects_tampered_body():
    body = b'{"hello":"world"}'
    ts = str(int(NOW))
    sig = _sign(body, ts)
    assert verify_slack_signature(body + b" ", ts, sig, SECRET, now=NOW) is False


def test_signature_rejects_missing_fields():
    body = b'{}'
    ts = str(int(NOW))
    assert verify_slack_signature(body, ts, _sign(body, ts), "", now=NOW) is False
    assert verify_slack_signature(body, "", "sig", SECRET, now=NOW) is False


# --- build_alert_blocks ------------------------------------------------------

def test_alert_blocks_carry_unique_id_on_buttons():
    blocks = build_alert_blocks("model.tecovas.fct_sales", "stale upstream")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    values = {e["action_id"]: e["value"] for e in actions["elements"]}
    assert values == {
        "retrigger_loader": "model.tecovas.fct_sales",
        "acknowledge": "model.tecovas.fct_sales",
    }


def test_alert_blocks_render_sources_and_actions():
    blocks = build_alert_blocks(
        "model.x",
        "summary",
        failing_sources=["src.a", "src.b"],
        actions_taken=["re-ran fivetran:c1"],
    )
    text = "".join(
        f.get("text", "")
        for b in blocks
        for f in ([b.get("text", {})] + b.get("fields", []))
        if isinstance(f, dict)
    )
    assert "src.a" in text and "src.b" in text
    assert "re-ran fivetran:c1" in text


# --- delete logic (fake WebClient, no network) -------------------------------

def test_delete_bot_messages_targets_only_this_bot_and_dedups_threads():
    history = [
        {"ts": "1", "user": "U_BOT"},               # ours (by user_id)
        {"ts": "2", "user": "U_HUMAN"},             # not ours
        {"ts": "3", "bot_id": "B_BOT", "reply_count": 2},  # ours, has a thread
    ]
    replies = {
        "3": [
            {"ts": "3", "bot_id": "B_BOT"},          # parent again -> must dedup
            {"ts": "4", "user": "U_BOT", "thread_ts": "3"},   # ours
            {"ts": "5", "user": "U_HUMAN", "thread_ts": "3"},  # not ours
        ]
    }
    fake = FakeWebClient(history=history, replies=replies)
    result = SlackClient(fake, "C1").delete_bot_messages()

    assert result["ok"] is True
    assert set(result["deleted"]) == {"1", "3", "4"}
    assert set(fake.deleted) == {"1", "3", "4"}  # ts "3" deleted exactly once


def test_delete_message_passes_ts_through():
    fake = FakeWebClient()
    assert SlackClient(fake, "C1").delete_message("123.456")["ok"] is True
    assert fake.deleted == ["123.456"]


# --- send_dm (resolves the DM channel, not a raw user id) --------------------

def test_send_dm_opens_im_channel_then_posts_there():
    fake = FakeWebClient()
    result = SlackClient(fake, "C1").send_dm("U123", "hello")

    assert result == {
        "ok": True,
        "ts": "999.000",
        "channel": "D_U123",
        "user_id": "U123",
    }
    # opened the IM, then posted to the resolved D-channel (NOT the raw user id).
    assert fake.opened == ["U123"]
    assert fake.posted[0]["channel"] == "D_U123"


def test_send_dm_resolves_email_to_user_id_first():
    fake = FakeWebClient(users_by_email={"a@b.com": "U999"})
    result = SlackClient(fake, "C1").send_dm("a@b.com", "hi")

    assert result["user_id"] == "U999"
    assert fake.opened == ["U999"]
    assert fake.posted[0]["channel"] == "D_U999"


# --- find_users_by_name (scans users.list, paginates, filters) ---------------

def _member(uid, *, name="", real="", display="", email=None, deleted=False, is_bot=False):
    return {
        "id": uid,
        "name": name,
        "real_name": real,
        "deleted": deleted,
        "is_bot": is_bot,
        "profile": {"display_name": display, "email": email},
    }


def test_find_users_by_name_matches_across_fields_and_paginates():
    members = [
        _member("U1", name="lpeve", real="Lorenzo Peve", display="Lorenzo", email="l@x.com"),
        _member("U2", name="jdoe", real="Jane Doe", display="Jane"),
        _member("U3", name="lorenzo_bot", real="Lorenzo Bot", is_bot=True),  # bot -> skip
        _member("U4", name="lpeve_old", real="Lorenzo Peve", deleted=True),  # deleted -> skip
    ]
    fake = FakeWebClient(members=members)
    result = SlackClient(fake, "C1").find_users_by_name("lorenzo")

    assert result["ok"] is True
    ids = [u["id"] for u in result["users"]]
    assert ids == ["U1"]
    assert result["users"][0] == {
        "id": "U1",
        "name": "lpeve",
        "real_name": "Lorenzo Peve",
        "display_name": "Lorenzo",
        "email": "l@x.com",
    }


def test_find_users_by_name_returns_all_matches_for_common_name():
    members = [
        _member("U1", real="Lorenzo Peve"),
        _member("U2", real="Lorenzo Garcia"),
        _member("U3", real="Jane Doe"),
    ]
    fake = FakeWebClient(members=members)
    result = SlackClient(fake, "C1").find_users_by_name("lorenzo")
    assert {u["id"] for u in result["users"]} == {"U1", "U2"}


def test_find_users_by_name_rejects_empty():
    result = SlackClient(FakeWebClient(), "C1").find_users_by_name("  ")
    assert result == {"ok": False, "error": "empty name", "users": []}


# --- find_channel_by_name (resolves a name to an id) -------------------------

def test_find_channel_by_name_resolves_exact_match_across_pages():
    channels = [
        {"id": "C1", "name": "random"},
        {"id": "C2", "name": "data_devs"},
        {"id": "C3", "name": "tech"},
    ]
    fake = FakeWebClient(channels=channels)
    # leading '#' and case are ignored
    result = SlackClient(fake, "C0").find_channel_by_name("#Data_Devs")
    assert result == {"ok": True, "id": "C2", "name": "data_devs"}


def test_find_channel_by_name_not_found():
    fake = FakeWebClient(channels=[{"id": "C1", "name": "random"}])
    result = SlackClient(fake, "C0").find_channel_by_name("data_devs")
    assert result["ok"] is False and "not found" in result["error"]


# --- LIVE tests: actually post to Slack, then clean up -----------------------

def _live_client() -> SlackClient:
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    if not token or not channel:
        raise RuntimeError(
            "set SLACK_BOT_TOKEN and SLACK_CHANNEL to run live Slack tests"
        )
    from slack_sdk import WebClient

    return SlackClient(WebClient(token=token), channel)


def test_live_self_check():
    slack = _live_client()
    result = slack.self_check()
    assert result["ok"] is True, result
    assert result.get("bot_id")


def test_live_post_alert_and_reply_then_delete():
    """Post a real alert + thread reply, then delete them by ts (needs chat:write)."""
    slack = _live_client()
    posted: list[str] = []
    try:
        blocks = build_alert_blocks(
            "model.edca_self_test",
            "edw-agent Slack self-test — safe to ignore.",
            failing_sources=["src.self_test"],
            actions_taken=["(connectivity check — no real action)"],
        )
        alert = slack.post_alert(text="edw-agent Slack self-test", blocks=blocks)
        assert alert["ok"] is True, alert
        posted.append(alert["ts"])

        reply = slack.reply_in_thread(alert["ts"], "self-test thread reply")
        assert reply["ok"] is True, reply
        posted.append(reply["ts"])
    finally:
        for ts in posted:
            slack.delete_message(ts)


def test_live_delete_bot_messages_cleans_channel():
    """Exercise the 'delete everything this bot posted' path end-to-end.

    Requires history scope for the channel (channels:history / groups:history /
    im:history) in addition to chat:write.
    """
    slack = _live_client()
    a = slack.post_alert(text="edw-agent cleanup self-test 1")
    b = slack.post_alert(text="edw-agent cleanup self-test 2")
    assert a["ok"] and b["ok"], (a, b)

    result = slack.delete_bot_messages()
    try:
        assert result["ok"] is True, result
        assert a["ts"] in result["deleted"] and b["ts"] in result["deleted"], result
    finally:
        # Belt-and-suspenders: remove anything delete_bot_messages didn't catch.
        for ts in (a["ts"], b["ts"]):
            if ts not in result.get("deleted", []):
                slack.delete_message(ts)
