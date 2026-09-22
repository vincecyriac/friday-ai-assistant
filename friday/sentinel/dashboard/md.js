/* Markdown → DOM for assistant replies. Escape-first by construction: the input is
   only ever placed into text nodes, so no tag or attribute can come from it.
   Supported: #/##/### headings, paragraphs, - * lists and 1. lists (one nesting level),
   > quotes, --- rules, fenced code, `code`, **bold**, *italic*, [text](http(s) url). */

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\))/g;

function text(value) {
  return document.createTextNode(value);
}

function element(tag, children, attrs) {
  const node = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (children) children.forEach((c) => node.append(c));
  return node;
}

export function inline(source) {
  const out = [];
  const re = new RegExp(INLINE.source, "g");     // fresh per call: inline() recurses for bold/italic
  let last = 0;
  let m;
  while ((m = re.exec(source)) !== null) {
    if (m.index > last) out.push(text(source.slice(last, m.index)));
    if (m[1]) out.push(element("code", [text(m[1].slice(1, -1))]));
    else if (m[2]) out.push(element("strong", inline(m[2].slice(2, -2))));
    else if (m[3]) out.push(element("em", inline(m[3].slice(1, -1))));
    else if (m[4]) out.push(element("a", inline(m[5]), { href: m[6], target: "_blank", rel: "noopener noreferrer" }));
    last = m.index + m[0].length;
  }
  if (last < source.length) out.push(text(source.slice(last)));
  return out;
}

const LIST_ITEM = /^(\s*)([-*]|\d+\.)\s+(.*)$/;

function parseList(lines, start, indent) {
  const first = LIST_ITEM.exec(lines[start]);
  const ordered = /\d+\./.test(first[2]);
  const list = element(ordered ? "ol" : "ul");
  let i = start;
  while (i < lines.length) {
    const m = LIST_ITEM.exec(lines[i]);
    if (!m) break;
    const depth = m[1].length;
    if (depth < indent) break;
    if (depth > indent) {
      const [nested, next] = parseList(lines, i, depth);
      if (!list.lastChild) list.append(element("li"));
      list.lastChild.append(nested);
      i = next;
      continue;
    }
    list.append(element("li", inline(m[3])));
    i += 1;
  }
  return [list, i];
}

function isBlockStart(line) {
  return /^(#{1,3}\s|```|>|\s*([-*]|\d+\.)\s|(-{3,}|\*{3,})\s*$)/.test(line);
}

export function render(markdown) {
  const frag = document.createDocumentFragment();
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const lang = line.slice(3).trim().replace(/[^A-Za-z0-9_+-]/g, "");
      const buf = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) { buf.push(lines[i]); i += 1; }
      i += 1;
      const code = element("code", [text(buf.join("\n"))]);
      if (lang) code.setAttribute("class", `language-${lang}`);
      frag.append(element("pre", [code]));
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      frag.append(element(["h3", "h4", "h5"][heading[1].length - 1], inline(heading[2].trim())));
      i += 1;
      continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) { frag.append(element("hr")); i += 1; continue; }
    if (line.startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].startsWith(">")) { buf.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      frag.append(element("blockquote", inline(buf.join(" "))));
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const [list, next] = parseList(lines, i, LIST_ITEM.exec(line)[1].length);
      frag.append(list);
      i = next;
      continue;
    }
    const buf = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) { buf.push(lines[i]); i += 1; }
    frag.append(element("p", inline(buf.join(" "))));
  }
  return frag;
}
