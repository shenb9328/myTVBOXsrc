# -*- coding: utf-8 -*-
"""
CCTV-6 往期电影与时移回看核心驱动模块
支持：
1. 咪咕公开 EPG 节目单抓取与清洗（过滤短插播）
2. 跨午夜零点时移时间戳精准换算（标准 UTC PLTV 格式）
3. TVBox VOD 协议接口响应生成
4. 带有 Catchup 时移标签的 M3U 源生成
5. 飞牛影视（trim.media）标准 .strm 虚拟文件库同步
6. 专为 iPad/触屏/电脑优化的高颜值 Web 点播台
"""

import os
import sys
import json
import time
import urllib.request
import threading
from datetime import datetime, timezone, timedelta

MEDIA_DIR = "/vol1/1001/myMedia/CCTV6往期电影"

BJ_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

CACHE = {}
CACHE_LOCK = threading.Lock()
LAST_FETCH_TIME = {}

def get_cctv6_movies(date_str):
    """获取指定日期 (YYYYMMDD) 的 CCTV-6 电影列表，并计算 PLTV 播放地址"""
    now = time.time()
    with CACHE_LOCK:
        if date_str in CACHE and (now - LAST_FETCH_TIME.get(date_str, 0)) < 3600:
            return CACHE[date_str]

    url = f"http://live.miguvideo.com/live/v2/tv-programs-data/624878396/{date_str}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"[CCTV6] Fetch EPG error for {date_str}: {e}")
        with CACHE_LOCK:
            return CACHE.get(date_str, [])

    progs = []
    def extract(obj):
        if isinstance(obj, dict):
            if 'contName' in obj:
                progs.append(obj)
            for v in obj.values(): extract(v)
        elif isinstance(obj, list):
            for i in obj: extract(i)
    extract(data)

    movies = []
    seen = set()
    for p in progs:
        name = p.get('contName', '').strip()
        start_ms = p.get('startTime', 0)
        end_ms = p.get('endTime', 0)
        if not start_ms or not end_ms or not name:
            continue
        
        # 过滤极短插播（如彩票开奖、微短播报等 < 15分钟）
        duration_min = int((end_ms - start_ms) / 1000 / 60)
        if duration_min < 15:
            continue

        unique_key = f"{name}_{start_ms}"
        if unique_key in seen:
            continue
        seen.add(unique_key)

        dt_start = datetime.fromtimestamp(start_ms / 1000, tz=BJ_TZ)
        dt_end = datetime.fromtimestamp(end_ms / 1000, tz=BJ_TZ)

        # 换算为标准 UTC 时间供 PLTV 时移协议使用 (解决跨午夜截断核心)
        dt_start_utc = dt_start.astimezone(UTC_TZ)
        dt_end_utc = dt_end.astimezone(UTC_TZ)
        start_pltv = dt_start_utc.strftime('%Y%m%dT%H%M%S.00Z')
        end_pltv = dt_end_utc.strftime('%Y%m%dT%H%M%S.00Z')

        play_url = f"http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225814/index.m3u8?playtype=1&starttime={start_pltv}&endtime={end_pltv}"
        pic = "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=500&auto=format&fit=crop&q=60"
        
        movies.append({
            "id": f"{date_str}_{start_ms}",
            "name": name,
            "date": date_str,
            "start_time": dt_start.strftime("%H:%M"),
            "end_time": dt_end.strftime("%H:%M" if dt_start.date() == dt_end.date() else "次日 %H:%M"),
            "full_time": f"{dt_start.strftime('%m-%d %H:%M')} ~ {dt_end.strftime('%H:%M' if dt_start.date() == dt_end.date() else '%m-%d %H:%M')}",
            "duration": f"{duration_min} 分钟",
            "duration_min": duration_min,
            "is_cross_day": dt_start.date() != dt_end.date(),
            "play_url": play_url,
            "pic": pic,
            "start_ts": start_ms,
            "end_ts": end_ms
        })

    with CACHE_LOCK:
        CACHE[date_str] = movies
        LAST_FETCH_TIME[date_str] = now

    return movies

