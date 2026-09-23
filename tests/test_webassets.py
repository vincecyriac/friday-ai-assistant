from pathlib import Path

from friday.webassets import WEBASSETS_DIR


def test_shared_assets_exist():
    assert (WEBASSETS_DIR / "three.module.min.js").is_file()
    orb = WEBASSETS_DIR / "orb.js"
    assert orb.is_file()
    src = orb.read_text()
    assert "export function mountOrb" in src
    assert "window.FridayOrb" in src
    assert 'import * as THREE from "three"' in src


def test_desktop_gui_points_at_the_shared_copy():
    import friday.desktop as desktop

    gui = Path(desktop.__file__).parent / "web_gui"
    assert not (gui / "orb.js").exists()
    assert not (gui / "vendor" / "three.module.min.js").exists()
    html = (gui / "index.html").read_text()
    assert '"three": "./shared/three.module.min.js"' in html
    assert 'src="shared/orb.js"' in html


def test_import_map_addresses_are_spec_valid():
    """An import-map address must be an absolute URL or start with / ./ or ../ —
    a bare "shared/x.js" is silently dropped and every `import "three"` fails."""
    import json
    import re

    for html_path in (Path("friday/desktop/web_gui/index.html"),
                      Path("friday/sentinel/dashboard/index.html")):
        html = html_path.read_text()
        for block in re.findall(r'<script type="importmap">(.*?)</script>', html, re.S):
            for specifier, address in json.loads(block).get("imports", {}).items():
                assert address.startswith(("/", "./", "../", "http://", "https://")), \
                    f"{html_path}: import map address {address!r} for {specifier!r} is not resolvable"
