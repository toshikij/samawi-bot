# Meta setup: Instagram and Threads for @samawi.indonesia

About 30 minutes. Nothing publishes until `publish_live = true`, so this is safe to do first.
Steps marked **(you)** need your phone, your password or a token, which Claude cannot handle.

## 1. Make @samawi.indonesia a Business account · phone, 2 min (you)

- [ ] Instagram app, signed in as @samawi.indonesia: **Profile → ☰ → Account type and tools →
      Switch to professional account → Business**. Category: *App page* or *Education*.
      API stories need a Business account, not Creator.
- [ ] Threads follows the Instagram account. Check https://www.threads.com/@samawi.indonesia loads.

## 2. Facebook Page, linked to Instagram · 5 min (you)

- [ ] Create a Page named **Samawi**: https://www.facebook.com/pages/create (a Page, not a profile).
- [ ] Link it: Instagram app as @samawi.indonesia → **Edit profile → Page → Connect existing Page → Samawi**.

## 3. Developer app · done 25 Nov 2026

App **Samawi Bot Publisher**, ID **`3524868094341159`**, use cases *Manage messaging & content on
Instagram* and *Access the Threads API*, development mode, no business portfolio (same as Seduh).

- [x] Threads permissions: `threads_basic`, `threads_content_publish`, `threads_delete`, `threads_manage_insights`, `threads_manage_replies`
- [x] Instagram permissions: `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_contents`, `instagram_manage_insights`, `instagram_manage_messages`, `pages_show_list`, `pages_read_engagement`, `business_management`
- [x] Threads tester invite sent to `samawi.indonesia` (status *Pending*)
- [ ] **(you)** accept it while signed in to Threads **as @samawi.indonesia** (Chrome is signed in as
      @agenasports.id): https://www.threads.com/settings/account → *Website permissions → Invites*

## 4. Tokens · 10 min (you)

**Instagram:** https://developers.facebook.com/tools/explorer/ → app *Samawi Bot Publisher* →
*User Token* → add the Instagram permissions above → **Generate Access Token** → *Opt in to current
Pages only* → tick **Samawi** and **@samawi.indonesia**. Copy the token.

**Threads:** app dashboard → *Use cases → Access the Threads API → Settings → User Token Generator*
→ **Generate Access Token** next to `samawi.indonesia` (it appears once the tester invite is accepted).

## 5. Store them · 2 min (you)

```bash
cd ~/samawi-bot && python3 scripts/store_meta_tokens.py --threads-long-lived
```

It asks for the app ID (`3524868094341159`), the app secret (App settings → Basic → Show) and both tokens (secrets are not echoed), swaps the Instagram
token for a 60-day one, checks both belong to @samawi.indonesia, and writes everything into the
`samawi-bot/app` secret without printing a value. Then tell Claude: it runs the `tokens` job to
confirm, posts the Phase 0 test image, and the `tokens` job keeps both refreshed from then on
(email 7 days before either would expire).
