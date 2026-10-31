# 蓝V互关雷达

列出你**还没关注**的中文蓝V：中文蓝V池 − 你已关注的人。

- **建池子**：定时用 monid `anyapi /twitter.search` 搜 `互关 / 浇水 / 蓝V …` + `lang:zh filter:blue_verified`，把作者存进 SQLite。
- **滚雪球扩池**：读池子里蓝V的粉丝，把“蓝V + 昵称/简介含中文”的人收进池子，被收进来的人以后也会当种子。每个种子只读一次。
- **查未关注**：用户输入用户名，用 monid `tikhub fetch_user_followings` 拉关注列表（50 人/页），从池子里减掉。
- **关注**：用户在页面上点按钮，打开对方主页后手动关注。不做自动关注，避免封号。

## 本地运行

```bash
pip install -r requirements.txt
python app.py          # 打开 http://localhost:8000
```

需要 `monid` 在 PATH 里，并配置好 `MONID_API_KEY`。

## 扩池

```bash
python app.py mine 1     # 滚雪球扩池，最多花 $1
python app.py sentinel   # 读哨兵号第 1 页抓新蓝V（服务运行时每天自动跑一次）
```

实测：5 个种子 × 5 页，花了 $0.025，新增 421 个中文蓝V（命中率约 45%）。
服务运行时也可以调 `POST /api/mine?budget=1&token=口令`（必须设置 `ADMIN_TOKEN`，否则接口禁用）。

## 书签（电脑用户）

`bm.js` 是书签源码，页面把它拼成 `javascript:` 链接让用户拖进收藏栏。用户在 x.com 自己的「正在关注」页点书签，
它自动往下滚，一边拦截 X 自己的 Following 请求拿用户 id，一边扫页面卡片拿用户名，读完跳回本站，结果放在 `#bm=` 后面，
不经过服务器。自己部署时，把 `bm.js` 里的 `SITE` 改成你的域名。完全免费，用的是用户自己的登录状态。X 改版后如果失效，优先检查 `UserCell`、`primaryColumn`、
`AppTabBar_Profile_Link` 这几个 `data-testid` 是否还在。

## 免费数据源（小号）

配置了 X 小号的 cookie 后，读关注列表、读粉丝、扩池、关键词搜索都会先用小号免费调 X 网页版接口（twscrape），
小号被限流或被锁时自动退回 monid。扩池只读“蓝V粉丝”接口，每页 100 人，后台每 20 分钟跑一轮，直到限流为止。

```bash
python app.py addx 小号用户名 "auth_token=xxx; ct0=yyy"   # 添加 / 更新小号
python app.py xstatus                                      # 查看小号状态
```

不要用主号。账号保存在 `accounts.db`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KEYWORDS` | 互关,浇水,浇朋友,蓝V,有关必回,回关,新人,互粉 | 采集用的关键词 |
| `COLLECT_PAGES` | 1 | 每个关键词翻几页（约 $0.002/页） |
| `COLLECT_INTERVAL_MIN` | 180 | 采集间隔（分钟），0=关闭 |
| `MAX_FOLLOWING_PAGES` | 100 | 关注列表最多翻几页（$0.001/页，100 页=5000 人） |
| `FOLLOWING_CACHE_H` | 24 | 关注列表缓存多少小时 |
| `ADMIN_TOKEN` | 空 | 手动采集 `POST /api/collect?token=` 的口令 |
| `MINE_PAGES` | 10 | 每个种子最多读几页粉丝 |
| `MINE_MIN_NEW` | 8 | 某页新增少于这个数就换下一个种子 |
| `SENTINELS` | 30 | 哨兵号数量，每天各读 1 页抓新蓝V |
| `CHECK_PER_IP_DAY` | 5 | 每个 IP 每天最多查询几次（命中缓存不算） |
| `CHECK_BUDGET_DAY` | 1.0 | 用户查询每天最多花多少美元 |
| `DAILY_HARD_CAP` | 3.0 | 所有 monid 花费每天的硬上限（美元），到了就不再付费读 |
| `CLICK_PER_IP_DAY` | 600 | 每个 IP 每天最多记录几次点击 |
| `JOIN_ENABLED` | 空 | 设为 1 才开放“我要上榜”接口 |
| `HOST` | 0.0.0.0 | 监听地址，放在反向代理后面时设为 127.0.0.1（真实 IP 从 X-Forwarded-For 取） |
| `PORT` | 8000 | 端口 |

## 成本

- 采集：默认每小时 16 次调用，约 $0.03，一天约 $0.8。想省钱就调大 `COLLECT_INTERVAL_MIN`。
- 每次查询：关注 1000 人约 $0.02，有缓存时为 0。

## 部署到 VPS（Ubuntu）

```bash
# 装 node + monid + python 依赖
npm i -g @monid-ai/cli && export MONID_API_KEY=xxx
pip install -r requirements.txt
ADMIN_TOKEN=换成你的口令 nohup python app.py > server.log 2>&1 &
```

前面再套一层 Caddy，自动配 HTTPS：`your.domain { reverse_proxy localhost:8000 }`

## License

MIT
