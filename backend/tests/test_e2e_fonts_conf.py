"""The fontconfig of the e2e browser. A file fontconfig cannot read is ignored without a word, and
on a machine whose default sans-serif font is already Inter (the developer's) nothing in the e2e
suite would show it; on the CI runner, where it is DejaVu Sans, the suite stops with a message
about Inter. This test finds a broken file everywhere."""

import xml.etree.ElementTree as ET
from pathlib import Path

FONTS_CONF = Path(__file__).parent / "e2e" / "fonts.conf"

# The families of the page's sans-serif stack: `ui-sans-serif, system-ui, sans-serif`.
PAGE_FONT_FAMILIES = {"ui-sans-serif", "system-ui", "sans-serif"}


def test_e2e_fonts_conf_is_well_formed_and_prefers_inter_for_the_pages_font_stack():
    root = ET.parse(FONTS_CONF).getroot()  # a parse error is what fontconfig would ignore

    assert root.tag == "fontconfig"
    assert [include.text for include in root.findall("include")] == ["/etc/fonts/fonts.conf"]
    # The aliases must come after the include of the system's configuration: with the include
    # last, fontconfig resolves the families to DejaVu Sans.
    children = [child.tag for child in root]
    assert children.index("include") < children.index("alias"), "<include> must come first"
    assert children.index("include") == 0 and set(children[1:]) == {"alias"}
    preferred = {
        alias.findtext("family"): (
            alias.get("binding"),
            [f.text for f in alias.findall("prefer/family")],
        )
        for alias in root.findall("alias")
    }
    assert preferred == {family: ("strong", ["Inter"]) for family in PAGE_FONT_FAMILIES}
