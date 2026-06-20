"""File-output dispatcher.

Forwards files written by Claude's Write tool to Telegram (§5).
Sends photos as images, documents as documents, and unknown extensions
as a text notice with the path.
"""

from __future__ import annotations

import html
import io
import logging
from pathlib import Path

import telegram.error
from telegram import Bot

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024    # 10 MB
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # 50 MB

PHOTO_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
)
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".csv", ".xlsx", ".html", ".md", ".txt"}
)


async def dispatch_file(path: Path, bot: Bot, chat_id: int) -> None:
    """Send a file created by Claude's Write tool to Telegram.

    Per §5:
    - Photos (.png / .jpg / .jpeg / .webp / .gif / .svg): send_photo if ≤10 MB,
      else text notice.
    - Documents (.pdf / .csv / .xlsx / .html / .md / .txt): send_document if ≤50 MB,
      else text notice.
    - Unknown extensions: text notice with path only.
    """
    suffix = path.suffix.lower()

    if suffix in PHOTO_EXTENSIONS:
        await _send_photo(path, bot, chat_id)
    elif suffix in DOCUMENT_EXTENSIONS:
        await _send_document(path, bot, chat_id)
    else:
        logger.debug("Unknown extension %r for %s; sending path notice", suffix, path)
        await _send_path_notice(path, bot, chat_id)


async def send_screenshot_bytes(raw: bytes, bot: Bot, chat_id: int) -> None:
    """Send in-memory screenshot bytes (from a tool result image block) to Telegram.

    Pipeline:
    1. Normalize via image_processor (resize ≤1568 px, JPEG q85) — handles
       Telegram's dimension limit. On failure falls back to sending raw bytes.
    2. Try send_photo. On TelegramError (size rejection, etc.) fall back to
       send_document with filename "screenshot.jpg".
    3. If send_document also fails, re-raise so the caller can log-and-skip.
    """
    from tgclaude.image_processor import normalize_image  # lazy import avoids circular dep

    try:
        jpeg_bytes, _ = await normalize_image(raw)
    except ValueError as exc:
        logger.warning(
            "send_screenshot_bytes: normalize_image failed (%d bytes): %s; using raw bytes",
            len(raw),
            exc,
        )
        jpeg_bytes = raw

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(jpeg_bytes),
            caption="📸 Screenshot",
        )
    except telegram.error.TelegramError as photo_exc:
        logger.warning(
            "send_screenshot_bytes: send_photo failed (%d bytes): %s; falling back to document",
            len(jpeg_bytes),
            photo_exc,
        )
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(jpeg_bytes),
                filename="screenshot.jpg",
                caption="📸 Screenshot",
            )
        except Exception as doc_exc:
            raise photo_exc from doc_exc


# ---------------------------------------------------------------------------
# Private senders
# ---------------------------------------------------------------------------


async def _send_photo(path: Path, bot: Bot, chat_id: int) -> None:
    size = _stat_size(path)
    if size is None:
        return

    if size > MAX_PHOTO_BYTES:
        logger.info("Photo %s is %d bytes (>10 MB); sending path notice", path, size)
        await _send_oversize_notice(path, "photo", MAX_PHOTO_BYTES, bot, chat_id)
        return

    logger.debug("Sending photo %s (%d bytes)", path, size)
    try:
        with path.open("rb") as fh:
            await bot.send_photo(chat_id=chat_id, photo=fh, caption=str(path))
    except Exception as exc:
        logger.warning("send_photo failed for %s: %s; sending path notice", path, exc)
        await _send_path_notice(path, bot, chat_id)


async def _send_document(path: Path, bot: Bot, chat_id: int) -> None:
    size = _stat_size(path)
    if size is None:
        return

    if size > MAX_DOCUMENT_BYTES:
        logger.info("Document %s is %d bytes (>50 MB); sending path notice", path, size)
        await _send_oversize_notice(path, "document", MAX_DOCUMENT_BYTES, bot, chat_id)
        return

    logger.debug("Sending document %s (%d bytes)", path, size)
    try:
        with path.open("rb") as fh:
            await bot.send_document(chat_id=chat_id, document=fh, filename=path.name)
    except Exception as exc:
        logger.warning(
            "send_document failed for %s: %s; sending path notice", path, exc
        )
        await _send_path_notice(path, bot, chat_id)


async def _send_path_notice(path: Path, bot: Bot, chat_id: int) -> None:
    """Tell the user where the file lives on the VPS."""
    await bot.send_message(
        chat_id=chat_id,
        text=f"📄 File written: <code>{html.escape(str(path))}</code>",
        parse_mode="HTML",
    )


async def _send_oversize_notice(
    path: Path, kind: str, limit_bytes: int, bot: Bot, chat_id: int
) -> None:
    limit_mb = limit_bytes // (1024 * 1024)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"📄 File written: <code>{html.escape(str(path))}</code>\n"
            f"<i>({kind} exceeds {limit_mb} MB — retrieve it over SSH)</i>"
        ),
        parse_mode="HTML",
    )


def _stat_size(path: Path) -> int | None:
    """Return file size in bytes, or None if the file cannot be stat'd."""
    try:
        return path.stat().st_size
    except OSError as exc:
        logger.warning("Cannot stat %s: %s", path, exc)
        return None
