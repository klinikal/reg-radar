#!/usr/bin/env python3
"""Checks wiki/ for coverage and consistency against the rest of the repo.

Stdlib only. Usage:
    python3 scripts/check-wiki.py

wiki/ holds only README.md (the index) and SKILL.md (the reg-radar-wiki
skill) at its root; every reference page lives under wiki/content/, so a
repo browser isn't confronted with dozens of files at the top of wiki/.

Checks:
  1. every file under regradar/, collector/, mcpserver/, webui/ or scripts/
     (excluding __pycache__/, *.pyc and requirements.txt) is named in some
     wiki page's `sources:` frontmatter list, either directly or via a
     directory entry (e.g. `collector/adapters/`) that contains it;
  2. every `sources:` path named by a wiki page resolves to a real file or
     directory in the repo;
  3. every markdown link inside wiki/README.md, wiki/SKILL.md and
     wiki/content/*.md resolves to a real file;
  4. every content page (all of wiki/content/*.md except _template.md) has
     every required heading and a non-empty `last-verified` frontmatter
     field;
  5. every content page's `type:` is one of the allowed values;
  6. no two content pages share the same `title:`;
  7. every content page is linked at least once from README.md, and every
     wiki-relative link in README.md points at a real page;
  8. no content page contains an em dash (style rule: none, ever).

Exits 1 with one line per problem, 0 on a clean pass.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = REPO_ROOT / "wiki"
CONTENT_DIR = WIKI_DIR / "content"
SCAN_DIRS = ["regradar", "collector", "mcpserver", "webui", "scripts"]
SKIP_NAMES = {"requirements.txt", "__init__.py"}
REQUIRED_HEADINGS = [
    "## Purpose",
    "## How it works",
    "## Inputs",
    "## Outputs and side effects",
    "## How to run or invoke",
    "## How to test",
    "## Boundaries",
    "## Related pages",
]
ALLOWED_TYPES = {
    "concept", "component", "cli", "config", "script", "job", "runbook",
}
SKIP_PAGES = {"_template.md"}
EM_DASH = "—"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def real_source_files() -> set[Path]:
    files = set()
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.name in SKIP_NAMES:
                continue
            files.add(path.resolve())
    return files


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + 4:]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def parse_sources(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    parts = [p.strip().strip('"').strip("'") for p in raw.split(",")]
    return [p for p in parts if p]


def resolve_source_path(src: str) -> Path:
    return (REPO_ROOT / src).resolve()


def check_links(page: Path, text: str) -> list[str]:
    problems = []
    for m in LINK_RE.finditer(text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        resolved = (page.parent / target).resolve()
        if not resolved.exists():
            problems.append(f"{page.relative_to(REPO_ROOT)}: broken link -> {target}")
    return problems


def main() -> int:
    problems: list[str] = []

    if not CONTENT_DIR.is_dir():
        print(f"FAIL: {CONTENT_DIR} not found")
        return 1

    pages = sorted(CONTENT_DIR.glob("*.md"))
    content_pages = [p for p in pages if p.name not in SKIP_PAGES]

    covered: set[Path] = set()
    titles: dict[str, list[str]] = {}
    for page in content_pages:
        text = page.read_text()
        fields, body = parse_frontmatter(text)
        rel = page.relative_to(REPO_ROOT)

        sources = parse_sources(fields.get("sources", ""))
        if not sources:
            problems.append(f"{rel}: no sources: listed")
        for src in sources:
            resolved = resolve_source_path(src)
            if not resolved.exists():
                problems.append(f"{rel}: sources path does not exist: {src}")
                continue
            covered.add(resolved)
            if resolved.is_dir():
                for sub in resolved.rglob("*"):
                    if sub.is_file() and "__pycache__" not in sub.parts and sub.suffix != ".pyc" and sub.name not in SKIP_NAMES:
                        covered.add(sub.resolve())

        if not fields.get("last-verified"):
            problems.append(f"{rel}: missing last-verified")

        page_type = fields.get("type")
        if page_type not in ALLOWED_TYPES:
            problems.append(f"{rel}: type '{page_type}' is not one of {sorted(ALLOWED_TYPES)}")

        title = fields.get("title")
        if title:
            titles.setdefault(title, []).append(page.name)

        for heading in REQUIRED_HEADINGS:
            if heading not in body:
                problems.append(f"{rel}: missing heading '{heading}'")

        if EM_DASH in text:
            problems.append(f"{rel}: contains an em dash")

        problems.extend(check_links(page, text))

    for title, names in titles.items():
        if len(names) > 1:
            problems.append(f"duplicate title '{title}': {', '.join(sorted(names))}")

    for f in sorted(real_source_files()):
        if f not in covered:
            problems.append(f"{f.relative_to(REPO_ROOT)}: no wiki page names this file in sources:")

    readme = WIKI_DIR / "README.md"
    if readme.is_file():
        readme_text = readme.read_text()
        problems.extend(check_links(readme, readme_text))
        linked = set(m.group(1).split("#", 1)[0].strip() for m in LINK_RE.finditer(readme_text))
        for page in content_pages:
            if f"content/{page.name}" not in linked:
                problems.append(f"{readme.relative_to(REPO_ROOT)}: does not link to {page.name}")
    else:
        problems.append("wiki/README.md not found")

    skill = WIKI_DIR / "SKILL.md"
    if skill.is_file():
        problems.extend(check_links(skill, skill.read_text()))

    if problems:
        for p in sorted(set(problems)):
            print(f"FAIL: {p}")
        return 1

    print(f"OK: {len(content_pages)} wiki pages, all sources exist, all links resolve, all required headings present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
