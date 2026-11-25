# samawi-bot

Autonomous Instagram + Threads content engine for **@samawi.indonesia**: it plans, writes,
verifies, renders and publishes Islamic educational content every day around the prayer times,
with no human review. "Ilmu Islam yang benar, dan bikin takjub."

- **Benar:** the LLM never types scripture. Quran text (Tanzil), translation (Kemenag), hadith
  (HadeethEnc, sahih/hasan only, cross-checked against fawazahmed0 graders) and duas come from
  sources; every number comes from a stored computation; a separate judge must mark every claim
  supported. A failed draft is never published — the day uses the evergreen bank instead.
- **Bikin penasaran:** hooks are scored on curiosity, specificity, payoff honesty and thumb-stop,
  and must clear 8/10 on all four.
- **Samawi brand:** sky palettes parsed from the app, Plus Jakarta Sans + Amiri Quran, WCAG AA
  measured on rendered pixels.

Start with [CLAUDE.md](CLAUDE.md) (how it works), then [docs/GO-LIVE.md](docs/GO-LIVE.md) (what is
left to switch it on), [docs/META-SETUP.md](docs/META-SETUP.md), [SOURCES.md](SOURCES.md) and
[QA.md](QA.md).

```bash
uv sync && uv run playwright install chromium
uv run pytest --cov --cov-branch     # unit, property, contract, fault-injection, golden, e2e
uv run python scripts/render_sample.py maghrib
```

Deployed on AWS (ap-southeast-3) with Terraform in `infra/terraform`: ECS Fargate jobs on
EventBridge Scheduler, RDS Postgres, private S3 media with presigned URLs, SES alerts, and an
owner-only dashboard reached through `scripts/dashboard-tunnel.sh`.
