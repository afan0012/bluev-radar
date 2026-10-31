"""免费数据源：twscrape + 小号 cookie 读 X 网页版接口。
没有可用小号（没配置 / 被限流 / 被锁）时抛 Unavailable，调用方退回 monid。
关注、粉丝、蓝V粉丝是 X 的不同接口，各自单独限流，互不影响。"""
import asyncio
import os
import threading
from pathlib import Path

os.environ.setdefault("TWS_TELEMETRY", "0")
os.environ.setdefault("TWS_LOG_LEVEL", "WARNING")

from twscrape import API, NoAccountError  # noqa: E402
from twscrape.models import parse_users  # noqa: E402

PAGE_SIZE = 100

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
# 账号都在用时最多等 20 秒，真被限流就直接抛 NoAccountError
api = API(str(Path(__file__).parent / "accounts.db"), raise_when_no_account=True, wait_timeout=20)
_uids = {}


class Unavailable(Exception):
    pass


class NotFound(Exception):
    pass


def _run(coro, timeout=180):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def add_account(username, cookies):
    _run(api.pool.add_account_cookies(username, cookies))


def accounts():
    return [{"username": a.username, "active": a.active, "error": a.error_msg, "locks": a.locks}
            for a in _run(api.pool.get_all())]


def available():
    try:
        return any(a.active for a in _run(api.pool.get_all()))
    except Exception:
        return False


def _norm(u):
    """统一成 tikhub 的字段名，后面的代码不用区分数据源"""
    return {"user_id": str(u.id), "screen_name": u.username, "name": u.displayname,
            "profile_image": u.profileImageUrl, "followers_count": u.followersCount,
            "description": u.rawDescription, "blue_verified": bool(u.blue)}


def user_id(username):
    key = username.lower()
    if key not in _uids:
        try:
            u = _run(api.user_by_login(username))
        except NoAccountError as e:
            raise Unavailable(str(e))
        if not u:
            raise NotFound(username)
        _uids[key] = u.id
    return _uids[key]


def search(query, limit=40):
    """搜最新推文，返回 anyapi /twitter.search 格式的 items"""
    async def go():
        return [t async for t in api.search(query, limit=limit)]
    try:
        tweets = _run(go())
    except NoAccountError as e:
        raise Unavailable(str(e))
    return [{"authorId": str(t.user.id), "authorUsername": t.user.username, "authorName": t.user.displayname,
             "authorImage": t.user.profileImageUrl, "authorFollowers": t.user.followersCount,
             "authorVerified": bool(t.user.blue), "createdUtc": int(t.date.timestamp()), "text": t.rawContent}
            for t in tweets]


def pages(kind, username):
    """同步生成器，逐页 yield 用户列表。kind: following / followers / blue_followers"""
    uid = user_id(username)
    fn = {"following": api.following_raw, "followers": api.followers_raw,
          "blue_followers": api.verified_followers_raw}[kind]
    gen = fn(uid, kv={"count": PAGE_SIZE})
    try:
        while True:
            try:
                rep = _run(gen.__anext__())
            except StopAsyncIteration:
                return
            except NoAccountError as e:
                raise Unavailable(str(e))
            yield [_norm(u) for u in parse_users(rep)]
    finally:
        try:
            _run(gen.aclose(), timeout=30)
        except Exception:
            pass
