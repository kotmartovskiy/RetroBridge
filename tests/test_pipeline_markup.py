from core.ir import IrResponse
from core.pipeline import Pipeline

def test_markup_profiles_downconvert_html(registry):
    html = b"<html><body><p>Hello <a href='/x'>link</a></p></body></html>"
    for profile_id, marker in (
        ("wap-wml-classic", b"<wml>"),
        ("chtml-constrained", b"<p>Hello"),
        ("xhtml-mp-constrained", b"<?xml"),
    ):
        response = IrResponse(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=html)
        result = Pipeline.from_plan(("media-type", "markup-downconvert", "charset", "script-strip", "size")).run(response, registry.get(profile_id))
        assert marker in result.body
