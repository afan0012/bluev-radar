"""未关注中文蓝V查找器

中文蓝V池（monid 搜索采集 + 主动入驻 + 粉丝列表顺带发现） − 用户已关注 = 未关注列表
回关率：记录站内“关注”点击，之后读点击者的粉丝列表核验对方有没有回关。
运行: python app.py  然后打开 http://localhost:8000
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = Path(__file__).parent
DB_PATH = BASE / "data.db"

KEYWORDS = os.getenv("KEYWORDS", "互关,浇水,浇朋友,蓝V,有关必回,回关,新人,互粉").split(",")
COLLECT_PAGES = int(os.getenv("COLLECT_PAGES", "1"))             # 每个关键词翻几页（20条/页，约$0.002/页）
COLLECT_INTERVAL_MIN = int(os.getenv("COLLECT_INTERVAL_MIN", "180"))  # 定时采集间隔，0=关闭
MAX_FOLLOWING_PAGES = int(os.getenv("MAX_FOLLOWING_PAGES", "100"))  # 关注列表最多翻几页（50人/页，$0.001/页）
FOLLOWER_PAGES = int(os.getenv("FOLLOWER_PAGES", "40"))             # 核验回关时最多读几页最新粉丝
FOLLOWING_CACHE_H = float(os.getenv("FOLLOWING_CACHE_H", "24"))      # 关注列表缓存小时数
BACK_WAIT_H = float(os.getenv("BACK_WAIT_H", "72"))                 # 点关注后多久没回关算“不回”
JOIN_PER_IP_HOUR = int(os.getenv("JOIN_PER_IP_HOUR", "5"))          # 每个IP每小时最多入驻次数（防刷 monid 费用）
CHECK_PER_IP_DAY = int(os.getenv("CHECK_PER_IP_DAY", "5"))          # 每个IP每天最多真正查几次（命中缓存不算）
CHECK_BUDGET_DAY = float(os.getenv("CHECK_BUDGET_DAY", "1.0"))      # 用户查询每天最多花多少美元
MAX_FANS = int(os.getenv("MAX_FANS", "100000"))                    # 粉丝超过这个数的号（媒体/大V）基本不回关，不展示
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")                          # 手动触发采集/扩池的口令，空=禁用这两个接口
DAILY_HARD_CAP = float(os.getenv("DAILY_HARD_CAP", "3.0"))          # 所有 monid 花费每天的硬上限（保险丝），到了就不再付费读
JOIN_ENABLED = os.getenv("JOIN_ENABLED", "") == "1"                 # “我要上榜”接口，前端隐藏期间默认关闭
CLICK_PER_IP_DAY = int(os.getenv("CLICK_PER_IP_DAY", "600"))        # 每个IP每天最多记录多少次点击
PORT = int(os.getenv("PORT", "8000"))

CJK = re.compile(r"[一-鿿]")
# 日文：假名，或日本特有的汉字写法（中文里基本不用）
JP = re.compile(r"[぀-ヿㇰ-ㇿｦ-ﾟ]|[辺沢斎齋斉広栄浜県円込畑峠匂枠働塚滝渕桜駅鉄売読様竜髙﨑実気発図対単黒楽伝訳険験戦経継歩恵]|公式|株式|会社員")


def is_chinese_name(text):
    return bool(CJK.search(text)) and not JP.search(text)


def log(*a):
    print(*a, flush=True)


# ---------- monid ----------

def _monid_cmd():
    exe = shutil.which("monid")
    if exe is None:
        raise RuntimeError("找不到 monid，请先安装并配置 MONID_API_KEY")
    # Windows 的 monid.cmd 会被 cmd.exe 二次解析，JSON 里的引号和 | 会出问题，直接调 node
    if exe.lower().endswith(".cmd"):
        js = Path(exe).parent / "node_modules" / "@monid-ai" / "cli" / "dist" / "index.js"
        if js.exists():
            return [shutil.which("node") or "node", str(js)]
    return [exe]


MONID = None


def monid(provider, endpoint, query=None, body=None, timeout=120):
    global MONID
    MONID = MONID or _monid_cmd()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.json"
        cmd = MONID + ["run", "-p", provider, "-e", endpoint, "-w", "90", "-j", "-o", str(out)]
        if query:
            cmd += ["--query", json.dumps(query, ensure_ascii=False)]
        if body:
            inp = Path(td) / "in.json"
            inp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            cmd += ["-f", str(inp)]
        env = {**os.environ, "NO_COLOR": "1", "MSYS_NO_PATHCONV": "1"}
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        if not out.exists():
            raise RuntimeError(f"monid 调用失败: {(p.stdout + p.stderr)[-400:]}")
        return json.loads(out.read_text(encoding="utf-8"))


def add_spend(usd):
    k = "spend:" + time.strftime("%Y-%m-%d")
    with db() as c:
        c.execute("INSERT INTO meta VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=CAST(v AS REAL)+?",
                  (k, usd, usd))


def today_spend():
    return float(meta_get("spend:" + time.strftime("%Y-%m-%d"), 0))


# ---------- 关注/粉丝列表：优先小号免费读（xsource），不可用时退回 monid ----------

try:
    import xsource as xs
except ImportError:
    xs = None

MONID_LISTS = {
    "following": ("/api/v1/twitter/web/fetch_user_followings", "following"),
    "followers": ("/api/v1/twitter/web/fetch_user_followers", "followers"),
    "blue_followers": ("/api/v1/twitter/web/fetch_user_followers", "followers"),  # monid 没有只读蓝V的接口
}


def free_ok():
    return xs is not None and xs.available()


def under_cap():
    return today_spend() < DAILY_HARD_CAP


def user_pages(kind, username, can_pay=under_cap):
    """逐页 yield (用户列表, 花费)。小号读到一半失败会抛异常（不花钱重读）；
    一页都没读到才退回 monid，每页付费前先问 can_pay()"""
    if free_ok():
        n = 0
        try:
            for page in xs.pages(kind, username):
                n += 1
                yield page, 0
            return
        except xs.NotFound:
            raise RuntimeError("找不到这个用户，或者账号已注销 / 被冻结")
        except Exception as e:
            log(f"[x] {kind} @{username} 第 {n + 1} 页失败: {e}")
            if n:
                raise RuntimeError("读取中断")
    endpoint, key = MONID_LISTS[kind]
    cursor = None
    while under_cap() and can_pay():
        q = {"screen_name": username}
        if cursor:
            q["cursor"] = cursor
        d = monid("tikhub", endpoint, query=q)
        add_spend(0.001)
        page = d.get(key)
        if page is None:
            raise RuntimeError(f"接口返回异常: {json.dumps(d, ensure_ascii=False)[:200]}")
        yield page, 0.001
        cursor = d.get("next_cursor")
        if not (d.get("more_users") and cursor and page):
            return


def fetch_pages(kind, username, max_pages, on_page=None, stop=None):
    """翻页读关注/粉丝列表，返回 (用户列表, 是否读完)"""
    users, pages, done = [], 0, True
    for page, _ in user_pages(kind, username):
        users += page
        pages += 1
        if on_page:
            on_page(pages, len(users))
        if pages >= max_pages or (stop and stop(users)):
            done = False
            break
    return users, done


# ---------- 数据库 ----------

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS pool(
            user_id TEXT PRIMARY KEY, username TEXT, name TEXT, avatar TEXT,
            followers INT, last_seen INT, last_tweet TEXT, added_at INT);
        CREATE TABLE IF NOT EXISTS following_cache(
            username TEXT PRIMARY KEY COLLATE NOCASE, ids TEXT, count INT,
            complete INT, fetched_at INT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS clicks(
            from_user TEXT COLLATE NOCASE, to_id TEXT, clicked_at INT,
            status TEXT DEFAULT 'pending', resolved_at INT,
            PRIMARY KEY(from_user, to_id));
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(pool)")}
        if "source" not in cols:   # search / self / follower
            c.execute("ALTER TABLE pool ADD COLUMN source TEXT DEFAULT 'search'")
        if "joined_at" not in cols:
            c.execute("ALTER TABLE pool ADD COLUMN joined_at INT")
        if "mined_at" not in cols:   # 作为种子读过粉丝的时间
            c.execute("ALTER TABLE pool ADD COLUMN mined_at INT")
        if "mine_yield" not in cols:  # 作为种子带来了多少新人
            c.execute("ALTER TABLE pool ADD COLUMN mine_yield INT")
        c.execute("""CREATE TABLE IF NOT EXISTS mine_pages(
            seed_id TEXT, page INT, read INT, added INT, at INT)""")


def meta_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))


def upsert_pool(c, uid, username, name, avatar, followers, seen=0, tweet="", source="search"):
    new = c.execute("SELECT 1 FROM pool WHERE user_id=?", (uid,)).fetchone() is None
    c.execute("""
        INSERT INTO pool(user_id, username, name, avatar, followers, last_seen, last_tweet, added_at, source)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, name=excluded.name, avatar=excluded.avatar,
            followers=excluded.followers,
            last_tweet=CASE WHEN excluded.last_seen>pool.last_seen THEN excluded.last_tweet ELSE pool.last_tweet END,
            last_seen=MAX(pool.last_seen, excluded.last_seen)
    """, (uid, username, name, avatar, followers or 0, seen, tweet[:140], int(time.time()), source))
    return int(new)


# ---------- 采集中文蓝V池 ----------

collect_lock = threading.Lock()


def collect():
    if not collect_lock.acquire(blocking=False):
        return {"skipped": "采集正在进行"}
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        added = seen = calls = 0
        for kw in KEYWORDS:
            if free_ok():
                try:
                    items = xs.search(f"{kw.strip()} lang:zh filter:blue_verified since:{since}")
                    with db() as c:
                        for t in items:
                            if t["authorVerified"]:
                                seen += 1
                                added += upsert_pool(c, t["authorId"], t["authorUsername"], t["authorName"],
                                                     t["authorImage"], t["authorFollowers"], t["createdUtc"], t["text"])
                    continue
                except Exception as e:
                    log(f"[collect] {kw} 小号搜索失败，改用 monid: {e}")
            if not under_cap():
                log("[collect] 今日花费已到上限，跳过 monid 搜索")
                break
            cursor = None
            for _ in range(COLLECT_PAGES):
                body = {"query": f"{kw.strip()} lang:zh filter:blue_verified since:{since}", "limit": 20}
                if cursor:
                    body["cursor"] = cursor
                try:
                    d = monid("anyapi", "/twitter.search", body=body)
                except Exception as e:
                    log(f"[collect] {kw} 失败: {e}")
                    break
                calls += 1
                add_spend(0.002)
                data = d.get("data", d)
                items = data.get("items") or []
                with db() as c:
                    for t in items:
                        if t.get("authorVerified") and t.get("authorId"):
                            seen += 1
                            added += upsert_pool(c, t["authorId"], t.get("authorUsername"), t.get("authorName"),
                                                 t.get("authorImage"), t.get("authorFollowers"),
                                                 int(t.get("createdUtc") or time.time()), t.get("text") or "")
                cursor = data.get("nextCursor")
                if not cursor or not items:
                    break
        meta_set("last_collect", int(time.time()))
        res = {"added": added, "seen": seen, "calls": calls}
        log(f"[collect] {res}")
        return res
    finally:
        collect_lock.release()


def collect_loop():
    while True:
        try:
            collect()
            today = time.strftime("%Y-%m-%d")
            if meta_get("last_sentinel") != today:
                sentinel()
                meta_set("last_sentinel", today)
        except Exception as e:
            log(f"[collect] 异常: {e}")
        time.sleep(COLLECT_INTERVAL_MIN * 60)


FREE_MINE_INTERVAL_MIN = int(os.getenv("FREE_MINE_INTERVAL_MIN", "20"))   # 有小号时，每隔多久免费扩池一轮


def free_mine_loop():
    """小号免费扩池：每轮读到限流为止（预算 0，不会退回 monid 花钱），等限流解除再继续"""
    while True:
        try:
            if free_ok():
                mine(budget=0)
        except Exception as e:
            log(f"[mine] 异常: {e}")
        time.sleep(FREE_MINE_INTERVAL_MIN * 60)


def add_blue_followers(c, users):
    """把粉丝列表里的中文蓝V（昵称/简介含中文）收进池子，返回新增数"""
    added = 0
    for u in users:
        name = u.get("name") or ""
        if u.get("blue_verified") and u.get("user_id") and is_chinese_name(name + (u.get("description") or "")):
            added += upsert_pool(c, u["user_id"], u.get("screen_name"), name, u.get("profile_image"),
                                 u.get("followers_count"), source="follower")
    return added


# ---------- 滚雪球扩池：读种子蓝V的粉丝，把里面的中文蓝V收进池子 ----------
# 粉丝列表按关注时间倒序，越靠前越是新人。逐页读，某页新增太少就换下一个种子（越往后越重复，不值得）。
# 每个种子的产出记在 pool.mine_yield，产出最高的一批当“哨兵”，每天只读第 1 页抓新蓝V。

MINE_PAGES = int(os.getenv("MINE_PAGES", "10"))            # 每个种子最多读几页粉丝（50人/页，$0.001/页）
MINE_MIN_NEW = int(os.getenv("MINE_MIN_NEW", "8"))         # 某页新增少于这个数就停止这个种子
MINE_WORKERS = int(os.getenv("MINE_WORKERS", "5"))         # 扩池并发数（每个并发是一个 node 进程）
SENTINELS = int(os.getenv("SENTINELS", "30"))              # 哨兵数量，每天各读 1 页
mine_lock = threading.Lock()


def mine(budget=1.0, pages=MINE_PAGES, min_new=MINE_MIN_NEW, seeds=None, page_from=1):
    """按预算一轮轮滚雪球，直到花完或没有种子。seeds 指定种子时只读这些（哨兵用）"""
    if not mine_lock.acquire(blocking=False):
        return {"skipped": "扩池正在进行"}
    try:
        max_calls = int(budget / 0.001)
        stat = {"calls": 0, "added": 0, "done": 0, "pages": 0}
        lock = threading.Lock()

        def can_pay():
            with lock:
                if stat["calls"] >= max_calls:
                    return False
                stat["calls"] += 1
                return True

        def one(r):
            got = read = 0
            try:
                # 小号读的是“蓝V粉丝”接口（只返回蓝V，100人/页）；退回 monid 时读全部粉丝
                for users, _ in user_pages("blue_followers", r["username"], can_pay):
                    read += 1
                    with lock, db() as c:
                        n = add_blue_followers(c, users)
                        c.execute("INSERT INTO mine_pages VALUES(?,?,?,?,?)",
                                  (r["user_id"], read, len(users), n, int(time.time())))
                        stat["added"] += n
                        stat["pages"] += 1
                    got += n
                    if read >= pages or n < min_new:
                        break
            except Exception as e:
                log(f"[mine] @{r['username']} 失败: {e}")
                if "找不到" in str(e):     # 改名 / 注销的种子标记为已挖，免得每轮都排在最前面
                    with lock, db() as c:
                        c.execute("UPDATE pool SET mined_at=?, mine_yield=0 WHERE user_id=?",
                                  (int(time.time()), r["user_id"]))
            if not read:
                return
            with lock, db() as c:
                c.execute("""UPDATE pool SET mined_at=?, mine_yield=MAX(COALESCE(mine_yield,0), ?)
                             WHERE user_id=?""", (int(time.time()), got, r["user_id"]))
                stat["done"] += 1
                log(f"[mine] {stat['done']} @{r['username']}: 新增 {got}，累计新增 {stat['added']}，"
                    f"花费 ${stat['calls'] * 0.001:.3f}")

        from concurrent.futures import ThreadPoolExecutor
        workers = MINE_WORKERS
        if free_ok():   # 小号每个接口同一时间只能被一个任务占用，并发数 = 小号数
            workers = max(1, sum(a["active"] for a in xs.accounts()))
        while True:
            before = stat["pages"]
            with db() as c:
                if seeds is not None:
                    rows = seeds
                else:
                    # 新种子优先：发过互关推文的（search）> 粉丝多的
                    rows = c.execute("""SELECT user_id, username, name FROM pool WHERE mined_at IS NULL
                                        ORDER BY source='search' DESC, last_seen DESC, followers DESC
                                        LIMIT ?""", (MINE_WORKERS * 10,)).fetchall()
                    # 非中文名的种子直接跳过（标记为已挖，产出 0），避免滚进日本圈
                    skip = [r for r in rows if not is_chinese_name(r["name"] or "")]
                    c.executemany("UPDATE pool SET mined_at=?, mine_yield=0 WHERE user_id=?",
                                  [(int(time.time()), r["user_id"]) for r in skip])
                    rows = [r for r in rows if is_chinese_name(r["name"] or "")]
                    if skip and not rows:
                        continue
            if not rows:
                break
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(one, rows))
            # 指定了种子只跑一轮；一页都没读到（预算花完 / 小号限流）就停
            if seeds is not None or stat["pages"] == before:
                break
        with db() as c:
            total = c.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
        res = {"seeds": stat["done"], "pages": stat["pages"], "calls": stat["calls"],
               "cost_usd": round(stat["calls"] * 0.001, 3), "added": stat["added"], "pool": total}
        log(f"[mine] {res}")
        return res
    finally:
        mine_lock.release()


def sentinel():
    """读产出最高的哨兵号的第 1 页粉丝：新开蓝V的人最先关注的就是这些互关大号"""
    with db() as c:
        rows = [r for r in c.execute("SELECT user_id, username, name FROM pool WHERE mine_yield>0 ORDER BY mine_yield DESC")
                if is_chinese_name(r["name"] or "")][:SENTINELS]
    if rows:
        log(f"[sentinel] {mine(budget=SENTINELS * 0.001, pages=1, seeds=rows)}")


# ---------- 回关核验 ----------

def verify_followbacks(job, username, following_ids, following_complete):
    """读用户最新的粉丝，核验他在站内点过关注的人有没有回关；顺带把粉丝里的中文蓝V收进池子"""
    now = int(time.time())
    with db() as c:
        pending = {r["to_id"]: r["clicked_at"] for r in c.execute(
            "SELECT to_id, clicked_at FROM clicks WHERE from_user=? AND status='pending' AND clicked_at<?",
            (username, now - 3600))}
    if not pending:
        return
    job.update(phase="核验回关")
    followers, _ = fetch_pages(
        "followers", username, FOLLOWER_PAGES,
        on_page=lambda p, n: job.update(count2=n),
        stop=lambda us: pending.keys() <= {u.get("user_id") for u in us})
    follower_ids = {u.get("user_id") for u in followers}
    with db() as c:
        add_blue_followers(c, followers)
        for to_id, ts in pending.items():
            age_h = (now - ts) / 3600
            if to_id in follower_ids:
                status = "back"
            elif following_complete and to_id not in following_ids and age_h > 24:
                status = "not_followed"     # 点了按钮但实际没关注，不计入
            elif to_id in following_ids and age_h > BACK_WAIT_H:
                status = "no_back"
            else:
                continue
            c.execute("UPDATE clicks SET status=?, resolved_at=? WHERE from_user=? AND to_id=?",
                      (status, now, username, to_id))


# ---------- 拉用户关注列表 ----------

jobs = {}           # job_id -> 状态
user_jobs = {}      # username -> 正在跑的 job_id
jobs_lock = threading.Lock()


def get_cached_following(username):
    with db() as c:
        r = c.execute("SELECT * FROM following_cache WHERE username=?", (username,)).fetchone()
    if r:
        return set(json.loads(r["ids"])), bool(r["complete"]), r["fetched_at"]
    return None


def check_job(job_id, username, known=None):
    """逐页读关注列表，每页的 id 立刻追加到 job["ids"]，前端边读边过滤。
    known 不为空时是增量刷新：关注列表按时间倒序，读到已知的人就停，只补新关注的。"""
    job = jobs[job_id]
    try:
        pages, more, seen = 0, False, set()
        try:
            for page, _ in user_pages("following", username):
                ids = [u["user_id"] for u in page if u.get("user_id") and u["user_id"] not in seen]
                seen.update(ids)
                hit_known = bool(known) and any(i in known for i in ids)
                job["ids"].extend(i for i in ids if not known or i not in known)
                pages += 1
                job["count"] = len(seen)
                if hit_known:
                    break
                if pages >= MAX_FOLLOWING_PAGES:
                    more = True
                    break
        except Exception as e:
            if not seen:
                raise
            log(f"[check] @{username} 读到 {len(seen)} 人后中断: {e}")
            more = True     # 已读到的先用着，标记为没读完
        if not seen and not known:
            raise RuntimeError("找不到这个用户，或者他没有关注任何人 / 关注列表不公开")
        if known:
            all_ids, complete = known | set(job["ids"]), not more
        else:
            all_ids, complete = set(job["ids"]), not more
        with db() as c:
            c.execute("INSERT OR REPLACE INTO following_cache VALUES(?,?,?,?,?)",
                      (username, json.dumps(list(all_ids)), len(all_ids), int(complete), int(time.time())))
        try:
            verify_followbacks(job, username, all_ids, complete)
        except Exception as e:
            log(f"[verify] {username} 失败: {e}")
        job.update(status="done", complete=complete, total=len(all_ids))
    except Exception as e:
        job.update(status="error", error=str(e))
    finally:
        with jobs_lock:
            user_jobs.pop(username.lower(), None)


def build_result(username, following_ids, complete, fetched_at):
    with db() as c:
        rows = c.execute("SELECT * FROM pool").fetchall()
        rates = {r["to_id"]: (r["back"], r["no_back"]) for r in c.execute("""
            SELECT to_id, SUM(status='back') back, SUM(status='no_back') no_back
            FROM clicks GROUP BY to_id""")}
        mine = dict(c.execute("""SELECT status, COUNT(*) FROM clicks WHERE from_user=? GROUP BY status""",
                              (username,)).fetchall())
    items = []
    for r in rows:
        if r["user_id"] in following_ids or (r["username"] or "").lower() == username.lower()                 or (r["followers"] or 0) > MAX_FANS:
            continue
        x = dict(r)
        x["back"], x["no_back"] = rates.get(r["user_id"], (0, 0))
        items.append(x)
    return {
        "username": username,
        "following_count": len(following_ids),
        "following_complete": complete,
        "fetched_at": fetched_at,
        "pool_size": len(rows),
        "my_clicks": mine,
        "items": items,
    }


# ---------- Web ----------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)   # 不公开接口文档
app.add_middleware(GZipMiddleware, minimum_size=2000)
join_hits = defaultdict(list)   # ip -> 时间戳
click_hits = defaultdict(list)  # ip -> 时间戳


def clean_username(s):
    u = s.strip().lstrip("@").rstrip("/").split("/")[-1].split("?")[0]
    if not u or not u.replace("_", "").isalnum() or len(u) > 15:
        raise HTTPException(400, "用户名格式不对")
    return u


class UserReq(BaseModel):
    username: str
    refresh: bool = False


class ClickReq(BaseModel):
    username: str
    to_id: str


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/bm.js")
def bookmarklet():
    """书签源码：页面把它拼成 javascript: 链接；公开出来方便用户自己检查"""
    return FileResponse(BASE / "bm.js", media_type="text/javascript; charset=utf-8")


pool_cache = {"at": 0, "data": None}


@app.get("/api/pool")
def pool_list():
    if time.time() - pool_cache["at"] > 300:
        with db() as c:
            rates = {r["to_id"]: (r["back"], r["no_back"]) for r in c.execute("""
                SELECT to_id, SUM(status='back') back, SUM(status='no_back') no_back FROM clicks GROUP BY to_id""")}
            rows = c.execute("""SELECT user_id, username, name, avatar, followers, last_seen, last_tweet, source
                                FROM pool WHERE COALESCE(followers,0) <= ?""", (MAX_FANS,)).fetchall()
        items = []
        for r in rows:
            x = dict(r)
            x["back"], x["no_back"] = rates.get(r["user_id"], (0, 0))
            items.append(x)
        pool_cache.update(at=time.time(), data={"items": items})
    return pool_cache["data"]


@app.get("/api/stats")
def stats():
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
        joined = c.execute("SELECT COUNT(*) FROM pool WHERE source='self'").fetchone()[0]
    return {"pool_size": n, "joined": joined, "last_collect": int(meta_get("last_collect", 0))}


check_hits = defaultdict(list)   # ip -> 时间戳


def client_ip(request):
    # 只信任本机反向代理（Caddy）给的 X-Forwarded-For：Caddy 会用真实来源 IP 覆盖它，
    # 外部直连时不看任何请求头，避免伪造 IP 绕过限流
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return host


@app.post("/api/check")
def check(req: UserReq, request: Request):
    """立刻返回已知的关注 id（缓存），需要时同时启动后台任务补全，前端用 /api/job 增量拿新 id"""
    username = clean_username(req.username)
    cached = get_cached_following(username)
    known, complete, fetched_at = cached if cached else (set(), False, 0)
    base = {"username": username, "ids": list(known), "complete": complete, "fetched_at": fetched_at}
    if cached and time.time() - fetched_at < 3600:
        return {"status": "done", **base}
    incremental = bool(cached and complete)
    with jobs_lock:
        jid = user_jobs.get(username.lower())
        if not jid:
            if not incremental:   # 增量刷新只读几页，不限流
                if today_spend() >= CHECK_BUDGET_DAY:
                    if cached:
                        return {"status": "done", **base}
                    raise HTTPException(503, "今天查询的人太多啦，额度用完了，明天再来～")
                ip, now = client_ip(request), time.time()
                check_hits[ip] = [t for t in check_hits[ip] if now - t < 86400]
                if len(check_hits[ip]) >= CHECK_PER_IP_DAY:
                    if cached:
                        return {"status": "done", **base}
                    raise HTTPException(429, "今天查询次数用完了，明天再来～")
                check_hits[ip].append(now)
            for k in [k for k, j in jobs.items() if time.time() - j["created"] > 3600]:
                jobs.pop(k, None)
            jid = uuid.uuid4().hex[:12]
            jobs[jid] = {"status": "running", "phase": "读取关注列表", "count": 0, "count2": 0,
                         "ids": [], "incremental": incremental, "created": time.time()}
            user_jobs[username.lower()] = jid
            threading.Thread(target=check_job, args=(jid, username, known if incremental else None),
                             daemon=True).start()
    return {"status": "running", "job_id": jid, "incremental": jobs[jid]["incremental"], **base}


@app.get("/api/job/{job_id}")
def job(job_id: str, since: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return {k: v for k, v in j.items() if k != "ids"} | {"ids": j["ids"][since:], "next": len(j["ids"])}


@app.post("/api/click")
def click(req: ClickReq, request: Request):
    username = clean_username(req.username)
    ip, now = client_ip(request), time.time()
    click_hits[ip] = [t for t in click_hits[ip] if now - t < 86400]
    if len(click_hits[ip]) >= CLICK_PER_IP_DAY:
        raise HTTPException(429, "操作太频繁")
    click_hits[ip].append(now)
    with db() as c:
        if not c.execute("SELECT 1 FROM pool WHERE user_id=?", (req.to_id,)).fetchone():
            raise HTTPException(404, "不在池子里")
        c.execute("INSERT OR IGNORE INTO clicks(from_user, to_id, clicked_at) VALUES(?,?,?)",
                  (username, req.to_id, int(time.time())))
    return {"ok": True}


@app.post("/api/join")
def join(req: UserReq, request: Request):
    if not JOIN_ENABLED:
        raise HTTPException(404, "Not Found")
    username = clean_username(req.username)
    with db() as c:
        r = c.execute("SELECT source FROM pool WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    if r and r["source"] == "self":
        return {"ok": True, "msg": "你已经在榜上了"}
    ip = client_ip(request)
    now = time.time()
    join_hits[ip] = [t for t in join_hits[ip] if now - t < 3600]
    if len(join_hits[ip]) >= JOIN_PER_IP_HOUR:
        raise HTTPException(429, "操作太频繁，过一会再试")
    join_hits[ip].append(now)
    try:
        p = monid("tikhub", "/api/v1/twitter/web/fetch_user_profile", query={"screen_name": username})
    except Exception as e:
        raise HTTPException(502, f"查询失败，稍后再试: {e}")
    uid = p.get("rest_id") or p.get("id")
    if not uid:
        raise HTTPException(404, "找不到这个用户")
    if not p.get("blue_verified"):
        raise HTTPException(400, "只有蓝V账号可以上榜")
    if p.get("protected"):
        raise HTTPException(400, "锁推账号不能上榜，别人关注不了你")
    with db() as c:
        upsert_pool(c, uid, p.get("profile") or username, p.get("name"), p.get("avatar"),
                    p.get("sub_count"), int(now), p.get("desc") or "", source="self")
        c.execute("UPDATE pool SET source='self', joined_at=COALESCE(joined_at, ?) WHERE user_id=?",
                  (int(now), uid))
    return {"ok": True, "msg": "上榜成功！别人查找时你会排在前面"}


@app.post("/api/mine")
def trigger_mine(token: str = "", budget: float = 0.1):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:   # 没设口令时禁用，防止公网上被人刷钱
        raise HTTPException(403, "口令错误")
    threading.Thread(target=mine, args=(budget,), daemon=True).start()
    return {"status": "started"}


@app.post("/api/collect")
def trigger_collect(token: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:   # 没设口令时禁用，防止公网上被人刷钱
        raise HTTPException(403, "口令错误")
    threading.Thread(target=collect, daemon=True).start()
    return {"status": "started"}


if __name__ == "__main__":
    import sys
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "mine":
        # python app.py mine [预算美元]
        mine(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
        sys.exit()
    if len(sys.argv) > 1 and sys.argv[1] == "sentinel":
        sentinel()
        sys.exit()
    if len(sys.argv) > 1 and sys.argv[1] == "addx":
        # python app.py addx 小号用户名 "auth_token=xxx; ct0=yyy"
        xs.add_account(sys.argv[2], sys.argv[3])
        print(xs.accounts())
        sys.exit()
    if len(sys.argv) > 1 and sys.argv[1] == "xstatus":
        for a in xs.accounts():
            print(a)
        sys.exit()
    if COLLECT_INTERVAL_MIN > 0:
        threading.Thread(target=collect_loop, daemon=True).start()
    if xs is not None and FREE_MINE_INTERVAL_MIN > 0:
        threading.Thread(target=free_mine_loop, daemon=True).start()
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=PORT)
