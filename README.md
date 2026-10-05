# MiniSeries

Scripts and tooling for **8090 Back-to-Back**, a mini series voiced by two HeyGen avatars, **Nia** and **Effie**.

## HeyGen dialogue workflow

```
scripts/ep01.md  ──parse_script.py──▶  33 dialogue lines  ──make_episode.py──▶  HeyGen videos (one per section)
                                         (speaker, text)        cast.json: avatar + voice per character
```

1. Paste the episode script into `scripts/epNN.md` (same format as `ep01.md`: `## SECTION`, `**SPEAKER (note)**`, then the line; `**[stage directions]**` are skipped).
2. Set your key: `export HEYGEN_API_KEY=...` (never commit it; `.env` is git-ignored).
3. Check who plays whom in `heygen/cast.json`. `python heygen/make_episode.py --list-avatars` prints your avatar looks and voices.
4. Preview for free: `python heygen/make_episode.py scripts/ep01.md --dry-run`
5. Render:
   - a short test: `python heygen/make_episode.py --lines 13-16`
   - one section: `python heygen/make_episode.py --section "TRUST"`
   - the whole episode, one video per section: `python heygen/make_episode.py`
   - add `--avatar-iv` for HeyGen's Avatar IV motion, `--aspect-ratio 9:16` for Shorts.

Videos land in `output/` (git-ignored) and in your HeyGen library; `output/manifest.json` records every video ID and share link.

Each spoken line becomes one scene with that character's avatar and voice, so the result is a cut-on-speaker dialogue. B-roll, two-shots and the title/end cards are left for the edit.

Cost seen on 2026-10-05: a 12-second 4-line test clip cost about $0.22 of API wallet balance (roughly $1 per minute of dialogue).

Note: HeyGen marks its v1/v2 endpoints as legacy with a sunset date of 2026-10-31. `make_episode.py` uses `POST /v2/video/generate`, so it will need moving to the v3 API before then.

## Credits

`heygen/vendor/lipnardo/heygen_client.py` (API client, retries, polling, download) is copied from
[AgriciDaniel/lipnardo](https://github.com/AgriciDaniel/lipnardo) under the MIT License. See
[heygen/vendor/lipnardo/NOTICE.md](heygen/vendor/lipnardo/NOTICE.md).
