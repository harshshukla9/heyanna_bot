#!/usr/bin/env python3
"""
Telegram announcement listener (historical + live).

What it does:
1) Backfills historical messages from a target Telegram group/channel.
2) Parses signal-like announcement messages.
3) Appends parsed signals to a JSONL file.
4) Keeps listening for new/edited posts and logs parsed signals in real time.

This uses Telegram USER API credentials (Telethon). You must log in as a user
(phone number), not as a bot. GetHistoryRequest / iter_messages are not allowed
for bot accounts; using a bot token or a bot session will raise BotMethodInvalidError.

Required env:
  TELEGRAM_API_ID
  TELEGRAM_API_HASH
  TELEGRAM_SIGNAL_CHAT        # e.g. @channel_username, -100123..., or invite target

Optional env:
  TELEGRAM_SESSION=tgbotanna_signal_listener
  TELEGRAM_SIGNAL_OUTPUT=logs/announcement_signals.jsonl
  TELEGRAM_SIGNAL_HISTORY_DAYS=7    # backfill window in days
  TELEGRAM_SIGNAL_HISTORY_LIMIT=0   # optional hard cap (0 = no cap)
  TELEGRAM_SIGNAL_LOG_RAW=0        # set 1 to log unmatched messages too
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Place trade before this time: signal arrival + 5 min or 15 min etc.
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

from dotenv import load_dotenv
from telethon import TelegramClient, events


@dataclass
class ListenerConfig:
    api_id: int
    api_hash: str
    chat_ref: str
    session: str
    output_path: Path
    history_days: int
    history_limit: int
    log_raw: bool


def _parse_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _load_config_from_env() -> ListenerConfig:
    api_id_raw = (os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip()
    chat_ref = (os.getenv("TELEGRAM_SIGNAL_CHAT") or "").strip()
    session = (os.getenv("TELEGRAM_SESSION") or "tgbotanna_signal_listener").strip()
    output = (os.getenv("TELEGRAM_SIGNAL_OUTPUT") or "logs/announcement_signals.jsonl").strip()
    history_days_raw = (os.getenv("TELEGRAM_SIGNAL_HISTORY_DAYS") or "7").strip()
    history_limit_raw = (os.getenv("TELEGRAM_SIGNAL_HISTORY_LIMIT") or "0").strip()
    log_raw = _parse_bool(os.getenv("TELEGRAM_SIGNAL_LOG_RAW"), default=False)

    if not api_id_raw or not api_hash or not chat_ref:
        raise ValueError(
            "Missing required env. Need TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SIGNAL_CHAT."
        )

    try:
        api_id = int(api_id_raw)
    except ValueError as e:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from e

    try:
        history_days = int(history_days_raw)
    except ValueError:
        history_days = 7
    history_days = max(1, history_days)

    try:
        history_limit = int(history_limit_raw)
    except ValueError:
        history_limit = 0

    return ListenerConfig(
        api_id=api_id,
        api_hash=api_hash,
        chat_ref=chat_ref,
        session=session,
        output_path=Path(output),
        history_days=history_days,
        history_limit=history_limit,
        log_raw=log_raw,
    )


def _build_message_key(chat_id: int | None, message_id: int | None) -> str:
    return f"{chat_id or 0}:{message_id or 0}"


def _load_seen_message_keys(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            key = obj.get("message_key")
            if isinstance(key, str) and key:
                seen.add(key)
    except Exception:
        pass
    return seen


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_signal_from_text(text: str) -> dict[str, Any] | None:
    """
    Lightweight parser for announcement-style short-horizon market calls.
    Output is geared for Polymarket: signal "YES" = Buy YES (bullish/UP),
    signal "NO" = Buy NO (bearish/DOWN). Parse-only, no trade execution.
    """
    src = (text or "").strip()
    if not src:
        return None
    low = src.lower()

    # Only parse messages that are signal announcements (e.g. "METAZEN SIGNAL — 5M").
    if "signal" not in low or "—" not in src:
        return None
    # Skip outcome reports (❌ LOSS — / ✅ WIN —), not entry signals.
    if re.search(r"LOSS\s*—|WIN\s*—", src):
        return None

    # These are Bitcoin signals; "METAZEN" in the header is the channel brand, not the asset.
    asset = "BTC"

    timeframe = None
    if re.search(r"\b(1m|1\s*min(?:ute)?s?)\b", low):
        timeframe = "1m"
    elif re.search(r"\b(5m|5\s*min(?:ute)?s?)\b", low):
        timeframe = "5m"
    elif re.search(r"\b(15m|15\s*min(?:ute)?s?)\b", low):
        timeframe = "15m"
    elif re.search(r"\b(30m|30\s*min(?:ute)?s?)\b", low):
        timeframe = "30m"
    elif re.search(r"\b(1h|60\s*min(?:ute)?s?)\b", low):
        timeframe = "1h"

    # Polymarket side: YES = buy yes (bullish), NO = buy no (bearish).
    side = None
    if re.search(r"\b(long|buy\s*yes|yes|bull|up|call)\b", low) or "🟢" in src:
        side = "YES"
    elif re.search(r"\b(short|buy\s*no|no|bear|down|put)\b", low) or "🔴" in src or re.search(r"▼\s*down", low):
        side = "NO"

    # Skip non-actionable chatter.
    if not timeframe and not side:
        return None

    direction = "UP" if side == "YES" else "DOWN" if side == "NO" else None

    # Polymarket market question: "Will Bitcoin go up in next 5 min?" etc.
    market = None
    if timeframe:
        tf_label = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hour"}.get(timeframe, timeframe)
        market = f"Will Bitcoin go up in next {tf_label}?"

    confidence_pct = None
    m_conf = re.search(r"\b(\d{1,3})\s*%", src)
    if m_conf:
        try:
            confidence_pct = max(0, min(100, int(m_conf.group(1))))
        except Exception:
            confidence_pct = None

    return {
        "asset": asset,
        "timeframe": timeframe,
        "market": market,
        "signal": side,
        "direction": direction,
        "confidence_pct": confidence_pct,
    }


async def run_listener(cfg: ListenerConfig) -> None:
    seen_keys = _load_seen_message_keys(cfg.output_path)
    print(f"[listener] loaded {len(seen_keys)} existing message keys from {cfg.output_path}")

    client = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash)
    await client.start()
    me = await client.get_me()
    if me and getattr(me, "bot", getattr(me, "is_bot", False)):
        session_path = Path(cfg.session + ".session")
        raise RuntimeError(
            "This script must run as a Telegram USER, not a bot. "
            "GetHistoryRequest/iter_messages are not allowed for bots. "
            f"Delete the session file and re-run to log in with your user account (phone): "
            f"rm -f {session_path}"
        )
    chat = await client.get_entity(cfg.chat_ref)
    chat_id = int(getattr(chat, "id", 0) or 0)
    chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or cfg.chat_ref
    print(f"[listener] connected to chat={chat_title} id={chat_id}")

    async def process_message(msg: Any, source: str) -> None:
        text = (getattr(msg, "message", None) or "").strip()
        msg_id = int(getattr(msg, "id", 0) or 0)
        if not msg_id:
            return
        key = _build_message_key(chat_id, msg_id)
        if key in seen_keys:
            return

        parsed = parse_signal_from_text(text)
        if not parsed:
            if cfg.log_raw and text:
                payload = {
                    "source": source,
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "message_id": msg_id,
                    "message_date": str(getattr(msg, "date", None)),
                    "sender_id": getattr(msg, "sender_id", None),
                    "message_key": key,
                    "parsed": False,
                    "logged_at": int(time.time()),
                }
                _append_jsonl(cfg.output_path, payload)
                seen_keys.add(key)
            return

        msg_date = getattr(msg, "date", None)
        signal_ts = int(msg_date.timestamp()) if msg_date else int(time.time())
        try:
            signal_at = msg_date.isoformat() if msg_date else None
        except Exception:
            signal_at = None

        # Market end = signal arrival + timeframe (place trade before this).
        tf = parsed.get("timeframe")
        delta_sec = TIMEFRAME_SECONDS.get(tf, 300) if tf else 300
        market_end_ts = signal_ts + delta_sec
        try:
            market_end_at = datetime.fromtimestamp(market_end_ts, tz=timezone.utc).isoformat()
        except Exception:
            market_end_at = None

        payload = {
            "source": source,  # historical | live:new | live:edit
            "chat_id": chat_id,
            "chat_title": chat_title,
            "message_id": msg_id,
            "message_date": str(msg_date),
            "signal_ts": signal_ts,
            "signal_at": signal_at,
            "market_end_ts": market_end_ts,
            "market_end_at": market_end_at,
            "sender_id": getattr(msg, "sender_id", None),
            "message_key": key,
            "parsed": True,
            **parsed,
        }
        _append_jsonl(cfg.output_path, payload)
        seen_keys.add(key)
        out = {**parsed, "signal_ts": signal_ts, "signal_at": signal_at, "market_end_ts": market_end_ts, "market_end_at": market_end_at}
        print(f"[listener] {source} signal: {json.dumps(out, ensure_ascii=False)}")

    # Historical backfill: last N days (default 7) + optional hard limit.
    now_ts = int(time.time())
    cutoff_ts = now_ts - (cfg.history_days * 24 * 60 * 60)
    limit = None if cfg.history_limit <= 0 else cfg.history_limit
    scanned = 0
    async for msg in client.iter_messages(chat, limit=limit):
        msg_date = getattr(msg, "date", None)
        msg_ts = int(msg_date.timestamp()) if msg_date else 0
        if msg_ts and msg_ts < cutoff_ts:
            # iter_messages default order is newest -> oldest, so we can stop early.
            break
        await process_message(msg, "historical")
        scanned += 1
        if scanned % 500 == 0:
            print(f"[listener] scanned {scanned} historical messages...")
    print(
        f"[listener] historical scan complete. scanned={scanned} "
        f"(window={cfg.history_days}d)"
    )

    # Live listeners
    @client.on(events.NewMessage(chats=chat))
    async def _on_new(event):
        await process_message(event.message, "live:new")

    @client.on(events.MessageEdited(chats=chat))
    async def _on_edit(event):
        await process_message(event.message, "live:edit")

    print("[listener] live mode started. Waiting for new messages...")
    await client.run_until_disconnected()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill and live-listen Telegram announcement signals."
    )
    parser.add_argument(
        "--chat",
        default=None,
        help="Override TELEGRAM_SIGNAL_CHAT (e.g. @groupusername).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override TELEGRAM_SIGNAL_OUTPUT JSONL path.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=None,
        help="Backfill only this many past days (default: env TELEGRAM_SIGNAL_HISTORY_DAYS or 7).",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=None,
        help="Optional hard cap on historical messages scanned (0 = no cap).",
    )
    parser.add_argument(
        "--log-raw",
        action="store_true",
        help="Also log unmatched raw messages to output.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    cfg = _load_config_from_env()
    args = parse_args()

    if args.chat:
        cfg.chat_ref = args.chat.strip()
    if args.output:
        cfg.output_path = Path(args.output.strip())
    if args.history_days is not None:
        cfg.history_days = max(1, int(args.history_days))
    if args.history_limit is not None:
        cfg.history_limit = int(args.history_limit)
    if args.log_raw:
        cfg.log_raw = True

    try:
        asyncio.run(run_listener(cfg))
    except KeyboardInterrupt:
        print("\n[listener] stopped.")
    except Exception as e:
        print(f"[listener] fatal error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()