def get_recent_days(days=7):
    """获取最近 N 天的日期列表 (从今天往回推)"""
    today = datetime.now(BJ_TZ).date()
    res = []
    labels = ["今天", "昨天", "前天", "3天前", "4天前", "5天前", "6天前", "7天前"]
    for i in range(days):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y%m%d")
        lbl = labels[i] if i < len(labels) else f"{i}天前"
        res.append({
            "date": d_str,
            "label": f"{lbl} ({d.strftime('%m/%d')})",
            "short_label": lbl
        })
    return res

def sync_strm_files():
    """将最近 5 天的电影自动生成为 .strm 虚拟文件供飞牛影视刮削"""
    if not os.path.exists(MEDIA_DIR):
        try:
            os.makedirs(MEDIA_DIR, exist_ok=True)
        except Exception as e:
            print(f"[CCTV6] Cannot create media dir: {e}")
            return 0

    count = 0
    recent = get_recent_days(5)
    for day in recent:
        d_str = day["date"]
        movies = get_cctv6_movies(d_str)
        day_dir = os.path.join(MEDIA_DIR, f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]}")
        os.makedirs(day_dir, exist_ok=True)
        for m in movies:
            clean_name = m["name"].replace("/", "_").replace("\\", "_").replace(":", "：").replace("*", "")
            strm_path = os.path.join(day_dir, f"{clean_name}.strm")
            try:
                with open(strm_path, "w", encoding="utf-8") as f:
                    f.write(m["play_url"].strip() + "\n")
                count += 1
            except Exception as e:
                print(f"[CCTV6] Write strm error: {e}")
    try:
        os.system(f"chmod -R 777 '{MEDIA_DIR}' 2>/dev/null")
    except:
        pass
    print(f"[CCTV6] [{datetime.now()}] Synced {count} .strm movie files to {MEDIA_DIR}")
    return count

def build_tvbox_response(qs):
    """TVBox json/spider 协议实现"""
    t = qs.get("t", [""])[0]
    ids = qs.get("ids", [""])[0]

    recent_days = get_recent_days(7)

    # 详情/播放播放地址
    if ids:
        parts = ids.split("_")
        date_str = parts[0]
        movies = get_cctv6_movies(date_str)
        target = next((m for m in movies if m["id"] == ids), None)
        if not target:
            return {"list": []}
        
        detail = {
            "vod_id": target["id"],
            "vod_name": target["name"],
            "vod_pic": target["pic"],
            "type_name": "CCTV-6往期",
            "vod_year": target["date"][:4],
            "vod_area": "中国大陆",
            "vod_remarks": target["duration"],
            "vod_content": f"播出时间: {target['full_time']}。本片为 CCTV-6 高清回放，无缝支持跨午夜播放。",
            "vod_play_from": "CCTV-6高清",
            "vod_play_url": f"全片正片${target['play_url']}"
        }
        return {"list": [detail]}

    # 分类列表
    classes = [{"type_id": d["date"], "type_name": d["label"]} for d in recent_days]

    if not t:
        t = recent_days[0]["date"]

    movies = get_cctv6_movies(t)
    vod_list = []
    for m in movies:
        vod_list.append({
            "vod_id": m["id"],
            "vod_name": m["name"],
            "vod_pic": m["pic"],
            "vod_remarks": f"{m['full_time']} ({m['duration']})"
        })

    return {
        "class": classes,
        "list": vod_list,
        "page": 1,
        "pagecount": 1,
        "limit": len(vod_list),
        "total": len(vod_list)
    }

