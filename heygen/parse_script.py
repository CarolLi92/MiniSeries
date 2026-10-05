#!/usr/bin/env python3
"""Parse a MiniSeries screenplay (Markdown) into spoken dialogue lines.

Format it understands (see scripts/ep01.md):
    ## SECTION NAME — 0:00–0:25        -> starts a new section
    **[stage direction]**              -> skipped
    **EFFIE (V.O.)**                   -> speaker cue (parenthetical kept as `note`)
    The spoken text...                 -> one or more paragraphs after the cue

Run directly to preview what will be sent to HeyGen:
    python heygen/parse_script.py scripts/ep01.md
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass

CUE_RE = re.compile(r"^\*\*([A-Z][A-Z .'-]*?)\s*(?:\(([^)]*)\))?\*\*$")
SECTION_RE = re.compile(r"^##\s+(?:\\?-+)?\s*(?:\*\*)?(.+?)(?:\*\*)?\s*$")
STOP_SECTIONS = ("END CARD",)


@dataclass
class Line:
    index: int
    section: str
    speaker: str
    note: str  # e.g. "V.O.", "laughs"; empty when on camera
    text: str


def _clean(text: str) -> str:
    text = text.replace("\\", "")
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)  # drop *italic* / **bold** markers
    return re.sub(r"\s+", " ", text).strip()


def parse(markdown: str, speakers: set[str] | None = None) -> list[Line]:
    lines: list[Line] = []
    section = ""
    speaker = note = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal speaker, note, buf
        text = _clean(" ".join(buf))
        if speaker and text:
            lines.append(Line(len(lines) + 1, section, speaker, note, text))
        speaker, note, buf = "", "", []

    for raw in markdown.splitlines():
        s = raw.strip()
        if not s or s.startswith("<!--"):
            continue
        m = SECTION_RE.match(s)
        if m:
            flush()
            section = _clean(m.group(1))
            if section.upper().startswith(STOP_SECTIONS):
                break
            continue
        if s.startswith("#"):
            continue
        m = CUE_RE.match(s)
        if m and (speakers is None or m.group(1).strip() in speakers):
            flush()
            speaker, note = m.group(1).strip(), (m.group(2) or "").strip()
            continue
        if s.startswith(("**[", "**\\[", "[", "\\[")) or s.startswith("**"):
            # Stage direction or a bold title card: ends the current line.
            flush()
            continue
        if speaker:
            buf.append(s)
    flush()
    return lines


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "scripts/ep01.md"
    with open(path, encoding="utf-8") as f:
        for line in parse(f.read()):
            print(json.dumps(asdict(line), ensure_ascii=False))


if __name__ == "__main__":
    main()
