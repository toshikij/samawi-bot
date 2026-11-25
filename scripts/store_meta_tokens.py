#!/usr/bin/env python3
"""Store the Instagram and Threads credentials in the samawi-bot/app secret (docs/META-SETUP.md, step 6).

Run it yourself:  python3 scripts/store_meta_tokens.py

It asks for the app ID and, without echoing them, the app secrets and the two short-lived tokens.
It exchanges each token for a 60-day one, looks up the Page, Instagram and Threads account ids,
checks they belong to @samawi.indonesia, and writes them into the secret. No secret value is ever
printed, and nothing is written to disk except a private temp file that is deleted at once.
"""
import getpass
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REGION, SECRET_ID, HANDLE = "ap-southeast-3", "samawi-bot/app", "samawi.indonesia"
GRAPH = "https://graph.facebook.com/v23.0"
THREADS = "https://graph.threads.net"


def get(base, path, params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "samawi-bot-setup"}), timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"_error": f"HTTP {e.code} {msg}".strip()}


def stop(msg):
    sys.exit(f"\n✗ {msg}\nNothing was written to the secret.")


def ask(label, hidden=True):
    v = (getpass.getpass if hidden else input)(f"{label}: ").strip()
    if not v or " " in v:
        stop(f"{label} looks empty or malformed.")
    return v


print("Paste each value and press Enter. Secrets are not shown as you type.\n")
app_id = input("Meta app ID [3524868094341159, Samawi Bot Publisher]: ").strip() or "3524868094341159"
app_secret = ask("Meta app secret (App settings → Basic → Show)")
ig_short = ask("Instagram token from Graph API Explorer (step 5a)")
LONG_LIVED = "--threads-long-lived" in sys.argv
# The dashboard's User Token Generator already issues a 60-day token; only a short-lived one needs the secret.
threads_secret = None if LONG_LIVED else ask("Threads app secret (Use cases → Threads API → Settings)")
threads_token = ask("Threads token (step 5b)")

# Instagram: short-lived user token → 60-day user token, then the Page and its Instagram account.
ll = get(GRAPH, "/oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret, "fb_exchange_token": ig_short})
if "_error" in ll or "access_token" not in ll:
    stop(f"Instagram token exchange failed: {ll.get('_error', 'no token returned')}. Check the app ID/secret, and generate a fresh Explorer token (it lasts one hour).")
ig_token = ll["access_token"]
pages = get(GRAPH, "/me/accounts", {"fields": "id,name,instagram_business_account{id,username}", "access_token": ig_token})
if "_error" in pages:
    stop(f"Could not list your Pages: {pages['_error']}")
match = [p for p in pages.get("data", []) if (p.get("instagram_business_account") or {}).get("username") == HANDLE]
if not match:
    names = ", ".join(p.get("name", "?") for p in pages.get("data", [])) or "none"
    stop(f"No Page linked to @{HANDLE} is visible to this token (Pages seen: {names}). Add business_management to the Explorer permissions and select the Samawi Page when Facebook asks which Pages to share (step 5a).")
page = match[0]

# Threads: short-lived → 60-day token, then the user id.
if LONG_LIVED:
    th = {"access_token": threads_token}
else:
    th = get(THREADS, "/access_token", {"grant_type": "th_exchange_token", "client_secret": threads_secret, "access_token": threads_token})
    if "_error" in th or "access_token" not in th:
        stop(f"Threads token exchange failed: {th.get('_error', 'no token returned')}. A token from the dashboard's User Token Generator is already the 60-day kind: rerun with --threads-long-lived.")
th_token = th["access_token"]
me = get(THREADS + "/v1.0", "/me", {"fields": "id,username", "access_token": th_token})
if "_error" in me:
    stop(f"Threads token does not work: {me['_error']}")
if me.get("username") != HANDLE:
    stop(f"The Threads token belongs to @{me.get('username')}, not @{HANDLE}. Generate it while signed in to Threads as @{HANDLE}.")

values = {
    "META_APP_ID": app_id,
    "META_APP_SECRET": app_secret,
    "IG_ACCESS_TOKEN": ig_token,
    "IG_USER_ID": page["instagram_business_account"]["id"],
    "FB_PAGE_ID": page["id"],
    "THREADS_ACCESS_TOKEN": th_token,
    "THREADS_USER_ID": me["id"],
}

raw = subprocess.check_output(["aws", "secretsmanager", "get-secret-value", "--region", REGION, "--secret-id", SECRET_ID, "--query", "SecretString", "--output", "text"])
secret = json.loads(raw)
already = [k for k in values if (secret.get(k) or "").strip()]
if already and input(f"\nThese are already set and will be replaced: {', '.join(already)}. Continue? [y/N] ").strip().lower() != "y":
    stop("Stopped at your request.")
secret.update(values)
fd, path = tempfile.mkstemp()
try:
    with os.fdopen(fd, "w") as f:
        json.dump(secret, f)
    subprocess.check_call(["aws", "secretsmanager", "put-secret-value", "--region", REGION, "--secret-id", SECRET_ID, "--secret-string", f"file://{path}"], stdout=subprocess.DEVNULL)
finally:
    os.remove(path)

days = int(ll.get("expires_in", 0)) // 86400
print(f"\n✓ Stored in {SECRET_ID}: {', '.join(values)}.")
print(f"  Page: {page.get('name')} · Instagram: @{page['instagram_business_account']['username']} · Threads: @{me['username']}")
print(f"  Instagram token lasts {days or 'about 60'} days; the tokens job refreshes both from now on.")
print("  Tell Claude it's done; nothing posts until publish_live = true.")
