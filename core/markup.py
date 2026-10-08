"""Conservative legacy-markup down-conversion for Phase 2B.

This is not a browser engine. It converts a small safe HTML subset into WML 1.x,
cHTML and XHTML-MP, dropping unsupported structure rather than inventing behavior.
"""
from __future__ import annotations
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

_TARGETS = ("wml-1.x", "chtml-1.x", "xhtml-mp")

class _BasicConverter(HTMLParser):
    ALLOWED = {
        "chtml-1.x": {"html", "head", "title", "body", "p", "br", "a", "b", "strong", "i", "em", "ul", "ol", "li", "h1", "h2", "h3", "hr", "pre", "form", "input"},
        "xhtml-mp": {"html", "head", "title", "body", "p", "br", "a", "b", "strong", "i", "em", "ul", "ol", "li", "h1", "h2", "h3", "hr", "pre", "form", "input"},
    }
    VOID = {"br", "hr", "input"}
    def __init__(self, target: str) -> None:
        super().__init__(convert_charrefs=False)
        self.target = target
        self.out: List[str] = []
        self.skip = 0
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag not in self.ALLOWED[self.target]:
            return
        rendered = ["<", tag]
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name in ("style", "id", "class"):
                continue
            if name in ("href", "src", "action", "method", "type", "name", "value", "alt", "maxlength") and value is not None:
                if name == "href" and value.lower().startswith(("javascript:", "vbscript:")):
                    value = "#"
                rendered.append(' %s="%s"' % (name, value.replace("&", "&amp;").replace('"', "&quot;")))
        rendered.append(" />" if tag in self.VOID else ">")
        self.out.append("".join(rendered))
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed"):
            if self.skip:
                self.skip -= 1
            return
        if not self.skip and tag in self.ALLOWED[self.target] and tag not in self.VOID:
            self.out.append("</%s>" % tag)
    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)
    def handle_entityref(self, name: str) -> None:
        if not self.skip:
            self.out.append("&%s;" % name)
    def handle_charref(self, name: str) -> None:
        if not self.skip:
            self.out.append("&#%s;" % name)
    def result(self) -> str:
        return "".join(self.out)

def _to_wml(text: str) -> str:
    parser = _BasicConverter("chtml-1.x")
    parser.feed(text)
    parser.close()
    body = parser.result()
    body = re.sub(r"<html[^>]*>|</html>|<head[^>]*>.*?</head>", "", body, flags=re.I | re.S)
    body = re.sub(r"<body[^>]*>", "", body, flags=re.I)
    body = re.sub(r"</body>", "", body, flags=re.I)
    body = re.sub(r"<a\b([^>]*)>(.*?)</a>", r'<anchor\1>\2</anchor>', body, flags=re.I | re.S)
    return '<?xml version="1.0"?>\n<!DOCTYPE wml PUBLIC "-//WAPFORUM//DTD WML 1.1//EN" "http://www.wapforum.org/DTD/wml_1.1.xml">\n<wml><card id="main" title="RetroBridge"><p>%s</p></card></wml>' % body

def downconvert(text: str, markup_profiles: Tuple[str, ...]) -> Tuple[str, Optional[str]]:
    targets = tuple(markup_profiles)
    if "html-3.2" in targets or "html-4" in targets:
        return text, None
    if "xhtml-mp" in targets:
        parser = _BasicConverter("xhtml-mp")
        parser.feed(text); parser.close()
        return '<?xml version="1.0" encoding="UTF-8"?>' + parser.result(), "application/vnd.wap.xhtml+xml"
    if "chtml-1.x" in targets:
        parser = _BasicConverter("chtml-1.x")
        parser.feed(text); parser.close()
        return parser.result(), "text/html"
    if "wml-1.x" in targets:
        return _to_wml(text), "text/vnd.wap.wml"
    return text, None
