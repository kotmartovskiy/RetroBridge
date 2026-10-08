"""Script-stripping helper for the pipeline's stage 5 (PROTOCOL §5.5).

Uses the standard library ``html.parser`` so malformed legacy markup degrades
gracefully instead of crashing: unknown tags and broken nesting are preserved
as-is, ``<script>`` bodies and inline event handlers are removed, and
``javascript:``/``vbscript:`` URLs are neutralized so pages stay navigable.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional, Tuple

#: Attributes whose values may carry script URLs.
_URL_ATTRS = frozenset({"href", "src", "action", "formaction", "background", "poster", "data", "xlink:href"})
_SCHEME_PREFIXES = ("javascript:", "vbscript:", "livescript:", "mocha:")


def _is_script_url(value: str) -> bool:
    compact = value.lstrip().lower()
    return compact.startswith(_SCHEME_PREFIXES)


def _escape_attribute(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


class _ScriptStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._chunks: List[str] = []
        self._skip_depth = 0

    def _render_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> str:
        rendered = ["<%s" % tag]
        for name, value in attrs:
            lowered = name.lower()
            if lowered.startswith("on"):
                continue  # inline event handler
            if value is None:
                rendered.append(" %s" % name)
                continue
            if lowered in _URL_ATTRS and _is_script_url(value):
                value = "#"  # neutralized script URL, link stays clickable
            rendered.append(' %s="%s"' % (name, _escape_attribute(value)))
        rendered.append(">")
        return "".join(rendered)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._chunks.append(self._render_starttag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if tag == "script" or self._skip_depth:
            return
        self._chunks.append(self._render_starttag(tag, attrs).rstrip(">") + " />")

    def handle_endtag(self, tag):
        if tag == "script":
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        self._chunks.append("</%s>" % tag)

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def handle_entityref(self, name):
        if not self._skip_depth:
            self._chunks.append("&%s;" % name)

    def handle_charref(self, name):
        if not self._skip_depth:
            self._chunks.append("&#%s;" % name)

    def handle_comment(self, data):
        if not self._skip_depth:
            self._chunks.append("<!--%s-->" % data)

    def handle_decl(self, decl):
        if not self._skip_depth:
            self._chunks.append("<!%s>" % decl)

    def handle_pi(self, data):
        if not self._skip_depth:
            self._chunks.append("<?%s>" % data)

    def unknown_decl(self, data):
        if not self._skip_depth:
            self._chunks.append("<![%s]>" % data)

    def result(self) -> str:
        return "".join(self._chunks)


def strip_scripts(text: str) -> str:
    """Return ``text`` with scripting removed; non-HTML input passes through."""
    if "<" not in text:
        return text
    parser = _ScriptStripper()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # pragma: no cover - parser is tolerant by design
        return text
    return parser.result()
