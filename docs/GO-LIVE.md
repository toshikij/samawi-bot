# Go-live: what is done and what is left

## Done (25 Nov 2026)

- [x] Code for spec phases 1–7 (sources, render, brain, publish, learn, reels, launch mode) with
      the test gates in CI (`.github/workflows/ci.yml`).
- [x] AWS stack applied (`infra/terraform`, state key `samawi-bot/terraform.tfstate`): ECR `samawi-bot`,
      ECS cluster `samawi`, task `samawi-pipeline`, RDS `samawi-bot` (Postgres 16, private,
      encrypted), S3 `samawi-bot-assets-249746593249`, secrets `samawi-bot/app` and `samawi-bot/db`,
      7 schedules (**disabled**), alarms + SNS `samawi-bot-alerts`, dashboard service `samawi-dashboard`.
- [x] Database migrated; image pushed; `samawi-bot/app` holds the Claude API key and session secret.
- [x] Playwright MCP QA pass (`QA.md`).
- [x] Meta developer app **Samawi Bot Publisher** (ID `3524868094341159`) with every Instagram and
      Threads permission the bot uses; Threads tester invite sent to `samawi.indonesia`.
- [x] Sources refreshed in AWS: 637 HadeethEnc hadith cached, 557 admitted; snapshot and cache persisted to S3.
- [x] Real Claude runs: the writer → checks → judge loop produces publishable posts (first pass:
      "Ternyata Jayapura sudah Isya saat Banda Aceh belum Maghrib", ~$0.80 including retries).

## Yours, in order

1. **Confirm the SNS subscription** email from AWS ("samawi-bot-alerts"), so infrastructure alarms reach you.
2. **Meta setup** — `docs/META-SETUP.md` steps 1, 2, 4, 5: Business account, Facebook Page link,
   accept the Threads tester invite **as @samawi.indonesia**, generate the two tokens, run
   `python3 scripts/store_meta_tokens.py --threads-long-lived`.
3. **Optional — live app assets:** create a fine-grained GitHub token with read-only *Contents*
   access to `toshikij/samawi` only, and add it as `SAMAWI_REPO_TOKEN` in `samawi-bot/app`. Without
   it the bot uses the snapshot baked into the image (refresh it with each deploy).
4. **HadeethEnc:** approve sending the permission request drafted in `SOURCES.md` (or send it yourself).
5. **Skim the evergreen bank** once (dashboard → Evergreen) after it is generated.

## Then Claude switches it on

1. `tokens` job confirms both tokens; Phase 0 test image posts to Instagram and Threads.
2. `schedules_enabled = true`, dry run (`publish_live = false`) for a few days: check each morning's
   07:00 email and the dashboard queue.
3. `publish_live = true` → fully autonomous. **Pause all publishing** on the dashboard (or
   `kill_switch = true`) stops everything without a deploy.
4. When the Android app is public: `launch_mode = "live"`, `play_store_url = "…"` → the launch
   series is planned automatically and comment→DM turns on. Update the bio link by hand (the API
   cannot edit bios) and pin the first launch post's comment by hand (the API cannot pin comments).

## Notes

- Deleting: Threads posts can be deleted through the API. Whether Instagram media can be is verified
  with the Phase 0 test image (post it, then `DELETE /{media-id}`) and recorded here. Prevention is
  the design either way: nothing unverified is ever published.
- Reels publish on Tue/Thu/Sat as the day's carousel in motion (silent audio track, no music).
