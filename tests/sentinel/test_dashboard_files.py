import json
import re
import shutil
import subprocess
import time

import pytest

from friday.sentinel.api import create_app
from friday.sentinel.auth import hash_password
from friday.sentinel.web import DASHBOARD_DIR

HTML = DASHBOARD_DIR / "index.html"
CSS = DASHBOARD_DIR / "tailwind.css"


def _js_files():
    files = list(DASHBOARD_DIR.glob("*.js")) + list((DASHBOARD_DIR / "views").glob("*.js"))
    return sorted(p for p in files if p.name != "tailwind.config.js")


def test_shell_files_exist_and_use_relative_urls():
    html = HTML.read_text()
    assert 'href="static/tailwind.css"' in html and 'type="module" src="static/app.js"' in html
    assert 'id="view"' in html and 'id="nav"' in html and 'id="toast"' in html
    assert not (DASHBOARD_DIR / "style.css").exists()
    assert CSS.is_file() and CSS.stat().st_size > 5_000
    for path in _js_files():
        src = path.read_text()
        assert not re.search(r"""["']/(api|auth|static|config|ws|nodes)""", src), f"{path.name}: absolute URL"
        for target in re.findall(r"""from\s+["']([^"']+)["']""", src):
            assert target.startswith("./") or target.startswith("../"), f"{path.name}: import {target!r}"
    assert "X-FRIDAY-Client" in (DASHBOARD_DIR / "api.js").read_text()


def _class_tokens() -> set[str]:
    tokens: set[str] = set()
    for m in re.finditer(r'class="([^"]*)"', HTML.read_text()):
        tokens.update(m.group(1).split())
    for path in _js_files():
        src = path.read_text()
        for m in re.finditer(r'\bclass:\s*"([^"]*)"', src):
            tokens.update(m.group(1).split())
        for m in re.finditer(r'\bcls\(([^)]*)\)', src):
            for literal in re.findall(r'"([^"]*)"', m.group(1)):
                tokens.update(literal.split())
        for m in re.finditer(r'\bclassName\s*=\s*"([^"]*)"', src):
            tokens.update(m.group(1).split())
        for m in re.finditer(r'classList\.(?:add|remove|toggle)\(\s*"([^"]*)"', src):
            tokens.update(m.group(1).split())
    return tokens


def _selector(token: str) -> str:
    return "." + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", token)


# `dark` on <html> is Tailwind's darkMode selector, not a utility; no rule is emitted for it.
NOT_UTILITIES = {"dark"}


