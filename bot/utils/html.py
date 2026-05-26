"""HTML rendering helpers for Telegram messages.

Telegram's HTML parser only accepts a small whitelist of tags. To avoid 400
errors when free-form text from parsers contains `<`, `>`, `&` or quotes, we
escape every value before substituting it into a template.
"""

from html import escape as _stdlib_escape
from urllib.parse import quote as _url_quote
from urllib.parse import urlparse


def html_escape(value: object) -> str:
    """Escape a value for safe inclusion inside Telegram HTML messages."""
    if value is None:
        return ""
    return _stdlib_escape(str(value), quote=True)


def safe_url(value: str | None) -> str:
    """Return an http(s) URL safe for use in an `<a href=...>`.

    Falls back to `#` if the URL is not a valid http(s) URL. The result is
    additionally HTML-escaped so quotes in the URL cannot break out of the
    attribute.
    """
    if not value:
        return "#"
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "#"
    # Re-encode the path/query to strip control characters and spaces.
    safe = (
        f"{parsed.scheme}://{parsed.netloc}"
        f"{_url_quote(parsed.path, safe='/%')}"
    )
    if parsed.query:
        safe += f"?{_url_quote(parsed.query, safe='=&%')}"
    if parsed.fragment:
        safe += f"#{_url_quote(parsed.fragment, safe='%')}"
    return html_escape(safe)


_BOLD_DIGITS = str.maketrans("0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗")


def to_bold_digits(text: str) -> str:
    """Replace ASCII digits with mathematical bold digits (𝟎-𝟗).

    Useful for visually emphasising numeric values inside Telegram messages
    where real font-size control is not available.
    """
    if not text:
        return ""
    return str(text).translate(_BOLD_DIGITS)


def tg_emoji(emoji_id: str | None, fallback: str) -> str:
    """Render a Telegram custom emoji ("icon") with a Unicode fallback.

    Telegram Premium clients render the icon identified by `emoji_id`; other
    clients fall back to the inner `fallback` glyph. Returns the bare
    `fallback` when `emoji_id` is empty.

    NOTE: custom emoji are only rendered inside *message bodies* (with HTML
    parse mode). They do **not** render in inline-keyboard button text — keep
    raw Unicode there.
    """
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{html_escape(emoji_id)}">{fallback}</tg-emoji>'


def truncate(text: str, length: int, suffix: str = "…") -> str:
    """Truncate `text` to `length` characters at a word boundary if possible."""
    if not text or len(text) <= length:
        return text or ""
    if length <= len(suffix):
        return suffix[:length]
    cut = text[: length - len(suffix)]
    space = cut.rfind(" ")
    # Only break at a word boundary if it isn't ridiculously early.
    if space >= length // 2:
        cut = cut[:space]
    return cut.rstrip() + suffix
