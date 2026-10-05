#!/usr/bin/env python3
"""Turn a MiniSeries script into HeyGen dialogue videos with the Nia & Effie avatars.

Each spoken line becomes one HeyGen scene played by that character's avatar and
voice (heygen/cast.json). By default one video is rendered per script section,
so a bad take only costs one section to redo.

The API key is read from HEYGEN_API_KEY (never commit it).

Examples:
    python heygen/make_episode.py --list-avatars             # find avatar / voice IDs
    python heygen/make_episode.py --dry-run                  # show payloads, spend nothing
    python heygen/make_episode.py --lines 13-16              # short test clip
    python heygen/make_episode.py --section "MONEY"          # one section
    python heygen/make_episode.py                            # whole episode, one video per section
    python heygen/make_episode.py --single-video             # whole episode as one video
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor" / "lipnardo"))
sys.path.insert(0, str(HERE))

from heygen_client import (  # noqa: E402  (vendored, MIT, see vendor/lipnardo/NOTICE.md)
    HeyGenAPIError,
    api_request,
    download_file,
    poll_video_status,
    sanitize_id,
)
from parse_script import Line, parse  # noqa: E402

DIMENSIONS = {"16:9": {"width": 1920, "height": 1080}, "9:16": {"width": 1080, "height": 1920}}
MAX_SCENES_PER_VIDEO = 50


def api_key() -> str:
    key = os.environ.get("HEYGEN_API_KEY", "")
    if not key:
        # Some sandboxes inject the key at a proxy instead; the header is then overwritten.
        print("warning: HEYGEN_API_KEY is not set; requests will only work behind a key-injecting proxy",
              file=sys.stderr)
    return key


def build_scene(line: Line, cast: dict, avatar_iv: bool) -> dict:
    who = cast.get(line.speaker)
    if not who:
        sys.exit(f"No cast entry for speaker {line.speaker!r} in cast.json")
    character = {"type": "talking_photo", "talking_photo_id": who["talking_photo_id"]}
    if avatar_iv:
        character["use_avatar_iv_model"] = True
    return {
        "character": character,
        "voice": {"type": "text", "input_text": line.text, "voice_id": who["voice_id"]},
        "background": {"type": "color", "value": who.get("background", "#000000")},
    }


def group_lines(lines: list[Line], single_video: bool) -> list[tuple[str, list[Line]]]:
    if single_video:
        groups = [("episode", lines)]
    else:
        groups = []
        for line in lines:
            if not groups or groups[-1][0] != line.section:
                groups.append((line.section, []))
            groups[-1][1].append(line)
    out = []
    for name, chunk in groups:  # HeyGen caps scenes per video
        for i in range(0, len(chunk), MAX_SCENES_PER_VIDEO):
            part = f"{name} (part {i // MAX_SCENES_PER_VIDEO + 1})" if len(chunk) > MAX_SCENES_PER_VIDEO else name
            out.append((part, chunk[i:i + MAX_SCENES_PER_VIDEO]))
    return out


def slug(text: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in text.split("—")[0])
    return "-".join(p for p in keep.split("-") if p)[:40] or "video"


def list_avatars(key: str) -> None:
    groups = api_request("GET", "/v2/avatar_group.list", key)["data"]["avatar_group_list"]
    for g in groups:
        print(f"\n{g['name']}  (group {g['id']}, default voice {g.get('default_voice_id')})")
        looks = api_request("GET", f"/v2/avatar_group/{g['id']}/avatars", key)["data"]["avatar_list"]
        for a in looks:
            look_id = a.get("id") or a.get("avatar_id") or a.get("talking_photo_id")
            print(f"  look  {look_id}  {a.get('name') or a.get('avatar_name', '')}")
    voices = api_request("GET", "/v2/voices", key)["data"]["voices"]
    in_use = {g.get("default_voice_id") for g in groups}
    print("\nVoices (your groups' defaults first; full list: GET /v2/voices):")
    for v in sorted(voices, key=lambda v: v["voice_id"] not in in_use)[:15]:
        print(f"  voice {v['voice_id']}  {v['name']}  ({v.get('language')}, {v.get('gender')})")


def parse_range(spec: str) -> tuple[int, int]:
    a, _, b = spec.partition("-")
    return int(a), int(b or a)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("script", nargs="?", default="scripts/ep01.md")
    p.add_argument("--cast", default=str(HERE / "cast.json"))
    p.add_argument("--section", help="Only sections whose name contains this text")
    p.add_argument("--lines", help="Only these line numbers, e.g. 13-16 (see parse_script.py)")
    p.add_argument("--single-video", action="store_true", help="One video instead of one per section")
    p.add_argument("--aspect-ratio", default="16:9", choices=DIMENSIONS)
    p.add_argument("--avatar-iv", action="store_true", help="Use Avatar IV motion (better, costs more)")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--no-wait", action="store_true", help="Submit and exit; don't poll or download")
    p.add_argument("--dry-run", action="store_true", help="Print payloads, call nothing")
    p.add_argument("--list-avatars", action="store_true")
    args = p.parse_args()

    key = "" if args.dry_run else api_key()
    if args.list_avatars:
        list_avatars(key)
        return

    cast = {k: v for k, v in json.loads(Path(args.cast).read_text()).items() if not k.startswith("_")}
    lines = parse(Path(args.script).read_text(encoding="utf-8"), speakers=set(cast))
    if args.section:
        lines = [l for l in lines if args.section.lower() in l.section.lower()]
    if args.lines:
        lo, hi = parse_range(args.lines)
        lines = [l for l in lines if lo <= l.index <= hi]
    if not lines:
        sys.exit("No dialogue lines matched.")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []

    for name, chunk in group_lines(lines, args.single_video):
        body = {
            "title": f"{Path(args.script).stem} · {name}",
            "video_inputs": [build_scene(l, cast, args.avatar_iv) for l in chunk],
            "dimension": DIMENSIONS[args.aspect_ratio],
        }
        label = f"{name}: lines {chunk[0].index}-{chunk[-1].index}, {sum(len(l.text) for l in chunk)} chars"
        if args.dry_run:
            print(f"# {label}\n{json.dumps(body, indent=2, ensure_ascii=False)}")
            continue

        print(f"Submitting {label}", file=sys.stderr)
        try:
            resp = api_request("POST", "/v2/video/generate", key, body=body)
        except HeyGenAPIError as e:
            sys.exit(f"HeyGen rejected {name}: {e}")
        video_id = resp.get("data", resp).get("video_id")
        entry = {"section": name, "lines": [chunk[0].index, chunk[-1].index], "video_id": video_id,
                 "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        manifest.append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        if args.no_wait:
            print(json.dumps(entry))
            continue

        result = poll_video_status(video_id, key)
        entry["video_url"] = result.get("video_url")
        entry["duration"] = result.get("duration")
        entry["share_url"] = f"https://app.heygen.com/share/{sanitize_id(video_id)}"
        file = out_dir / f"{Path(args.script).stem}-{slug(name)}-{sanitize_id(video_id)}.mp4"
        try:
            entry["path"] = download_file(entry["video_url"], str(file))
        except Exception as e:  # the video is still in the HeyGen library; keep going
            print(f"warning: download failed ({e.__class__.__name__}); open {entry['share_url']}",
                  file=sys.stderr)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        print(json.dumps(entry))


if __name__ == "__main__":
    main()