def build_m3u_content():
    """生成带标准 Catchup 时移标签的 M3U8 文件"""
    m3u = [
        '#EXTM3U x-tvg-url="https://live.fanmingming.com/e.xml"',
        '',
        '# CCTV-6 直播 (支持全天 EPG 回看)',
        '#EXTINF:-1 tvg-name="CCTV-6" tvg-id="cctv6" catchup="append" catchup-source="?playtype=1&starttime=${(b)yyyyMMddTHHmmss.00Z}&endtime=${(e)yyyyMMddTHHmmss.00Z}" group-title="央视频道",CCTV-6 电影 (时移回放)',
        'http://ottrrs.hl.chinamobile.com/PLTV/88888888/224/3221225814/index.m3u8',
        ''
    ]

    for day in get_recent_days(3):
        movies = get_cctv6_movies(day["date"])
        for m in movies:
            m3u.append(f'#EXTINF:-1 tvg-name="CCTV-6" group-title="CCTV6往期-{day["short_label"]}",{m["name"]} ({m["full_time"]})')
            m3u.append(m["play_url"])

    return "\n".join(m3u)

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>CCTV-6 往期电影点播台</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/plyr@3.7.8/dist/plyr.css" />
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/plyr@3.7.8/dist/plyr.polyfilled.min.js"></script>
    <style>
        :root {
            --bg-color: #0f1115;
            --card-bg: rgba(26, 29, 36, 0.85);
            --card-border: rgba(255, 255, 255, 0.08);
            --accent: #ff4757;
            --accent-gradient: linear-gradient(135deg, #ff4757, #ff6b81);
            --text-main: #f1f2f6;
            --text-sub: #a4b0be;
            --tag-bg: rgba(255, 71, 87, 0.15);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
            min-height: 100vh;
        }
        header {
            padding: 20px 20px 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--card-border);
            backdrop-filter: blur(20px);
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(15, 17, 21, 0.85);
        }
        .logo-box h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
        .logo-box h1 span { color: var(--accent); }
        .logo-box p { font-size: 12px; color: var(--text-sub); margin-top: 2px; }
        .nav-links { display: flex; gap: 8px; }
        .tvbox-btn {
            font-size: 13px;
            background: rgba(255, 255, 255, 0.1);
            color: #fff;
            padding: 7px 12px;
            border-radius: 20px;
            text-decoration: none;
            border: 1px solid var(--card-border);
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }

        .date-tabs-wrapper {
            overflow-x: auto;
            white-space: nowrap;
            padding: 16px 20px;
            scrollbar-width: none;
        }
        .date-tabs-wrapper::-webkit-scrollbar { display: none; }
        .date-tabs { display: inline-flex; gap: 10px; }
        .date-tab {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            color: var(--text-sub);
            padding: 10px 18px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .date-tab.active {
            background: var(--accent-gradient);
            color: #fff;
            border-color: transparent;
            box-shadow: 0 4px 15px rgba(255, 71, 87, 0.35);
        }

        .container { padding: 0 20px 40px; }
        .movie-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 16px;
        }
        .movie-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.25s ease;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .movie-card:hover {
            transform: translateY(-3px);
            border-color: rgba(255, 71, 87, 0.5);
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
        }
        .movie-title {
            font-size: 17px;
            font-weight: 600;
            margin-bottom: 8px;
            line-height: 1.35;
        }
        .movie-meta {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 13px;
            color: var(--text-sub);
            margin-bottom: 12px;
        }
        .tag-cross {
            background: rgba(255, 159, 26, 0.2);
            color: #ff9f1a;
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 6px;
            font-weight: 600;
        }
        .card-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-top: 10px;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            font-size: 12px;
            color: var(--text-sub);
        }
        .play-now {
            background: var(--accent);
            color: #fff;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }

        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(25px);
            z-index: 1000;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .modal.open { display: flex; }
        .modal-content {
            background: #14171d;
            width: 100%;
            max-width: 900px;
            border-radius: 20px;
            overflow: hidden;
            border: 1px solid var(--card-border);
            box-shadow: 0 20px 50px rgba(0,0,0,0.8);
        }
        .modal-header {
            padding: 16px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--card-border);
        }
        .modal-title { font-size: 17px; font-weight: 600; }
        .modal-close {
            background: rgba(255,255,255,0.1);
            border: none;
            color: #fff;
            width: 32px; height: 32px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .player-box {
            position: relative;
            background: #000;
            width: 100%;
            aspect-ratio: 16 / 9;
        }
        .player-box video { width: 100%; height: 100%; }

        .empty-tip {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-sub);
            font-size: 15px;
        }
    </style>
