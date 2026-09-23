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
import socket
import urllib.request
import threading
from datetime import datetime, timezone, timedelta

def get_lan_ip():
    """获取本机的真实局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '192.168.0.120'

LAN_IP = get_lan_ip()
PORT = 5888

MEDIA_DIR = "/vol1/1001/myMedia/CCTV6往期电影"

BJ_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

CACHE = {}
CACHE_LOCK = threading.Lock()
LAST_FETCH_TIME = {}

MIGU_BASE_URL = "http://hlsztemgsplive.miguvideo.com:8080/wd_r2/2018/ocn/cctv6hd/1000/index.m3u8?msisdn=2026092310030269189d86f8e04f61948b3d1bb604d588&mdspid=&spid=699004&netType=0&sid=5500212872&pid=2028597139&timestamp=20260923100302&Channel_ID=0116_2600000900-99000-201600010010027&ProgramID=624878396&ParentNodeID=-99&assertID=5500212872&client_ip=171.8.79.254&SecurityKey=20260923100302&promotionId=&mvid=5100001694&mcid=500020&playurlVersion=ZQ-A1-9.9.2-SNAPSHOT&userid=&jmhm=&videocodec=h264&appCode=miguvideo_android&bean=mgspad&tid=android&conFee=0&encrypt=43dccd3b779d155c0c649d24e6ba6c98"

def is_migu_url_alive(url):
    """毫秒级验证咪咕直播/时移基准地址是否有效（返回 200 为有效，返回 605/403/超时为失效）"""
    if not url or not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False

def get_migu_base_url(force_refresh=False):
    """自动获取、自检并自愈更新咪咕 CCTV-6 动态签名流地址"""
    global MIGU_BASE_URL
    import re
    cache_file = os.path.join(os.path.dirname(__file__), "migu_base_url.cache")

    # 1. 优先复用当前内存中或磁盘缓存中的 URL（需经过存活校验）
    if not force_refresh:
        if MIGU_BASE_URL and is_migu_url_alive(MIGU_BASE_URL):
            return MIGU_BASE_URL
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    saved = f.read().strip()
                if saved.startswith("http") and is_migu_url_alive(saved):
                    MIGU_BASE_URL = saved
                    return MIGU_BASE_URL
            except Exception:
                pass

    # 2. 缓存失效或强制刷新时，从多镜像订阅源拉取最新候选地址
    urls = [
        "https://ghfast.top/raw.githubusercontent.com/Supprise0901/TVBox_live/main/live.txt",
        "https://raw.githubusercontent.com/Supprise0901/TVBox_live/main/live.txt",
        "https://ghfast.top/raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
        "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u"
    ]
    candidates = []
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode("utf-8")
                for line in text.splitlines():
                    if ("CCTV6" in line or "CCTV-6" in line) and "miguvideo.com" in line:
                        parts = line.split(",")
                        for p in parts:
                            p = p.strip()
                            if p.startswith("http") and "miguvideo.com" in p and p not in candidates:
                                candidates.append(p)
            if candidates:
                break
        except Exception:
            pass

    # 3. 按时间戳倒序排列，优先检测最新生成的 token 候选
    def extract_ts(c_url):
        m = re.search(r'timestamp=(\d+)', c_url)
        return m.group(1) if m else ''
    candidates.sort(key=extract_ts, reverse=True)

    # 4. 逐个验证存活状态，命中首个 200 即落盘缓存并返回
    for cand in candidates:
        if is_migu_url_alive(cand):
            MIGU_BASE_URL = cand
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    f.write(MIGU_BASE_URL)
            except Exception:
                pass
            return MIGU_BASE_URL

    return MIGU_BASE_URL

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

        # 换算为时间戳格式供咪咕时移使用 (YYYYMMDDHHMMSS)
        start_migu = dt_start.strftime('%Y%m%d%H%M%S')
        end_migu = dt_end.strftime('%Y%m%d%H%M%S')

        # 换算为标准 UTC 时间供 PLTV 时移协议使用 (作为跨网络备用线路)
        dt_start_utc = dt_start.astimezone(UTC_TZ)
        dt_end_utc = dt_end.astimezone(UTC_TZ)
        start_pltv = dt_start_utc.strftime('%Y%m%dT%H%M%S.00Z')
        end_pltv = dt_end_utc.strftime('%Y%m%dT%H%M%S.00Z')

        migu_play_url = f"http://{LAN_IP}:{PORT}/api/cctv6/play.m3u8?begin={start_migu}&end={end_migu}"
        proxy_play_url = f"http://{LAN_IP}:{PORT}/api/cctv6/play.m3u8?begin={start_migu}&end={end_migu}&proxy=1"
        pltv_play_url = proxy_play_url
        
        play_url = migu_play_url
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
            "migu_url": migu_play_url,
            "pltv_url": pltv_play_url,
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

def get_all_recent_movies(days=7):
    """获取近 N 天的所有已播出电影，按播出时间倒序排列"""
    all_movies = []
    now_ms = int(time.time() * 1000)
    for d in get_recent_days(days):
        movies = get_cctv6_movies(d["date"])
        # 只保留已经播出或正在播出的电影 (start_ts <= 当前时间)
        broadcasted = [m for m in movies if m.get("start_ts", 0) <= now_ms]
        all_movies.extend(broadcasted)
    # 按 start_ts 倒序（最新的电影排最前）
    all_movies.sort(key=lambda x: x.get("start_ts", 0), reverse=True)
    return all_movies

def query_cctv6_for_super_vod(page=1, pagesize=30, class_kw="", keyword=""):
    """供超级私有影库 /api/vod 调用的查询与筛选函数"""
    movies = get_all_recent_movies(7)
    
    # 关键词搜索
    if keyword:
        kw = keyword.strip().lower()
        movies = [m for m in movies if kw in m["name"].lower()]

    # 日期多维筛选 (class_kw: 如 "今天", "昨天", "前天", "09/19" 或 "20260919")
    elif class_kw:
        ckw = class_kw.strip()
        recent = get_recent_days(7)
        # 查找对应的 date_str
        target_date = ""
        for r in recent:
            if ckw in r["label"] or ckw in r["short_label"] or ckw == r["date"]:
                target_date = r["date"]
                break
        if target_date:
            movies = [m for m in movies if m["date"] == target_date]
        else:
            movies = [m for m in movies if ckw in m["date"] or ckw in m["full_time"]]

    total = len(movies)
    start_idx = (page - 1) * pagesize
    end_idx = start_idx + pagesize
    page_items = movies[start_idx:end_idx]

    vod_list = []
    for m in page_items:
        migu_u = m.get("migu_url") or m["play_url"]
        pltv_u = m.get("pltv_url") or m["play_url"]
        vod_list.append({
            "vod_id": f"cctv6_{m['id']}",
            "vod_name": m["name"],
            "type_id": 6,
            "type_name": "CCTV6",
            "vod_pic": m["pic"],
            "vod_remarks": f"{m['full_time']} · {m['duration']}",
            "vod_year": m["date"][:4],
            "vod_area": "中国大陆",
            "vod_actor": "CCTV-6高清重温",
            "vod_director": "中央广播电视总台",
            "vod_content": f"播出时间: {m['full_time']}。本片为 CCTV-6 高清回放，无缝支持跨午夜播放。",
            "vod_play_from": "咪咕高清时移$$$移动OTT时移",
            "vod_play_url": f"全片正片${migu_u}$$$全片正片${pltv_u}"
        })

    import math
    pagecount = max(1, math.ceil(total / pagesize)) if pagesize else 1

    return {
        "page": page,
        "pagecount": pagecount,
        "limit": pagesize,
        "total": total,
        "list": vod_list
    }

def get_cctv6_detail_for_super_vod(vod_id):
    """根据 vod_id (如 cctv6_20260918_1789743660000) 返回单片完整详情与线路"""
    raw_id = vod_id.replace("cctv6_", "")
    parts = raw_id.split("_")
    date_str = parts[0]
    movies = get_cctv6_movies(date_str)
    target = next((m for m in movies if m["id"] == raw_id), None)
    if not target:
        # 如果找不到，从所有缓存中搜一遍
        all_m = get_all_recent_movies(7)
        target = next((m for m in all_m if m["id"] == raw_id), None)
    
    if not target:
        return None

    migu_u = target.get("migu_url") or target["play_url"]
    proxy_u = target.get("pltv_url") or f"{migu_u}&proxy=1"

    return {
        "vod_id": vod_id,
        "vod_name": target["name"],
        "type_id": 6,
        "type_name": "CCTV6",
        "vod_pic": target["pic"],
        "vod_remarks": f"{target['full_time']} · {target['duration']}",
        "vod_year": target["date"][:4],
        "vod_area": "中国大陆",
        "vod_actor": "CCTV-6高清重温",
        "vod_director": "中央广播电视总台",
        "vod_content": f"播出时间: {target['full_time']}。本片为 CCTV-6 高清回放，无缝支持跨午夜播放。",
        "vod_play_from": "咪咕超清时移$$$咪咕代理加速",
        "vod_play_url": f"全片正片${migu_u}$$$全片正片${proxy_u}"
    }

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
        
        migu_u = target.get("migu_url") or target["play_url"]
        proxy_u = target.get("pltv_url") or f"{migu_u}&proxy=1"
        detail = {
            "vod_id": target["id"],
            "vod_name": target["name"],
            "vod_pic": target["pic"],
            "type_name": "CCTV-6往期",
            "vod_year": target["date"][:4],
            "vod_area": "中国大陆",
            "vod_remarks": target["duration"],
            "vod_content": f"播出时间: {target['full_time']}。本片为 CCTV-6 高清回放，无缝支持跨午夜播放。",
            "vod_play_from": "咪咕超清时移$$$咪咕代理加速",
            "vod_play_url": f"全片正片${migu_u}$$$全片正片${proxy_u}"
        }
        return {"list": [detail]}

    # 分类列表
    classes = [{"type_id": d["date"], "type_name": d["label"]} for d in recent_days]

    if not t:
        t = recent_days[0]["date"]

    movies = get_cctv6_movies(t)
    now_ms = int(time.time() * 1000)
    # 过滤掉未来未播出的场次
    movies = [m for m in movies if m.get("start_ts", 0) <= now_ms]
    # 按播出时间倒序排列
    movies.sort(key=lambda x: x.get("start_ts", 0), reverse=True)
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
        f'#EXTINF:-1 tvg-name="CCTV-6" tvg-id="cctv6" catchup="append" catchup-source="?playbackbegin=${{(b)yyyyMMddHHmmss}}&playbackend=${{(e)yyyyMMddHHmmss}}" group-title="央视频道",CCTV-6 电影 (咪咕时移)',
        f'http://{LAN_IP}:{PORT}/api/cctv6/play.m3u8',
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
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/artplayer/dist/artplayer.js"></script>
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
            padding: 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--card-border);
            gap: 12px;
        }
        .modal-header-left {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            min-width: 0;
        }
        .modal-title {
            font-size: 16px;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .mode-badge {
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 10px;
            font-weight: 600;
            white-space: nowrap;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }
        .mode-lan {
            background: rgba(46, 213, 115, 0.18);
            color: #2ed573;
            border: 1px solid rgba(46, 213, 115, 0.35);
        }
        .mode-wan {
            background: rgba(55, 66, 250, 0.18);
            color: #70a1ff;
            border: 1px solid rgba(55, 66, 250, 0.35);
        }
        .mode-switch-btn {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--card-border);
            color: var(--text-sub);
            padding: 3px 9px;
            border-radius: 12px;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .mode-switch-btn:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.18);
        }
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
            flex-shrink: 0;
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
                <div class="modal-header-left">
                    <div class="modal-title" id="modalTitle">正在播放</div>
                    <span id="modeBadge" class="mode-badge mode-lan">⚡ 局域网播放 (NAS加速)</span>
                    <button id="lineSwitchBtn" class="mode-switch-btn" onclick="togglePlayLine()">换移动专线</button>
                    <button id="copyUrlBtn" class="mode-switch-btn" onclick="copyDirectUrl()">复制直连</button>
                </div>
                <button class="modal-close" onclick="closeModal()">✕</button>
            </div>
            <div class="player-box" id="artplayer-container"></div>
        </div>
    </div>

    <script>
        let art = null;
        let activeDate = "";
        let currentPlayMode = "";
        let currentMovie = null;

        function isLocalNetwork() {
            // 1. 如果当前页面以 HTTPS 协议访问，浏览器强制封锁一切明文 HTTP 媒体流(Mixed Content)，必须走同源代理
            if (window.location.protocol === 'https:') {
                return false;
            }
            const host = window.location.hostname;
            // 2. 本地回环或局域网私有网段，直接直连
            if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host.endsWith('.local')) {
                return true;
            }
            if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
            if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
            if (/^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
            return false;
        }

        async function init() {
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

        let currentLine = 'migu'; // 'migu' or 'pltv'

        function updateModeUI() {
            const badge = document.getElementById('modeBadge');
            const btn = document.getElementById('lineSwitchBtn');
            if (badge) {
                if (isLocalNetwork()) {
                    badge.className = 'mode-badge mode-lan';
                    badge.innerHTML = '⚡ 局域网播放 (NAS加速)';
                } else {
                    badge.className = 'mode-badge mode-wan';
                    badge.innerHTML = '🌐 外网访问 (远程加速)';
                }
            }
            if (btn) {
                btn.innerText = (currentLine === 'migu') ? '换移动专线' : '换咪咕专线';
            }
        }

        function togglePlayLine() {
            if (!currentMovie) return;
            currentLine = (currentLine === 'migu') ? 'pltv' : 'migu';
            startPlayWithCurrentLine(currentMovie);
        }

        function copyDirectUrl() {
            if (!currentMovie) return;
            const rawUrl = (currentLine === 'pltv' && currentMovie.pltv_url) ? currentMovie.pltv_url : (currentMovie.migu_url || currentMovie.play_url);
            if (navigator.clipboard) {
                navigator.clipboard.writeText(rawUrl).then(() => alert('已复制直连地址到剪贴板！可粘贴至 PotPlayer / VLC / 电视机顶盒播放。'));
            } else {
                prompt('请长按复制直连播放地址：', rawUrl);
            }
        }

        function playMovie(movie) {
            currentMovie = movie;
            currentLine = 'migu';
            document.getElementById('modalTitle').innerText = movie.name + ' (' + movie.full_time + ')';
            const modal = document.getElementById('playModal');
            modal.classList.add('open');
            startPlayWithCurrentLine(movie);
        }

        function startPlayWithCurrentLine(movie) {
            updateModeUI();

            const rawUrl = (currentLine === 'pltv' && movie.pltv_url) ? movie.pltv_url : (movie.migu_url || movie.play_url);
            const targetUrl = '/api/cctv6/proxy?url=' + encodeURIComponent(rawUrl);

            if (art) {
                art.destroy(false);
                art = null;
            }

            let hasFallback = false;
            function triggerFallback(errDetail) {
                if (hasFallback) return;
                hasFallback = true;
                if (currentLine === 'migu' && movie.pltv_url) {
                    console.warn('[CCTV6] 咪咕专线拉流受阻 (' + errDetail + ')，自动降级切换为移动专线重试...');
                    currentLine = 'pltv';
                    startPlayWithCurrentLine(movie);
                }
            }

            art = new Artplayer({
                container: '#artplayer-container',
                url: targetUrl,
                title: movie.name + ' (' + movie.full_time + ')',
                type: 'm3u8',
                theme: '#ff4757',
                volume: 0.8,
                isLive: false,
                autoplay: true,
                pip: true,
                setting: true,
                playbackRate: true,
                aspectRatio: true,
                fullscreen: true,
                fullscreenWeb: true,
                playsInline: true,
                airplay: true,
                hotkey: true,
                fastForward: true,
                autoOrientation: true,
                lock: true,
                moreVideoAttr: {
                    crossOrigin: 'anonymous',
                    playsInline: true,
                    'webkit-playsinline': true
                },
                customType: {
                    m3u8: function (video, url, artInstance) {
                        if (video.hls) {
                            video.hls.destroy();
                        }
                        if (video.canPlayType('application/vnd.apple.mpegurl')) {
                            video.src = url;
                            video.onerror = () => triggerFallback('Native HLS error');
                        } else if (Hls.isSupported()) {
                            const hls = new Hls({
                                enableWorker: true,
                                lowLatencyMode: false,
                                maxBufferLength: 30,
                                maxMaxBufferLength: 120,
                                backBufferLength: 30,
                                maxBufferSize: 60 * 1000 * 1000,
                            });

                            video.addEventListener('playing', () => {
                                try {
                                    if (hls.config) {
                                        hls.config.maxBufferLength = 600;
                                        hls.config.maxMaxBufferLength = 900;
                                        hls.config.maxBufferSize = 500 * 1024 * 1024;
                                    }
                                } catch (e) {}
                            }, { once: true });

                            hls.loadSource(url);
                            hls.attachMedia(video);
                            video.hls = hls;

                            hls.on(Hls.Events.ERROR, function (event, data) {
                                if (data.fatal) {
                                    switch (data.type) {
                                        case Hls.ErrorTypes.NETWORK_ERROR:
                                            triggerFallback('HLS Network Error');
                                            break;
                                        case Hls.ErrorTypes.MEDIA_ERROR:
                                            hls.recoverMediaError();
                                            break;
                                        default:
                                            triggerFallback('HLS Fatal Error');
                                            break;
                                    }
                                }
                            });
                        } else {
                            video.src = url;
                        }
                    }
                }
            });

            art.on('error', () => {
                triggerFallback('Artplayer error');
            });
        }

        function closeModal() {
            const modal = document.getElementById('playModal');
            modal.classList.remove('open');
            if (art) {
                art.destroy(false);
                art = null;
            }
            currentMovie = null;
        }

        window.onload = init;
    </script>
</body>
</html>
"""
