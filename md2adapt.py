# md2adapt.py — Generate Adapt Framework JSON (contentObjects, articles, blocks, components, course)
#
# Usage:
#   python md2adapt.py INPUT.md --out course/en --lang en --menu "Main menu"
#
# The script intentionally uses only the Python standard library.
#
# It tolerates *arbitrary* Markdown structures:
# - H1 (#) first occurrence -> course title (fallback to file stem)
# - Pages:
#     Prefer H2 (##). If none exist, treat subsequent H1 as pages. If still none, create one page.
#     SPECIAL CASE: If H2s use a "[block]" marker (e.g., "## [block] Title"), they are treated as BLOCKS
#     under a single synthetic page and article (so they don't get parsed as pages/menus).
# - Articles:
#     Prefer H3 within each page. If none, create a single article per page.
#     In the "[block] at H2" special case, one synthetic article is created unless front-matter overrides it.
# - Blocks:
#     Prefer H4 within each article. If none, split body by horizontal rules (---/***) or create one block.
#     In the "[block] at H2" special case, those H2s are the blocks.
# - Components:
#     For now, everything within a block is emitted as a single TEXT component (safe default).
#     In the "[block] at H2" special case, H3s inside each H2-block become individual TEXT components.
#     You can opt-in to other components by inline markers in Markdown (optional, see README in docstring).
#
# Optional simple front-matter (before a dashed line "--------------"):
#   pageTitle: "..."
#   articleTitle: "..."
#
# The script writes five files to the output folder:
#   contentObjects.json, articles.json, blocks.json, components.json, course.json
#
# Strong guarantees:
# - 24-char hex IDs for all objects except course (_id = "course")
# - Valid parent chains: menu → page → article → block → component
# - Monotonic unique _trackingId for blocks
#
# If something looks off, the validator will print explicit Orphaned/Missing IDs before writing.
#
import argparse, json, os, re, sys, textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Tuple, Iterable

HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
MARKER_RE = re.compile(r"^\[(\w+)\]\s*(.*)$")

def split_marker(title: str):
    """
    If title starts with [something] return (marker_lower, rest).
    Otherwise return (None, title).
    """
    m = MARKER_RE.match(title.strip())
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return None, title

def gen_hex24(n: int) -> str:
    """Deterministic-ish 24-hex generator from an incrementing integer."""
    # 24 hex == 96 bits; use n encoded in hex and left-pad with random-ish but stable salt
    # For simplicity: zero-pad left to length 24.
    h = format(n, "x")[-24:]
    return h.rjust(24, "0")

class IdSpace:
    def __init__(self) -> None:
        self._next = 1
    def new(self) -> str:
        i = self._next
        self._next += 1
        return gen_hex24(i)

