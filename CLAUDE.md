# Samawi Bot — working context

Autonomous Instagram + Threads content engine for **@samawi.indonesia** (Samawi, the ad-free
prayer and Quran app). It plans, writes, verifies, renders and publishes every day with **no human
review**. The full specification is `~/Downloads/samawi-social-automation.md` (copied decisions are
summarised here); the app repo `toshikij/samawi` wins on brand, tone and which features exist.

## The one rule

**The verification layer is the reviewer.** One viral wrong hadith costs more trust than 100 good
posts earn. A draft that fails any check is never published; the day falls back to the evergreen
bank. Never loosen a check, threshold, blocked topic or render guard to get content through.

## How it works

| Stage | Code | Notes |
|---|---|---|
| Sources | `sources/` | Snapshot of the app's assets with SHA-256 manifest (`snapshot/`); Quran (Tanzil + Kemenag), hadith (HadeethEnc, admitted only if sahih/hasan and Bukhari/Muslim, or Sunan with no fawazahmed0 grader marking it weak), duas (`needsReview: false` only), features (`samawi_features.dart`, shipped only), prayer engine (matches the app's Dart engine to the minute — `tests/fixtures/app_engine_golden.json`), Hijri (Umm al-Qura, isbat wording) |
| Facts | `verify/facts.py` | Every number is a stored computation keyed like `compare:Sabang:Merauke:2026-11-26:Subuh`. The writer only sees these |
| Writer / judge | `brain/` | Claude Opus 5.5, structured output, server-side refusal fallback. The LLM never types scripture — it outputs references |
| Checks | `checks/` | Deterministic: references resolve, numbers backed, bridge shipped, banned/blocked patterns (normalised against leetspeak, zero-width, spacing), limits, too-easy list, 60-day topic dedupe |
| Gate | `verify/gate.py` | Judge verdicts: every claim supported, no contested framing, hook ≥ 8 on curiosity/specificity/payoff honesty/thumb-stop, novelty ≥ 60%, accessibility ≥ 8. Malformed judge output fails closed |
| Render | `render/` | Jinja + headless Chromium. Fonts must load (no fallback), no overflow, scripture text equals source after NFC, WCAG AA measured on real pixels. Long ayat paginate, never shrink. Reels: slides + ffmpeg, silent |
| Publish | `publisher/`, `jobs/publish.py` | Kill switch → payload → idempotency lease → quota. Threads chains resume, never duplicate |
| Learn | `learn/`, `jobs/ops.py` | Thompson sampling over pillar/hook/format/slot/bridge only (`ALLOWED_DIMENSIONS`); cannot touch §6 |

Jobs run as ECS Fargate tasks from EventBridge Scheduler (`infra/terraform/schedule.json`, WIB).
`produce` (02:00) also creates one-off `at()` schedules for each prayer-anchored slot; the hourly
`publish-due` sweep is the safety net and logs the heartbeat the dead-man alarm watches.

## Switches (Terraform variables, not secrets)

`publish_live` (dry run until true) · `launch_mode` prelaunch|live · `play_store_url` ·
`translation_mode` kemenag|makna · `kill_switch` · `schedules_enabled`. The dashboard's
**Pause all publishing** sets the database kill switch without a deploy.

## Commands

```bash
uv sync && uv run playwright install chromium
uv run pytest --cov --cov-branch          # ≥ 95% gate; safety modules 100% (see CI)
uv run mypy && uv run ruff check src tests
uv run python scripts/render_sample.py maghrib   # render the sample carousel locally
scripts/deploy.sh                          # build ARM64 image, push, roll dashboard
scripts/run-task.sh produce --day 2026-11-27     # run any job in AWS now
scripts/dashboard-tunnel.sh                # dashboard at http://localhost:3200/samawi
```

## Rules of the codebase

- No secrets in the repo. Values live in `samawi-bot/app` (Secrets Manager); Terraform never sees them.
- Posts show no source lists or translation credits, except the HadeethEnc caption credit its terms require (`SOURCES.md`).
- Bridges only to `shipped` features with a public name (`sources/features_reader.py:DISPLAY_NAMES`).
- No trial, price, guilt, "ketik AAMIIN", or competitor names, ever. Prelaunch posts never link to an install.
- Brand values come from the app (`sky_palette.dart`, fonts, logo) via the snapshot — never hand-copied.
- A new check or failure path gets a test; a new render template gets golden snapshots.