def test_css_covers_every_class():
    css = CSS.read_text()
    tokens = _class_tokens() - NOT_UTILITIES
    assert len(tokens) > 50
    missing = sorted(t for t in tokens if not re.search(re.escape(_selector(t)) + r"(?![\w-])", css))
    assert not missing, f"tailwind.css is stale — run deploy/build_css.sh; missing: {missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_modules_parse(path):
    with path.open("rb") as f:
        proc = subprocess.run(["node", "--input-type=module", "--check"], stdin=f, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


async def test_real_dashboard_is_served(aiohttp_client, services):
    await services.store.user_upsert("vince", hash_password("pw"), ts=time.time())
    client = await aiohttp_client(create_app(services))
    resp = await client.get("/login")
    assert resp.status == 200 and 'id="view"' in await resp.text()
    for path in ("/static/tailwind.css", "/static/app.js", "/static/api.js", "/static/socket.js",
                 "/static/ui.js", "/static/views/login.js", "/static/views/settings.js", "/static/views/tokens.js",
                 "/static/views/overview.js", "/static/views/activity.js", "/static/views/controls.js",
                 "/static/md.js", "/static/views/assistant.js",
                 "/shared/orb.js", "/shared/three.module.min.js", "/static/views/voice.js"):
        assert (await client.get(path)).status == 200, path
    assert (await client.get("/", allow_redirects=False)).status == 302


def test_app_registers_real_views_not_placeholders():
    app_js = (DASHBOARD_DIR / "app.js").read_text()
    for name in ("overview", "activity", "controls", "assistant"):
        assert f'placeholder("{name.capitalize()}")' not in app_js, name
        assert f'import * as {name} from "./views/{name}.js"' in app_js


def test_md_renderer_never_uses_innerhtml():
    src = (DASHBOARD_DIR / "md.js").read_text()
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "outerHTML" not in src
    assert "createElement" in src and "createTextNode" in src or "textContent" in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_md_renderer_escapes_and_renders(tmp_path):
    """Drive md.js in node with a tiny DOM shim: output must contain no raw tag from input."""
    shim = tmp_path / "run.mjs"
    shim.write_text(f'''
const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
class Node {{ constructor(tag) {{ this.tag = tag; this.children = []; this.attrs = {{}}; }}
  append(...items) {{ items.forEach((c) => this.children.push(typeof c === "string" ? {{ text: c }} : c)); }}
  setAttribute(k, v) {{ this.attrs[k] = v; }}
  get lastChild() {{ return this.children[this.children.length - 1] || null; }}
  serialize() {{ const inner = this.children.map((c) => c.serialize ? c.serialize() : esc(c.text)).join("");
    const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${{k}}="${{v}}"`).join("");
    return this.tag === "#fragment" ? inner : `<${{this.tag}}${{attrs}}>${{inner}}</${{this.tag}}>`; }} }}
globalThis.Node = Node;
globalThis.document = {{ createElement: (t) => new Node(t), createDocumentFragment: () => new Node("#fragment"),
  createTextNode: (t) => ({{ text: String(t) }}) }};
const {{ render }} = await import({json.dumps(str(DASHBOARD_DIR / "md.js"))});
const cases = [
  "# Title\\n\\nHello **bold** and *it* and `code` <script>alert(1)</script>",
  "- one\\n- two\\n  - nested\\n\\n1. first\\n2. second",
  "```py\\nprint('x')\\n```\\n> quote\\n---\\n[link](https://x.example/a?b=1) [bad](javascript:alert(1))",
  "",
  "unterminated **bold and `code",
];
for (const c of cases) process.stdout.write(render(c).serialize() + "\\n===\\n");
''')
    proc = subprocess.run(["node", str(shim)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.split("\n===\n")
    assert "<script>" not in out[0] and "&lt;script&gt;alert(1)" in out[0]      # text node, not a tag
    assert "<h3>Title</h3>" in out[0] and "<strong>bold</strong>" in out[0] and "<em>it</em>" in out[0] and "<code>code</code>" in out[0]
    assert "<ul>" in out[1] and out[1].count("<li>") == 5 and "<ol>" in out[1]
    assert '<pre><code class="language-py">print(\'x\')</code></pre>' in out[2]
    assert "<blockquote>quote</blockquote>" in out[2] and "<hr>" in out[2]
    assert 'href="https://x.example/a?b=1"' in out[2] and 'rel="noopener noreferrer"' in out[2]
    assert 'href="javascript:' not in out[2] and "[bad](javascript:alert(1))" in out[2]   # literal text, no link
    assert out[3] == ""
    assert "**bold and `code" in out[4]


def test_assistant_view_wires_voice_and_the_orb():
    src = (DASHBOARD_DIR / "views" / "assistant.js").read_text()
    assert 'import { createVoice } from "./voice.js"' in src
    assert "orbStage" in src
    html = HTML.read_text()
    assert '"three": "./shared/three.module.min.js"' in html    # importmap for the orb


def test_voice_module_uses_relative_urls_and_the_shared_orb():
    src = (DASHBOARD_DIR / "views" / "voice.js").read_text()
    assert "shared/orb.js" in src
    assert "voice/ws" in src and not re.search(r'["\']/voice/ws', src)
    assert "getUserMedia" in src and "24000" in src and "16000" in src
    # the AudioContext must be created/resumed inside the click handler's call chain
    assert "resume()" in src


async def test_static_assets_must_be_revalidated(aiohttp_client, services):
    """FileResponse sends ETag/Last-Modified but no Cache-Control, so browsers fall
    back to heuristic freshness and serve an edited module from cache without ever
    asking. "no-cache" keeps the cache but forces revalidation (a 304 when unchanged)."""
    client = await aiohttp_client(create_app(services))
    for path in ("/static/app.js", "/static/views/assistant.js", "/static/tailwind.css",
                 "/shared/orb.js"):
        resp = await client.get(path)
        assert resp.status == 200, path
        assert "no-cache" in resp.headers.get("Cache-Control", ""), f"{path} may be served stale"