</head>
<body>
    <header>
        <div class="logo-box">
            <h1>CCTV-6 <span>电影点播</span></h1>
            <p>全天候时移 · 彻底解决跨零点截断</p>
        </div>
        <div class="nav-links">
            <a href="/" class="tvbox-btn">🏠 影库大盘</a>
            <a href="/live.m3u" class="tvbox-btn">📺 M3U订阅</a>
        </div>
    </header>

    <div class="date-tabs-wrapper">
        <div class="date-tabs" id="dateTabs"></div>
    </div>

    <div class="container">
        <div class="movie-grid" id="movieGrid">
            <div class="empty-tip">正在加载往期电影节目...</div>
        </div>
    </div>

    <!-- 播放器弹窗 -->
    <div class="modal" id="playModal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="modal-title" id="modalTitle">正在播放</div>
                <button class="modal-close" onclick="closeModal()">✕</button>
            </div>
            <div class="player-box">
                <video id="player" playsinline controls></video>
            </div>
        </div>
    </div>

    <script>
        let currentHls = null;
        let plyrPlayer = null;
        let activeDate = "";

        async function init() {
            const video = document.getElementById('player');
            plyrPlayer = new Plyr(video, {
                controls: ['play-large', 'play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'fullscreen', 'airplay', 'pip']
            });

            const res = await fetch('/api/cctv6/days');
            const days = await res.json();
            const tabsEl = document.getElementById('dateTabs');
            tabsEl.innerHTML = '';
            
            days.forEach((d, idx) => {
                const btn = document.createElement('div');
                btn.className = 'date-tab' + (idx === 0 ? ' active' : '');
                btn.innerText = d.label;
                btn.onclick = () => selectDate(d.date, btn);
                tabsEl.appendChild(btn);
            });

            if (days.length > 0) {
                selectDate(days[0].date, tabsEl.firstChild);
            }
        }

        async function selectDate(dateStr, btnEl) {
            activeDate = dateStr;
            document.querySelectorAll('.date-tab').forEach(el => el.classList.remove('active'));
            if (btnEl) btnEl.classList.add('active');

            const grid = document.getElementById('movieGrid');
            grid.innerHTML = '<div class="empty-tip">正在加载此日电影清单...</div>';

            const res = await fetch('/api/cctv6/movies?date=' + dateStr);
            const movies = await res.json();

            if (!movies || movies.length === 0) {
                grid.innerHTML = '<div class="empty-tip">该日期暂无电影记录</div>';
                return;
            }

            grid.innerHTML = '';
            movies.forEach(m => {
                const card = document.createElement('div');
                card.className = 'movie-card';
                card.innerHTML = `
                    <div>
                        <div class="movie-title">${m.name}</div>
                        <div class="movie-meta">
                            <span>🕒 ${m.full_time}</span>
                            ${m.is_cross_day ? '<span class="tag-cross">跨午夜</span>' : ''}
                        </div>
                    </div>
                    <div class="card-footer">
                        <span>片长: ${m.duration}</span>
                        <div class="play-now">▶ 播放</div>
                    </div>
                `;
                card.onclick = () => playMovie(m);
                grid.appendChild(card);
            });
        }

        function playMovie(movie) {
            document.getElementById('modalTitle').innerText = movie.name + ' (' + movie.full_time + ')';
            const modal = document.getElementById('playModal');
            modal.classList.add('open');

            const video = document.getElementById('player');
            const url = movie.play_url;

            if (currentHls) {
                currentHls.destroy();
                currentHls = null;
            }

            if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = url;
                video.play();
            } else if (Hls.isSupported()) {
                currentHls = new Hls();
                currentHls.loadSource(url);
                currentHls.attachMedia(video);
                currentHls.on(Hls.Events.MANIFEST_PARSED, function() {
                    video.play();
                });
            } else {
                video.src = url;
                video.play();
            }
        }

        function closeModal() {
            const modal = document.getElementById('playModal');
            modal.classList.remove('open');
            const video = document.getElementById('player');
            video.pause();
            if (currentHls) {
                currentHls.destroy();
                currentHls = null;
            }
        }

        window.onload = init;
    </script>
</body>
</html>
"""
