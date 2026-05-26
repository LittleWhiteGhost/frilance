"""Tests for the small HTML helpers used in every Telegram message."""

from __future__ import annotations

from bot.utils.html import html_escape, safe_url, to_bold_digits, truncate


class TestHtmlEscape:
    def test_escapes_lt_gt_amp(self):
        assert html_escape("<b>&\"'</b>") == "&lt;b&gt;&amp;&quot;&#x27;&lt;/b&gt;"

    def test_handles_none(self):
        assert html_escape(None) == ""

    def test_coerces_non_strings(self):
        assert html_escape(42) == "42"


class TestSafeUrl:
    def test_keeps_https_link(self):
        assert safe_url("https://kwork.ru/projects/1") == "https://kwork.ru/projects/1"

    def test_blocks_javascript(self):
        # `javascript:` URLs would be a clickable XSS via Telegram's HTML mode
        # if we forwarded them.
        assert safe_url("javascript:alert(1)") == "#"

    def test_blocks_data_uri(self):
        assert safe_url("data:text/html,<script>alert(1)</script>") == "#"

    def test_handles_none(self):
        assert safe_url(None) == "#"

    def test_strips_whitespace(self):
        assert safe_url("  https://fl.ru/path  ") == "https://fl.ru/path"

    def test_escapes_html_special_chars_in_url(self):
        # `<` etc. inside the URL must not break out of the surrounding
        # `<a href="...">` once we drop it into a Telegram HTML message.
        out = safe_url("https://example.com/?q=<script>")
        assert "<" not in out
        assert "script" in out


class TestToBoldDigits:
    def test_converts_ascii_digits(self):
        assert to_bold_digits("5 000 ₽") == "𝟓 𝟎𝟎𝟎 ₽"

    def test_leaves_other_chars_untouched(self):
        assert to_bold_digits("договорная") == "договорная"

    def test_handles_empty(self):
        assert to_bold_digits("") == ""
        assert to_bold_digits(None) == ""


class TestTruncate:
    def test_returns_input_when_short(self):
        assert truncate("hello", 100) == "hello"

    def test_truncates_at_word_boundary(self):
        text = "this is a fairly long sentence that definitely exceeds"
        out = truncate(text, 30)
        assert out.endswith("…")
        # Should not break mid-word in the middle.
        assert " " in out or out == "…"

    def test_handles_empty(self):
        assert truncate("", 10) == ""

    def test_handles_none(self):
        assert truncate(None, 10) == ""