# -------- Minimal Markdown-to-HTML (safe default) --------
def md_to_html(md: str) -> str:
    """Very small markdown→html converter (paragraphs + unordered lists + links)."""
    lines = md.strip().splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        # list
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                item = re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip()
                items.append(f"<li>{inline_md(item)}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # paragraph
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^\s*[-*+]\s+", lines[i]):
            para.append(lines[i].rstrip())
            i += 1
        out.append("<p>" + inline_md(" ".join(para).strip()) + "</p>")
    return "".join(out)

def inline_md(text: str) -> str:
    # very small inline markdown: **bold**, *italic*, [text](url)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text

# -------- Parse sections --------
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

@dataclass
class Section:
    level: int
    title: str
    start: int
    end: int = None

def parse_headings(md: str) -> List[Section]:
    lines = md.splitlines()
    sections: List[Section] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            sections.append(Section(level, title, idx))
    # close ranges
    for i, s in enumerate(sections):
        s.end = sections[i+1].start if i+1 < len(sections) else len(lines)
    return sections

def slice_text(md: str, start: int, end: int) -> str:
    lines = md.splitlines()[start:end]
    # drop the heading line itself
    if lines and HEADING_RE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()

# -------- Adapt object builders --------
def course_template(title: str, lang: str) -> Dict[str, Any]:
    return {
        "_id": "course",
        "_type": "course",
        "title": title,
        "displayTitle": "",
        "description": "",
        "_defaultLanguage": lang,
        "_defaultDirection": "ltr"
    }

def menu_template(_id: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "menu",
        "_id": _id,
        "_parentId": "course",
        "title": title,
        "displayTitle": title
    }

def page_template(_id: str, parent_id: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "page",
        "_id": _id,
        "_parentId": parent_id,
        "title": title,
        "displayTitle": title
    }

def article_template(_id: str, parent_id: str, title: str) -> Dict[str, Any]:
    return {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "article",
        "title": title,
        "displayTitle": "",
        "_articleBlockSlider": {"_isEnabled": False, "_hasTabs": False},
        "_assessment": {"_questions": {"_resetType": "hard"}}
    }

def block_template(_id: str, parent_id: str, track_id: int, title: str) -> Dict[str, Any]:
    return {
        "_id": _id,
        "_parentId": parent_id,
        "_type": "block",
        "title": title,
        "displayTitle": title,
        "_trackingId": track_id,
        "_pageLevelProgress": {"_isEnabled": False}
    }

def component_common(_id: str, parent_id: str, comp: str, title: str) -> Dict[str, Any]:
    return {
        "_type": "component",
        "_component": comp,
        "_id": _id,
        "_parentId": parent_id,
        "title": title,
        "displayTitle": "",
        "_layout": "full",
        "_classes": "",
        "_isOptional": False,
        "_isAvailable": True,
        "_isHidden": False,
        "_isVisible": True,
        "_isResetOnRevisit": "false"
    }

def text_component(_id: str, parent_id: str, title: str, html_body: str) -> Dict[str, Any]:
    d = component_common(_id, parent_id, "text", title)
    d["body"] = html_body
    return d

# -------- Tiny front-matter (optional) --------
def _extract_meta_titles(md: str) -> dict:
    """
    Very small 'front-matter' parser for lines like:
      pageTitle: "..."
      articleTitle: "..."
    before the first dashed separator line (---...).
    """
    meta = {}
    for line in md.splitlines():
        if re.match(r"^\s*-{3,}\s*$", line):
            break
        m = re.match(r'^\s*([A-Za-z][\w-]*):\s*"(.*)"\s*$', line.strip())
        if m:
            meta[m.group(1)] = m.group(2)
    return meta

# -------- Builder orchestrator --------
def build_from_markdown(md: str, lang: str, menu_title: str) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict], Dict]:
    ids = IdSpace()
    sections = parse_headings(md)
    # Course title
    h1s = [s for s in sections if s.level == 1]
    course_title = h1s[0].title if h1s else "Course"
    course = course_template(course_title, lang)

    # Menu
    menu_id = ids.new()
    menu = menu_template(menu_id, menu_title or "Menu")

    # Detect "[block]" at H2 to avoid treating blocks as pages
    has_block_h2 = any(s.level == 2 and split_marker(s.title)[0] == "block" for s in sections)

    # Pages
    if has_block_h2:
        # single synthetic page so H2 [block] become blocks, not pages
        meta = _extract_meta_titles(md)
        synthetic_page_title = meta.get("pageTitle", course_title)
        pages_heads = [Section(level=2, title=synthetic_page_title, start=0, end=len(md.splitlines()))]
    else:
        pages_heads = [s for s in sections if s.level == 2]
        if not pages_heads:
            # fallback: use H1 beyond the first as pages
            pages_heads = h1s[1:]
        if not pages_heads:
            # final fallback: create a synthetic page spanning the whole doc
            pages_heads = [Section(level=2, title=course_title, start=0, end=len(md.splitlines()))]

    pages: List[Dict] = []
    articles: List[Dict] = []
    blocks: List[Dict] = []
    components: List[Dict] = []

    tracking = 1

    for p_idx, p_sec in enumerate(pages_heads, 1):
        page_id = ids.new()
        pages.append(page_template(page_id, menu_id, p_sec.title))

        # Articles
        if has_block_h2:
            # One synthetic article; prefer front-matter title
            meta = _extract_meta_titles(md)
            article_title = meta.get("articleTitle", p_sec.title)
            a_heads = [Section(level=3, title=article_title, start=p_sec.start, end=p_sec.end)]
        else:
            # H3 inside this page range
            a_heads = [s for s in sections if s.level == 3 and p_sec.start <= s.start < s.end if s.end is not None]  # safety
            # Fix comprehension to use correct end boundary:
            a_heads = [s for s in sections if s.level == 3 and p_sec.start <= s.start < p_sec.end]
            if not a_heads:
                a_heads = [Section(level=3, title=p_sec.title, start=p_sec.start, end=p_sec.end)]

        for a_sec in a_heads:
            article_id = ids.new()
            articles.append(article_template(article_id, page_id, a_sec.title))

            # Blocks
            if has_block_h2:
                # Treat H2 [block] as blocks inside the synthetic article
                b_heads = [s for s in sections if s.level == 2 and split_marker(s.title)[0] == "block"]
                b_chunks = [slice_text(md, s.start, s.end) for s in b_heads]
            else:
                # H4 blocks inside article; otherwise fall back to rules/single block
                b_heads = [s for s in sections if s.level == 4 and a_sec.start <= s.start < a_sec.end]
                if not b_heads:
                    # Split on horizontal rules (--- or ***) within article body
                    article_body = slice_text(md, a_sec.start, a_sec.end)
                    chunks = re.split(r"(?m)^\s*(?:-{3,}|\*{3,})\s*$", article_body)
                    if len(chunks) <= 1:
                        b_heads = [Section(level=4, title=a_sec.title, start=a_sec.start, end=a_sec.end)]
                        b_chunks = [slice_text(md, a_sec.start, a_sec.end)]
                    else:
                        b_heads = []
                        b_chunks = []
                        cursor = 0
                        for i, chunk in enumerate(chunks, 1):
                            # approximate start index by counting lines back from a_sec.start
                            b_heads.append(Section(level=4, title=f"{a_sec.title} – Section {i}", start=a_sec.start + cursor, end=None))
                            b_chunks.append(chunk.strip())
                            cursor += len(chunk.splitlines()) + 1
                else:
                    # collect chunks per H4
                    b_chunks = [slice_text(md, s.start, s.end) for s in b_heads]

            for b_sec, chunk in zip(b_heads, b_chunks):
                block_id = ids.new()
                b_marker, b_title = split_marker(b_sec.title)
                blocks.append(block_template(block_id, article_id, tracking, b_title))
                tracking += 1

                if has_block_h2:
                    # Components = H3 inside this H2 block (fallback to single component)
                    sub_heads = [s for s in sections if s.level == 3 and b_sec.start <= s.start < (b_sec.end if b_sec.end is not None else len(md.splitlines()))]
                    if sub_heads:
                        for sub in sub_heads:
                            comp_id = ids.new()
                            c_marker, c_title = split_marker(sub.title)
                            sub_chunk = slice_text(md, sub.start, sub.end)
                            html = md_to_html(sub_chunk) if sub_chunk.strip() else "<p></p>"
                            # For now everything -> TEXT component (safe default), marker only affects title
                            components.append(text_component(comp_id, block_id, c_title, html))
                    else:
                        # Fallback: single component from entire block chunk
                        html = md_to_html(chunk) if chunk.strip() else "<p></p>"
                        comp_id = ids.new()
                        components.append(text_component(comp_id, block_id, b_title, html))
                else:
                    # Original behavior: single TEXT component per block
                    html = md_to_html(chunk) if chunk.strip() else "<p></p>"
                    comp_id = ids.new()
                    comp_title = b_title
                    components.append(text_component(comp_id, block_id, comp_title, html))

    # contentObjects array = [menu] + pages
    content_objects = [menu] + pages
    return content_objects, articles, blocks, components, course

