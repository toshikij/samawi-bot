"""Graph client retries, container polling, and the Instagram / Threads request contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from publish_fakes import IG_USER, TH_USER, FakeMeta, Sleeps, check_cassette, error
from samawi_bot.publisher.instagram import Instagram
from samawi_bot.publisher.meta import TRANSIENT_CODES, ContainerError, Graph, MetaApiError, wait_for_container
from samawi_bot.publisher.threads import Threads

IG_BASE = "https://graph.test/v23.0"
TH_BASE = "https://threads.test/v1.0"


@pytest.fixture
def meta() -> FakeMeta:
    return FakeMeta()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def http(meta: FakeMeta) -> Iterator[httpx.Client]:
    with httpx.Client(transport=httpx.MockTransport(meta)) as c:
        yield c


@pytest.fixture
def ig(http: httpx.Client, sleeps: Sleeps) -> Instagram:
    return Instagram(Graph(http, IG_BASE, "IG-TOKEN", sleep=sleeps), IG_USER)


@pytest.fixture
def th(http: httpx.Client, sleeps: Sleeps) -> Threads:
    return Threads(Graph(http, TH_BASE, "TH-TOKEN", sleep=sleeps), TH_USER)


def graph_with(handler: Callable[[httpx.Request], httpx.Response], sleeps: Sleeps, attempts: int = 3, base_delay: float = 2.0) -> Graph:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Graph(client, IG_BASE, "IG-TOKEN", sleep=sleeps, attempts=attempts, base_delay=base_delay)


class Script:
    """A handler returning queued responses (or raising queued exceptions) in order."""

    def __init__(self, *items: httpx.Response | Exception):
        self.items = list(items)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


OK = httpx.Response(200, json={"id": "1"})


# ---- Graph: retries and error parsing ---------------------------------------------------------


def test_success_sends_access_token_and_returns_body(sleeps: Sleeps) -> None:
    s = Script(OK)
    g = graph_with(s, sleeps)
    assert g.get("/x", fields="a") == {"id": "1"}
    assert s.requests[0].url.params["access_token"] == "IG-TOKEN"
    assert s.requests[0].url.params["fields"] == "a"
    assert sleeps == []


def test_post_sends_form_body(sleeps: Sleeps) -> None:
    s = Script(OK)
    graph_with(s, sleeps).post("/x", caption="hi there")
    body = dict(httpx.QueryParams(s.requests[0].content.decode()))
    assert body == {"caption": "hi there", "access_token": "IG-TOKEN"}
    assert "access_token" not in s.requests[0].url.params


def test_delete_uses_query_params(sleeps: Sleeps) -> None:
    s = Script(httpx.Response(200, json={"success": True}))
    assert graph_with(s, sleeps).delete("/123") == {"success": True}
    assert s.requests[0].method == "DELETE"
    assert s.requests[0].url.params["access_token"] == "IG-TOKEN"


def test_empty_body_is_empty_dict(sleeps: Sleeps) -> None:
    assert graph_with(Script(httpx.Response(200, content=b"")), sleeps).get("/x") == {}


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_retries_on_5xx_and_429_then_succeeds(status: int, sleeps: Sleeps) -> None:
    s = Script(error(status, None), OK)
    assert graph_with(s, sleeps).get("/x") == {"id": "1"}
    assert len(s.requests) == 2
    assert sleeps == [2.0]


@pytest.mark.parametrize("code", sorted(TRANSIENT_CODES))
def test_retries_on_transient_codes(code: int, sleeps: Sleeps) -> None:
    s = Script(error(400, code), error(400, code), OK)
    assert graph_with(s, sleeps).post("/x") == {"id": "1"}
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_attempts_with_exponential_backoff(sleeps: Sleeps) -> None:
    s = Script(error(503, 2, "Service temporarily unavailable"))
    with pytest.raises(MetaApiError) as ei:
        graph_with(s, sleeps, attempts=4, base_delay=1.5).get("/x")
    assert len(s.requests) == 4
    assert sleeps == [1.5, 3.0, 6.0]  # no sleep after the last attempt
    assert ei.value.status == 503 and ei.value.code == 2 and ei.value.retryable


def test_network_errors_are_retried(sleeps: Sleeps) -> None:
    s = Script(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), OK)
    assert graph_with(s, sleeps).get("/x") == {"id": "1"}
    assert sleeps == [2.0, 4.0]


def test_network_errors_exhausted_raise_meta_error(sleeps: Sleeps) -> None:
    s = Script(httpx.ConnectError("refused"))
    with pytest.raises(MetaApiError, match="network error calling /x") as ei:
        graph_with(s, sleeps).get("/x")
    assert ei.value.status == 0 and ei.value.code is None
    assert len(s.requests) == 3


def test_no_retry_on_bad_parameter(sleeps: Sleeps) -> None:
    s = Script(error(400, 100, "Invalid parameter", subcode=2207006), OK)
    with pytest.raises(MetaApiError) as ei:
        graph_with(s, sleeps).post("/x")
    assert len(s.requests) == 1 and sleeps == []
    e = ei.value
    assert (e.status, e.code, e.subcode) == (400, 100, 2207006)
    assert not e.retryable and not e.token_problem
    assert "Meta API POST /x: Invalid parameter" in str(e)


@pytest.mark.parametrize("code", [102, 190, 463, 467])
def test_token_problem_codes_not_retried(code: int, sleeps: Sleeps) -> None:
    s = Script(error(400, code, "Error validating access token"))
    with pytest.raises(MetaApiError) as ei:
        graph_with(s, sleeps).get("/x")
    assert ei.value.token_problem and not ei.value.retryable
    assert len(s.requests) == 1


def test_http_401_is_a_token_problem(sleeps: Sleeps) -> None:
    with pytest.raises(MetaApiError) as ei:
        graph_with(Script(httpx.Response(401, json={})), sleeps).get("/x")
    assert ei.value.token_problem
    assert "HTTP 401" in str(ei.value)


def test_error_user_msg_preferred(sleeps: Sleeps) -> None:
    r = error(400, 100, "technical", error_user_msg="The aspect ratio is not supported.")
    with pytest.raises(MetaApiError, match="aspect ratio"):
        graph_with(Script(r), sleeps).get("/x")


def test_error_in_200_body_is_an_error(sleeps: Sleeps) -> None:
    with pytest.raises(MetaApiError) as ei:
        graph_with(Script(httpx.Response(200, json={"error": {"code": 100, "message": "nope"}})), sleeps).get("/x")
    assert ei.value.status == 200 and ei.value.code == 100


def test_non_json_body(sleeps: Sleeps) -> None:
    s = Script(httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(MetaApiError, match="non-JSON response"):
        graph_with(s, sleeps).get("/x")
    assert len(s.requests) == 1  # a 200 that is not JSON is not retryable


def test_non_json_gateway_error_is_retried(sleeps: Sleeps) -> None:
    s = Script(httpx.Response(502, text="<html>Bad Gateway</html>"), OK)
    assert graph_with(s, sleeps).get("/x") == {"id": "1"}
    assert sleeps == [2.0]


@pytest.mark.parametrize("body", [[1, 2], "str", 3])
def test_non_dict_json(body: object, sleeps: Sleeps) -> None:
    with pytest.raises(MetaApiError, match="unexpected response shape"):
        graph_with(Script(httpx.Response(200, json=body)), sleeps).get("/x")


# ---- container polling ------------------------------------------------------------------------


@pytest.mark.parametrize("final", ["FINISHED", "PUBLISHED"])
def test_wait_for_container_done(final: str, meta: FakeMeta, ig: Instagram, sleeps: Sleeps) -> None:
    meta.statuses_for_next(["IN_PROGRESS", "IN_PROGRESS", final])
    cid = ig.graph.post(f"/{IG_USER}/media", image_url="https://cdn/x.png")["id"]
    wait_for_container(ig.graph, cid, "status_code", interval=7.0)
    assert sleeps == [7.0, 7.0]


def test_wait_for_container_stuck_in_progress_times_out(meta: FakeMeta, ig: Instagram, sleeps: Sleeps) -> None:
    meta.statuses_for_next(["IN_PROGRESS"])
    cid = ig.graph.post(f"/{IG_USER}/media", image_url="https://cdn/x.png")["id"]
    with pytest.raises(ContainerError, match="still IN_PROGRESS after 24 polls") as ei:
        wait_for_container(ig.graph, cid, "status_code")
    assert ei.value.status == 408
    assert len(sleeps) == 24 and set(sleeps) == {5.0}


def test_wait_for_container_error_state_ig(meta: FakeMeta, ig: Instagram, sleeps: Sleeps) -> None:
    meta.statuses_for_next(["ERROR"])
    cid = ig.graph.post(f"/{IG_USER}/media", image_url="https://cdn/x.png")["id"]
    with pytest.raises(ContainerError, match="ERROR: Error: Media upload has failed"):
        wait_for_container(ig.graph, cid, "status_code")
    assert sleeps == []


def test_wait_for_container_expired_without_detail(meta: FakeMeta, ig: Instagram) -> None:
    meta.statuses_for_next(["EXPIRED"])
    cid = ig.graph.post(f"/{IG_USER}/media", image_url="https://cdn/x.png")["id"]
    with pytest.raises(ContainerError, match="EXPIRED: no detail"):
        wait_for_container(ig.graph, cid, "status_code")


def test_wait_for_container_threads_error_message(meta: FakeMeta, th: Threads) -> None:
    meta.statuses_for_next(["ERROR"])
    cid = th.graph.post(f"/{TH_USER}/threads", media_type="TEXT", text="x")["id"]
    with pytest.raises(ContainerError, match="FAILED_PROCESSING_IMAGE"):
        wait_for_container(th.graph, cid, "status")


# ---- Instagram contracts ----------------------------------------------------------------------


def urls(n: int) -> list[str]:
    return [f"https://cdn.test/posts/2026-11-26/siang_feed/{i:02d}.png" for i in range(1, n + 1)]


@pytest.mark.parametrize("n", [0, 1, 11])
def test_carousel_size_rejected_before_any_call(n: int, meta: FakeMeta, ig: Instagram) -> None:
    with pytest.raises(ValueError, match=f"2–10 images, got {n}"):
        ig.carousel(urls(n), "cap")
    assert meta.requests == []


def test_carousel_two_items_contract(meta: FakeMeta, ig: Instagram) -> None:
    pub = ig.carousel(urls(2), "Caption #samawi")
    assert pub.media_id == meta.ig_published[0].media_id
    assert pub.permalink == f"https://www.instagram.com/p/{pub.media_id}/"
    check_cassette("ig_carousel_2", meta.requests)


def test_carousel_ten_items(meta: FakeMeta, ig: Instagram) -> None:
    pub = ig.carousel(urls(10), "cap")
    children = meta.calls("POST", f"/{IG_USER}/media")
    assert len(children) == 11
    assert all(c.params["is_carousel_item"] == "true" for c in children[:10])
    parent = children[10].params
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"].split(",") == [f"ig_c{i}" for i in range(1, 11)]
    assert len(meta.calls("POST", f"/{IG_USER}/media_publish")) == 1
    assert pub.media_id


def test_carousel_child_error_stops_before_parent(meta: FakeMeta, ig: Instagram) -> None:
    meta.statuses_for_next(["FINISHED"], ["ERROR"])
    with pytest.raises(ContainerError):
        ig.carousel(urls(3), "cap")
    assert len(meta.calls("POST", f"/{IG_USER}/media")) == 2
    assert meta.calls("POST", f"/{IG_USER}/media_publish") == []


def test_reel_with_cover_contract(meta: FakeMeta, ig: Instagram) -> None:
    ig.reel("https://cdn.test/reel.mp4", "Reel caption", cover_url="https://cdn.test/cover.png")
    check_cassette("ig_reel_cover", meta.requests)


def test_reel_without_cover(meta: FakeMeta, ig: Instagram) -> None:
    ig.reel("https://cdn.test/reel.mp4", "Reel caption")
    params = meta.calls("POST", f"/{IG_USER}/media")[0].params
    assert "cover_url" not in params
    assert params["media_type"] == "REELS" and params["share_to_feed"] == "true"


def test_reel_polls_up_to_60(meta: FakeMeta, ig: Instagram, sleeps: Sleeps) -> None:
    meta.statuses_for_next(["IN_PROGRESS"] * 59 + ["FINISHED"])
    ig.reel("https://cdn.test/reel.mp4", "c")
    assert len(sleeps) == 59


def test_story_contract(meta: FakeMeta, ig: Instagram) -> None:
    ig.story("https://cdn.test/story.png")
    check_cassette("ig_story", meta.requests)


def test_single_image(meta: FakeMeta, ig: Instagram) -> None:
    pub = ig.single_image("https://cdn.test/one.png", "c")
    params = meta.calls("POST", f"/{IG_USER}/media")[0].params
    assert params == {"image_url": "https://cdn.test/one.png", "caption": "c", "access_token": "IG-TOKEN"}
    assert pub.permalink


def test_permalink_failure_does_not_fail_publish(meta: FakeMeta, ig: Instagram) -> None:
    meta.fail("GET", r"/ig_m\d+", error(500, 1, "Unknown error"), times=3)
    pub = ig.story("https://cdn.test/story.png")
    assert pub.permalink is None
    assert pub.media_id in meta.published


def test_publish_not_ready_raises(meta: FakeMeta, ig: Instagram) -> None:
    meta.fail("GET", r"/ig_c\d+", lambda: httpx.Response(200, json={"status_code": "FINISHED"}))
    meta.statuses_for_next(["IN_PROGRESS"])
    with pytest.raises(MetaApiError) as ei:
        ig.story("https://cdn.test/story.png")
    assert ei.value.code == 9007


def test_delete(meta: FakeMeta, ig: Instagram) -> None:
    pub = ig.story("https://cdn.test/s.png")
    ig.delete(pub.media_id)
    assert pub.media_id not in meta.published
    assert meta.requests[-1].method == "DELETE"


def test_private_reply_payload(meta: FakeMeta, ig: Instagram) -> None:
    ig.private_reply("17900000000000001", "Assalamualaikum! Ini link-nya: https://play.test/samawi")
    check_cassette("ig_private_reply", meta.requests)
    p = meta.requests[0].params
    assert json.loads(p["recipient"]) == {"comment_id": "17900000000000001"}
    assert json.loads(p["message"]) == {"text": "Assalamualaikum! Ini link-nya: https://play.test/samawi"}


def test_publishing_limit(meta: FakeMeta, ig: Instagram) -> None:
    meta.ig_quota = {"quota_usage": 12, "config": {"quota_total": 50}}
    assert ig.publishing_limit() == (12, 50)
    check_cassette("ig_publishing_limit", meta.requests)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, (0, 50)),
        ({"data": []}, (0, 50)),
        ({"data": [{}]}, (0, 50)),
        ({"data": [{"quota_usage": 7}]}, (7, 50)),
        ({"data": [{"quota_usage": 7, "config": {}}]}, (7, 50)),
        ({"data": [{"config": {"quota_total": 25}}]}, (0, 25)),
    ],
)
def test_publishing_limit_missing_fields(data: dict[str, object], expected: tuple[int, int], meta: FakeMeta, ig: Instagram) -> None:
    meta.fail("GET", rf"/{IG_USER}/content_publishing_limit", httpx.Response(200, json=data))
    assert ig.publishing_limit() == expected


def test_insights_total_value_and_values(meta: FakeMeta, ig: Instagram) -> None:
    meta.insights["ig_m9"] = {
        "data": [
            {"name": "reach", "period": "lifetime", "total_value": {"value": 1500}},
            {"name": "saved", "period": "lifetime", "values": [{"value": 42}]},
            {"name": "shares", "values": [{}]},
            {"name": "likes"},
            {"name": "comments", "total_value": {"value": None}},
            {"name": "follows", "total_value": {}, "values": [{"value": 3}]},
        ]
    }
    assert ig.insights("ig_m9", ["reach", "saved"]) == {"reach": 1500, "saved": 42, "shares": 0, "likes": 0, "comments": 0, "follows": 3}
    assert meta.requests[0].params["metric"] == "reach,saved"


def test_insights_empty(ig: Instagram) -> None:
    assert ig.insights("ig_m1", ["reach"]) == {}


def test_account_and_comments(meta: FakeMeta, ig: Instagram) -> None:
    meta.comments["ig_m3"] = [{"id": "c1", "text": "SAMAWI", "from": {"id": "u1", "username": "a"}}]
    assert ig.account()["username"] == "samawi.indonesia"
    assert ig.comments("ig_m3") == meta.comments["ig_m3"]
    assert meta.requests[1].params["limit"] == "100"
    assert ig.comments("ig_m4") == []


# ---- Threads contracts ------------------------------------------------------------------------


def test_threads_text_500_ok_501_rejected(meta: FakeMeta, th: Threads) -> None:
    assert th.text("a" * 500)
    with pytest.raises(ValueError, match="501 characters; the cap is 500"):
        th.text("a" * 501)
    assert len(meta.calls("POST", f"/{TH_USER}/threads")) == 1


def test_threads_text_reply_contract(meta: FakeMeta, th: Threads) -> None:
    first = th.text("Post satu")
    th.text("Post dua", reply_to=first)
    check_cassette("threads_text_reply", meta.requests)


def test_threads_image_with_and_without_reply(meta: FakeMeta, th: Threads) -> None:
    first = th.image("Hook", "https://cdn.test/1.png")
    th.image("Lanjut", "https://cdn.test/2.png", reply_to=first)
    a, b = meta.calls("POST", f"/{TH_USER}/threads")
    assert a.params["media_type"] == "IMAGE" and "reply_to_id" not in a.params
    assert b.params["reply_to_id"] == first


@pytest.mark.parametrize("n", [1, 21])
def test_threads_carousel_size_rejected(n: int, meta: FakeMeta, th: Threads) -> None:
    with pytest.raises(ValueError, match=f"2–20 items, got {n}"):
        th.carousel("t", urls(n))
    assert meta.requests == []


@pytest.mark.parametrize("n", [2, 20])
def test_threads_carousel_ok(n: int, meta: FakeMeta, th: Threads) -> None:
    pid = th.carousel("Hook", urls(n))
    posts = meta.calls("POST", f"/{TH_USER}/threads")
    assert len(posts) == n + 1
    assert all(
        p.params == {"media_type": "IMAGE", "image_url": u, "is_carousel_item": "true", "access_token": "TH-TOKEN"}
        for p, u in zip(posts, urls(n), strict=False)
    )
    assert posts[-1].params["media_type"] == "CAROUSEL"
    assert len(posts[-1].params["children"].split(",")) == n
    assert pid in meta.published


def test_threads_carousel_contract_with_reply(meta: FakeMeta, th: Threads) -> None:
    first = th.text("Halo")
    th.carousel("Gambar", urls(2), reply_to=first)
    check_cassette("threads_carousel_reply", meta.requests)


def test_chain_replies_to_previous(meta: FakeMeta, th: Threads) -> None:
    seen: list[tuple[int, str]] = []
    ids = th.chain(["satu", "dua", "tiga"], [], lambda i, pid: seen.append((i, pid)))
    assert [pid for _, pid in seen] == ids and [i for i, _ in seen] == [0, 1, 2]
    creates = meta.calls("POST", f"/{TH_USER}/threads")
    assert "reply_to_id" not in creates[0].params
    assert creates[1].params["reply_to_id"] == ids[0]
    assert creates[2].params["reply_to_id"] == ids[1]
    check_cassette("threads_chain_3", meta.requests)


def test_chain_resumes_without_reposting(meta: FakeMeta, th: Threads) -> None:
    first = th.chain(["satu", "dua", "tiga", "empat"][:2], [], lambda i, p: None)
    before = len(meta.threads_published)
    ids = th.chain(["satu", "dua", "tiga", "empat"], first, lambda i, p: None)
    assert ids[:2] == first and len(ids) == 4
    assert len(meta.threads_published) == before + 2
    creates = meta.calls("POST", f"/{TH_USER}/threads")[2:]
    assert [c.params["text"] for c in creates] == ["tiga", "empat"]
    assert creates[0].params["reply_to_id"] == first[1]


def test_chain_already_complete_posts_nothing(meta: FakeMeta, th: Threads) -> None:
    assert th.chain(["a"], ["th_m1"], lambda i, p: None) == ["th_m1"]
    assert meta.requests == []


def test_chain_before_each_stops_mid_chain(meta: FakeMeta, th: Threads) -> None:
    class Stop(Exception):
        pass

    calls = {"n": 0}

    def guard() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise Stop

    saved: list[str] = []
    with pytest.raises(Stop):
        th.chain(["a", "b", "c", "d"], [], lambda i, p: saved.append(p), before_each=guard)
    assert len(saved) == 2 and len(meta.threads_published) == 2


def test_chain_first_post_one_image_is_image(meta: FakeMeta, th: Threads) -> None:
    th.chain(["hook", "dua"], [], lambda i, p: None, first_images=["https://cdn.test/cover.png"])
    creates = meta.calls("POST", f"/{TH_USER}/threads")
    assert creates[0].params["media_type"] == "IMAGE" and creates[0].params["image_url"] == "https://cdn.test/cover.png"
    assert creates[1].params["media_type"] == "TEXT"


def test_chain_first_post_many_images_is_carousel(meta: FakeMeta, th: Threads) -> None:
    ids = th.chain(["hook", "dua"], [], lambda i, p: None, first_images=urls(3))
    check_cassette("threads_chain_carousel_first", meta.requests)
    assert meta.published[ids[0]].params["media_type"] == "CAROUSEL"


def test_threads_permalink_delete_me(meta: FakeMeta, th: Threads) -> None:
    pid = th.text("x")
    assert th.permalink(pid) == f"https://www.threads.net/@samawi.indonesia/post/{pid}/"
    assert th.permalink("nope") is None
    th.delete(pid)
    assert pid not in meta.published
    assert th.me() == {"id": TH_USER, "username": "samawi.indonesia"}


def test_threads_publishing_limit(meta: FakeMeta, th: Threads) -> None:
    meta.th_quota = {"quota_usage": 3, "config": {"quota_total": 250}, "reply_quota_usage": 9, "reply_config": {"quota_total": 1000}}
    assert th.publishing_limit() == (3, 250, 9, 1000)
    assert meta.requests[0].params["fields"] == "quota_usage,config,reply_quota_usage,reply_config"


@pytest.mark.parametrize(
    "data,expected",
    [({}, (0, 250, 0, 1000)), ({"data": [{"reply_quota_usage": 5}]}, (0, 250, 5, 1000)), ({"data": [{"config": {}, "reply_config": {}}]}, (0, 250, 0, 1000))],
)
def test_threads_publishing_limit_missing_fields(data: dict[str, object], expected: tuple[int, ...], meta: FakeMeta, th: Threads) -> None:
    meta.fail("GET", rf"/{TH_USER}/threads_publishing_limit", httpx.Response(200, json=data))
    assert th.publishing_limit() == expected


def test_threads_insights(meta: FakeMeta, th: Threads) -> None:
    meta.insights["th_m1"] = {"data": [{"name": "views", "values": [{"value": 99}]}, {"name": "likes", "total_value": {"value": 4}}, {"name": "replies"}]}
    assert th.insights("th_m1") == {"views": 99, "likes": 4, "replies": 0}
    assert meta.requests[0].params["metric"] == "views,likes,replies,reposts,quotes,shares"


def test_expired_token_rejected_by_fake(meta: FakeMeta, http: httpx.Client, sleeps: Sleeps) -> None:
    g = Graph(http, IG_BASE, "OLD-TOKEN", sleep=sleeps)
    with pytest.raises(MetaApiError) as ei:
        Instagram(g, IG_USER).story("https://cdn.test/s.png")
    assert ei.value.token_problem and ei.value.subcode == 463
    assert sleeps == []


def test_meta_errors_never_carry_presigned_query_strings() -> None:
    from samawi_bot.publisher.meta import MetaApiError, redact_urls

    url = "https://bucket.s3.ap-southeast-3.amazonaws.com/posts/x.png?X-Amz-Credential=ASIA123&X-Amz-Security-Token=SECRET"
    err = MetaApiError(400, 9004, f"The media could not be fetched from this URI: {url}")
    assert "SECRET" not in str(err) and "ASIA123" not in str(err)
    assert "https://bucket.s3.ap-southeast-3.amazonaws.com/posts/x.png?…" in str(err)
    assert redact_urls("no url here") == "no url here"
