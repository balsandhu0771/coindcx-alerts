"""
Telegram notification sender.
Calls the Bot API directly via requests — no extra library needed.
"""

import logging
import os
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_alert(message: str) -> bool:
    """
    Send a Markdown-formatted message to the configured Telegram chat.
    Returns True on success, False on failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
            "Please add them to your environment secrets."
        )
        return False

    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def test_connection() -> bool:
    """Send a startup ping to confirm Telegram is configured correctly."""
    return send_alert(
        "✅ *CoinDCX Alert Bot started*\n"
        "Monitoring previous 4H candle high/low for fakeouts."
    )