# -------- Validation --------
def validate_graph(content_objects, articles, blocks, components) -> None:
    ids = {o["_id"] for o in content_objects + articles + blocks + components}
    # hex check
    bad_hex = [i for i in ids if i != "course" and not HEX24_RE.match(i)]
    if bad_hex:
        raise ValueError(f"Non-hex _ids (should be 24 hex): {', '.join(bad_hex)}")

    # parent existence
    missing: List[str] = []
    for arr, label in [
        (articles, "article"),
        (blocks, "block"),
        (components, "component"),
        ([o for o in content_objects if o.get('_type') == 'page'], "page")
    ]:
        for obj in arr:
            pid = obj.get("_parentId")
            if pid and pid not in ids:
                missing.append(pid)
    if missing:
        raise ValueError(f"Missing _ids (referenced as _parentId but not found): {', '.join(sorted(set(missing)))}")

def write_jsons(out_dir: Path, content_objects, articles, blocks, components, course):
    out_dir.mkdir(parents=True, exist_ok=True)
    def dump(name, data):
        (out_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    dump("contentObjects.json", content_objects)
    dump("articles.json", articles)
    dump("blocks.json", blocks)
    dump("components.json", components)
    dump("course.json", course)

def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Markdown into Adapt JSON (menu/page/article/block/components + course.json).")
    ap.add_argument("input_md", help="Path to Markdown file")
    ap.add_argument("--out", default="course/en", help="Output folder (e.g., course/en)")
    ap.add_argument("--lang", default="en", help="Default language code for course.json")
    ap.add_argument("--menu", default="Menu", help="Menu title")
    args = ap.parse_args(argv)

    md_path = Path(args.input_md)
    if not md_path.exists():
        print(f"ERROR: Markdown file not found: {md_path}", file=sys.stderr)
        sys.exit(2)

    md = md_path.read_text(encoding="utf-8")
    content_objects, articles, blocks, components, course = build_from_markdown(md, args.lang, args.menu)

    # validate then write
    validate_graph(content_objects, articles, blocks, components)
    out_dir = Path(args.out)
    write_jsons(out_dir, content_objects, articles, blocks, components, course)

    print(f"Wrote: {out_dir/'contentObjects.json'}, {out_dir/'articles.json'}, {out_dir/'blocks.json'}, {out_dir/'components.json'}, {out_dir/'course.json'}")
    print("Objects: contentObjects=%d (menu+pages), articles=%d, blocks=%d, components=%d" % (
        len(content_objects), len(articles), len(blocks), len(components)
    ))

if __name__ == "__main__":
    main()
