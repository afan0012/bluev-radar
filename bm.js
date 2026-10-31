/* 蓝V互关雷达 · 书签
   在 x.com 你的「正在关注」页上运行：自动往下滚，记下你关注的每个人，读完跳回 bluefriends.heyafan.com。
   只读取关注列表，不会关注、发推、点赞或读私信，也不会把数据发到任何服务器（结果放在网址 # 后面，只有你的浏览器能看到）。 */
(() => {
  const SITE = 'https://bluefriends.heyafan.com/';
  if (!/(^|\.)(x|twitter)\.com$/.test(location.hostname)) {
    alert('请先打开 x.com 并登录，再点这个书签');
    return;
  }
  if (window.__bfRunning) return;
  // 电脑版从侧栏头像链接拿用户名；手机版没有这个链接，就用当前「xxx/following」页的用户名
  const link = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  const m = location.pathname.match(/^\/([A-Za-z0-9_]{1,15})\/following\/?$/);
  const me = (link && link.getAttribute('href').split('/')[1]) || (m && m[1]);
  if (!me) {
    alert('请先登录 x.com，打开你的主页 →「正在关注」，再点这个书签');
    return;
  }
  const path = '/' + me + '/following';
  if (location.pathname.toLowerCase() !== path.toLowerCase()) {
    alert('先带你去「正在关注」页，页面加载出来后再点一次书签');
    location.href = 'https://x.com' + path;
    return;
  }
  window.__bfRunning = true;

  const ids = new Set(), names = new Set(), self = me.toLowerCase();

  // 1) 拦截 X 自己加载关注列表的请求，从返回里拿用户 id
  const eat = obj => {
    const walk = o => {
      if (!o || typeof o !== 'object') return;
      if (o.__typename === 'User' && o.rest_id) {
        const sn = (o.core && o.core.screen_name) || (o.legacy && o.legacy.screen_name) || '';
        if (sn.toLowerCase() !== self) {
          ids.add(o.rest_id);
          if (sn) names.add(sn.toLowerCase());
        }
      }
      for (const k in o) walk(o[k]);
    };
    walk(obj);
  };
  const isFollowing = u => /\/graphql\/[^/]+\/Following\b/.test(String(u));
  const xo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    if (isFollowing(u)) {
      this.addEventListener('load', () => {
        try { eat(this.responseType === 'json' ? this.response : JSON.parse(this.responseText)); } catch (e) {}
      });
    }
    return xo.apply(this, arguments);
  };
  const f0 = window.fetch;
  window.fetch = function (input) {
    const p = f0.apply(this, arguments);
    if (isFollowing(typeof input === 'string' ? input : input && input.url)) {
      p.then(r => r.clone().json()).then(eat).catch(() => {});
    }
    return p;
  };

  // 2) 同时扫页面上的用户卡片拿用户名（拦截不到时兜底）。只扫中间主栏，右边“推荐关注”不算
  const scan = () => {
    const col = document.querySelector('[data-testid="primaryColumn"]') || document;
    col.querySelectorAll('[data-testid="UserCell"]').forEach(c => {
      const a = c.querySelector('a[role="link"][href^="/"]');
      const sn = a && a.getAttribute('href').split('/')[1];
      if (sn && sn.toLowerCase() !== self) names.add(sn.toLowerCase());
    });
  };

  // 进度浮层
  const box = document.createElement('div');
  Object.assign(box.style, {
    position: 'fixed', top: '16px', right: '16px', zIndex: 99999, background: '#000', color: '#e7e9ea',
    border: '1px solid #1d9bf0', borderRadius: '16px', padding: '14px 18px', font: '15px/1.6 sans-serif',
    boxShadow: '0 4px 24px #1d9bf055', maxWidth: '300px'
  });
  const txt = document.createElement('div');
  const stop = document.createElement('button');
  stop.textContent = '停止，用已读到的结果';
  Object.assign(stop.style, {
    marginTop: '8px', background: '#1d9bf0', color: '#fff', border: '0', borderRadius: '999px',
    padding: '6px 14px', cursor: 'pointer', font: 'bold 14px sans-serif'
  });
  box.append(txt, stop);
  document.body.append(box);
  let stopped = false;
  stop.onclick = () => { stopped = true; };

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const count = () => Math.max(ids.size, names.size);

  (async () => {
    scrollTo(0, 0);
    await sleep(500);
    let lastH = 0, idle = 0, done = false;
    while (!stopped) {
      scan();
      txt.textContent = '蓝V互关雷达：正在读取你的关注列表… 已读 ' + count() + ' 人（请别关闭这个页面）';
      const atBottom = scrollY + innerHeight >= document.documentElement.scrollHeight - 200;
      if (!atBottom) {
        scrollBy(0, innerHeight * 1.5);
        await sleep(250);
        continue;
      }
      // 到底了：等 X 加载下一页；X 偶尔报错会出现“重试”按钮，帮忙点一下
      const h = document.documentElement.scrollHeight;
      if (h > lastH) { lastH = h; idle = 0; } else idle++;
      document.querySelectorAll('[role="button"]').forEach(b => {
        if (/^(重试|Retry)$/i.test(b.textContent.trim())) b.click();
      });
      if (idle >= 20) { done = true; break; }   // 连续 10 秒没有新内容 = 读完了
      scrollBy(0, -300);
      await sleep(100);
      scrollTo(0, document.documentElement.scrollHeight);
      await sleep(400);
    }
    scan();
    const data = { u: me, i: [...ids], n: [...names], d: done };
    txt.textContent = '读完了，共 ' + count() + ' 人，正在跳回蓝V互关雷达…';
    location.href = SITE + '#bm=' + encodeURIComponent(JSON.stringify(data));
  })();
})();
