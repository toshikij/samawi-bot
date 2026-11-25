"""Read-only admin dashboard (spec §8): status, queue, posts and metrics, evergreen bank, kill switch.

Visible to the owner only: sign-in is a one-time link emailed to ALERT_EMAIL (no address is ever
typed in), and the service accepts no inbound traffic in AWS; it is reached through an SSM tunnel.
The kill switch is the only thing it can change.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import func, select

from ..alerts import safe_send
from ..app import App
from ..db import session_scope
from ..db.models import ContentPlan, DraftRow, EvergreenPost, JobRun, Metric, Post, Token
from ..kill_switch import is_on, set_switch
from ..publisher.slots import WIB
from ..timeutil import utc

SESSION = "samawi_session"
LINK_TTL = 15 * 60
SESSION_TTL = 7 * 24 * 3600


def create_admin(app: App) -> FastAPI:
    st = app.settings
    base = st.admin_base_path.rstrip("/")
    signer = URLSafeTimedSerializer(st.session_secret, salt="samawi-admin")
    env = Environment(loader=PackageLoader("samawi_bot.admin", "templates"), autoescape=select_autoescape())
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def page(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(base=base, live=st.publish_live, mode=st.launch_mode.value, **ctx))

    def authed(request: Request) -> str:
        token = request.cookies.get(SESSION, "")
        try:
            who = signer.loads(token, max_age=SESSION_TTL)
        except (BadSignature, SignatureExpired):
            raise HTTPException(status_code=303, headers={"Location": f"{base}/login"}) from None
        if who != st.alert_email:
            raise HTTPException(status_code=303, headers={"Location": f"{base}/login"})
        return str(who)

    @web.get(f"{base}/login", response_class=HTMLResponse)
    def login(request: Request, sent: int = 0) -> HTMLResponse:
        return page("login.html", request, sent=sent)

    @web.post(f"{base}/login")
    def send_link(request: Request) -> RedirectResponse:
        if st.alert_email:
            token = signer.dumps(f"link:{st.alert_email}")
            safe_send(app.mailer, "Dashboard sign-in link", f"{st.dashboard_url}/auth?token={token}\n\nValid for 15 minutes.")
        return RedirectResponse(f"{base}/login?sent=1", status_code=303)

    @web.get(f"{base}/auth")
    def auth(token: str) -> RedirectResponse:
        try:
            who = signer.loads(token, max_age=LINK_TTL)
        except (BadSignature, SignatureExpired):
            return RedirectResponse(f"{base}/login", status_code=303)
        if who != f"link:{st.alert_email}" or not st.alert_email:
            return RedirectResponse(f"{base}/login", status_code=303)
        resp = RedirectResponse(f"{base}/", status_code=303)
        resp.set_cookie(
            SESSION,
            signer.dumps(st.alert_email),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="strict",
            secure=not st.dashboard_url.startswith("http://localhost"),
        )
        return resp

    @web.get(f"{base}/", response_class=HTMLResponse)
    def status(request: Request) -> HTMLResponse:
        authed(request)
        now = app.now()
        with session_scope(app.db) as s:
            today = now.astimezone(WIB).date()
            ctx = {
                "kill": is_on(s, st.kill_switch),
                "kill_env": st.kill_switch,
                "runs": s.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(25)).all(),
                "today": s.scalars(select(ContentPlan).where(ContentPlan.date == today).order_by(ContentPlan.scheduled_at)).all(),
                "tokens": s.scalars(select(Token)).all(),
                "reserve": s.scalar(select(func.count()).select_from(EvergreenPost).where(EvergreenPost.retired.is_(False))),
                "wib": lambda d: utc(d).astimezone(WIB).strftime("%d %b %H:%M") if d else "",
            }
            return page("status.html", request, **ctx)

    @web.post(f"{base}/kill")
    def kill(request: Request, on: str = Form(...)) -> RedirectResponse:
        who = authed(request)
        with session_scope(app.db) as s:
            set_switch(s, on == "1", who, app.now())
        safe_send(app.mailer, f"Kill switch {'ON' if on == '1' else 'off'}", f"Set from the dashboard by {who}.")
        return RedirectResponse(f"{base}/", status_code=303)

    @web.get(f"{base}/queue", response_class=HTMLResponse)
    def queue(request: Request) -> HTMLResponse:
        authed(request)
        today = app.now().astimezone(WIB).date()
        with session_scope(app.db) as s:
            plans = s.scalars(
                select(ContentPlan)
                .where(ContentPlan.date >= today, ContentPlan.date <= today + timedelta(days=7))
                .order_by(ContentPlan.date, ContentPlan.scheduled_at)
            ).all()
            payloads = {}
            for p in plans:
                row = s.scalars(select(DraftRow).where(DraftRow.plan_id == p.id, DraftRow.status == "payload").order_by(DraftRow.id.desc())).first()
                if row:
                    payloads[p.id] = row.json
            drafts = {}
            for p in plans:
                d = s.scalars(select(DraftRow).where(DraftRow.plan_id == p.id, DraftRow.status.in_(("passed", "failed"))).order_by(DraftRow.id.desc())).first()
                if d:
                    drafts[p.id] = d
            return page(
                "queue.html",
                request,
                plans=plans,
                payloads=payloads,
                drafts=drafts,
                wib=lambda d: utc(d).astimezone(WIB).strftime("%a %d %b %H:%M") if d else "",
            )

    @web.get(f"{base}/posts", response_class=HTMLResponse)
    def posts(request: Request) -> HTMLResponse:
        authed(request)
        with session_scope(app.db) as s:
            rows = s.scalars(select(Post).order_by(Post.id.desc()).limit(100)).all()
            metrics = {}
            for p in rows:
                m = s.scalars(select(Metric).where(Metric.post_id == p.id).order_by(Metric.age_hours.desc())).first()
                if m:
                    metrics[p.id] = m
            return page("posts.html", request, posts=rows, metrics=metrics)

    @web.get(f"{base}/evergreen", response_class=HTMLResponse)
    def evergreen(request: Request) -> HTMLResponse:
        authed(request)
        with session_scope(app.db) as s:
            rows = s.scalars(select(EvergreenPost).order_by(EvergreenPost.id)).all()
            return page("evergreen.html", request, items=rows)

    @web.get(f"{base}/media/{{key:path}}")
    def media(request: Request, key: str) -> Response:
        authed(request)
        if ".." in key or not key.startswith("posts/"):
            raise HTTPException(404)
        try:
            data = app.storage.get(key)
        except Exception as exc:
            raise HTTPException(404) from exc
        return Response(data, media_type="video/mp4" if key.endswith(".mp4") else "image/png")

    @web.get(f"{base}/healthz")
    def healthz() -> dict[str, str]:
        return {"ok": "yes"}

    return web
