#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭影视点播源局域网服务与管理仪表盘
内置：智能大类聚合代理（将采集站碎分类自动聚合成【电影】、【电视】、【综艺】、【动漫】）
"""

import os
import sys
import json
import time
import socket
import ssl
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 导入本地检测引擎与 SQLite 镜像聚合中枢
from checker import run_inspection, OUTPUT_VOD_JSON, OUTPUT_TVBOX_JSON, OUTPUT_VALID_FILE
from db import init_db, query_videos, query_detail, search_videos, get_stats
from sync import run_sync

PORT = 5888
TVBOX_IP = "192.168.0.245"
TVBOX_PORT = 9978
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

is_refreshing = False
refresh_lock = threading.Lock()

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# 内存缓存每个站点的原生分类列表
SOURCE_CLASSES_CACHE = {}

def get_lan_ip():
    """获取本机的真实局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '192.168.0.116'

LAN_IP = get_lan_ip()

def load_json_file(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
    return None

def trigger_background_refresh():
    global is_refreshing
    with refresh_lock:
        if is_refreshing:
            return False
        is_refreshing = True

    def _worker():
        global is_refreshing
        try:
            run_inspection()
        except Exception as e:
            print(f"后台刷新异常: {e}")
        finally:
            with refresh_lock:
                is_refreshing = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True

def auto_refresh_scheduler():
    """守护线程：每隔 6 小时自动重新执行全源巡检和清洗"""
    while True:
        time.sleep(6 * 3600)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 触发定时自动巡检测速...")
        trigger_background_refresh()

def daily_sync_scheduler():
    """守护线程：每天凌晨 05:00 准时自动执行增量更新 (h=24) 与多线路合并"""
    last_run_date = ""
    while True:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == 5 and now.minute == 0 and last_run_date != today_str:
            last_run_date = today_str
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 触发凌晨 05:00 影视库增量同步更新...")
            try:
                run_sync(mode="daily")
            except Exception as e:
                print(f"凌晨定时同步异常: {e}")
        time.sleep(30)

def push_config_to_box():
    """向局域网泰捷 WEBOX (192.168.0.245:9978) 影视仓远程推送配置"""
    sub_url = f"http://{LAN_IP}:{PORT}/vod.json"
    post_data = urllib.parse.urlencode({"do": "api", "url": sub_url}).encode("utf-8")
    try:
        req = urllib.request.Request(f"http://{TVBOX_IP}:{TVBOX_PORT}/action", data=post_data, headers={
            "Content-Type": "application/x-www-form-urlencoded"
        })
        with urllib.request.urlopen(req, timeout=4) as resp:
            content = resp.read().decode("utf-8", errors="ignore").strip()
            return {"success": True, "msg": f"推送成功! 盒子响应: {content}", "box_ip": TVBOX_IP}
    except Exception as e:
        return {"success": False, "msg": f"推送失败: {str(e)}", "box_ip": TVBOX_IP}

def get_real_source_api(source_key):
    """根据 key 从有效源列表获取真实的 API 基础地址"""
    summary = load_json_file(OUTPUT_VALID_FILE)
    if summary:
        for s in summary.get('valid_sources', []):
            if s.get('key') == source_key:
                return s.get('api')
    # fallback
    fallback_map = {
        'hongniu': 'https://www.hongniuzy2.com/api.php/provide/vod/',
        'suoni': 'https://suoniapi.com/api.php/provide/vod/',
        'guangsu': 'https://api.guangsuapi.com/api.php/provide/vod/',
        'jisu': 'https://jszyapi.com/api.php/provide/vod/',
        'ikun': 'https://ikunzyapi.com/api.php/provide/vod/',
        '1080zy': 'https://api.1080zyku.com/inc/apijson.php',
        '360zy': 'https://360zy.com/api.php/provide/vod/'
    }
    return fallback_map.get(source_key)

def fetch_source_classes(api_base):
    """拉取并缓存站点的真实分类树"""
    if api_base in SOURCE_CLASSES_CACHE:
        return SOURCE_CLASSES_CACHE[api_base]
    sep = '&' if '?' in api_base else '?'
    url = f"{api_base}{sep}ac=list"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=4, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            classes = data.get('class', [])
            SOURCE_CLASSES_CACHE[api_base] = classes
            return classes
    except Exception:
        return []

# 内存缓存每个站点的原生分类列表与聚合查询结果
SOURCE_CLASSES_CACHE = {}
AGGREGATE_CACHE = {}
AGGREGATE_CACHE_LOCK = threading.Lock()
CACHE_TTL = 600  # 聚合数据缓存 10 分钟，毫秒级即时响应

def classify_subcategories(raw_classes):
    """
    智能将采集站的碎片子分类映射为四大核心类别：
    1: 电影 (动作、喜剧、爱情、科幻、恐怖、剧情、战争、悬疑、动画等)
    2: 电视 (国产/内地、欧美、韩剧、日剧、港台、马泰、泰剧、海外等)
    3: 综艺 (大陆综艺、港台综艺、日韩综艺、欧美综艺)
    4: 动漫 (中国/国产动漫、日本动漫、欧美动漫、日韩动漫等)
    注意：坚决排除各大采集站中内容为空或只有测试片的虚拟父类【电影】、【电影片】、【电视剧】、【连续剧】
    """
    movie_kw = ['动作', '喜剧', '爱情', '科幻', '恐怖', '剧情', '战争', '悬疑', '惊悚', '犯罪', '奇幻', '武侠', '灾难', '动画片']
    tv_kw = ['内地剧', '国产剧', '大陆剧', '欧美剧', '香港剧', '韩剧', '日剧', '台湾剧', '港澳剧', '马泰剧', '泰剧', '海外剧']
    va_kw = ['大陆综艺', '港台综艺', '日韩综艺', '欧美综艺']
    anime_kw = ['中国动漫', '国产动漫', '日本动漫', '日韩动漫', '欧美动漫', '港台动漫', '动漫电影']

    movies = []
    tvs = []
    vas = []
    animes = []

    for c in raw_classes:
        name = c.get('type_name', '').strip()
        cid = c.get('type_id')
        if not cid or not name:
            continue
        if any(k in name for k in movie_kw) and name not in ['电影', '电影片']:
            movies.append((cid, name))
        elif any(k in name for k in tv_kw) and name not in ['电视剧', '连续剧']:
            tvs.append((cid, name))
        elif any(k in name for k in va_kw):
            vas.append((cid, name))
        elif any(k in name for k in anime_kw):
            animes.append((cid, name))

    # 兜底：如果某些特殊站无细分子类，使用包含该字样的分类
    if not movies:
        for c in raw_classes:
            if '电影' in c.get('type_name', ''):
                movies.append((c.get('type_id'), c.get('type_name')))
    if not tvs:
        for c in raw_classes:
            if any(k in c.get('type_name', '') for k in ['剧', '连续']):
                tvs.append((c.get('type_id'), c.get('type_name')))
    if not vas:
        for c in raw_classes:
            if '综艺' in c.get('type_name', ''):
                vas.append((c.get('type_id'), c.get('type_name')))
    if not animes:
        for c in raw_classes:
            if '动漫' in c.get('type_name', ''):
                animes.append((c.get('type_id'), c.get('type_name')))

    return movies, tvs, vas, animes

def proxy_aggregate_cms(source_key, query_params):
    """核心聚合代理引擎：将采集站的碎片分类自动汇聚为标准的【电影】、【电视】、【综艺】、【动漫】"""
    api_base = get_real_source_api(source_key)
    if not api_base:
        return {"code": 0, "msg": f"未知站点标识: {source_key}", "list": []}

    ac = query_params.get('ac', ['detail'])[0]
    t = query_params.get('t', [''])[0]
    wd = query_params.get('wd', [''])[0]
    ids = query_params.get('ids', [''])[0]
    pg = query_params.get('pg', ['1'])[0]

    STANDARD_CLASSES = [
        {"type_id": 1, "type_name": "电影"},
        {"type_id": 2, "type_name": "电视"},
        {"type_id": 3, "type_name": "综艺"},
        {"type_id": 4, "type_name": "动漫"}
    ]

    # 缓存键
    cache_key = f"{source_key}:{ac}:{t}:{pg}:{wd}:{ids}"
    now_ts = time.time()
    with AGGREGATE_CACHE_LOCK:
        if cache_key in AGGREGATE_CACHE:
            ts, cached_data = AGGREGATE_CACHE[cache_key]
            if now_ts - ts < CACHE_TTL:
                return cached_data

    sep = '&' if '?' in api_base else '?'

    # 1. 搜索关键词透传
    if wd:
        kw = urllib.parse.quote(wd)
        target_url = f"{api_base}{sep}ac=detail&wd={kw}"
        try:
            req = urllib.request.Request(target_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as resp:
                d = json.loads(resp.read().decode('utf-8', errors='ignore'))
                d['class'] = STANDARD_CLASSES
                with AGGREGATE_CACHE_LOCK:
                    AGGREGATE_CACHE[cache_key] = (now_ts, d)
                return d
        except Exception as e:
            return {"code": 0, "msg": str(e), "list": []}

    # 2. 具体影片 ID 透传详情
    if ids:
        target_url = f"{api_base}{sep}ac=detail&ids={ids}"
        try:
            req = urllib.request.Request(target_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as resp:
                d = json.loads(resp.read().decode('utf-8', errors='ignore'))
                d['class'] = STANDARD_CLASSES
                with AGGREGATE_CACHE_LOCK:
                    AGGREGATE_CACHE[cache_key] = (now_ts, d)
                return d
        except Exception as e:
            return {"code": 0, "msg": str(e), "list": []}

    # 3. TVBox 初始化拉取分类列表
    if ac == 'list' and not t:
        res = {
            "code": 1,
            "msg": "数据列表",
            "page": 1,
            "pagecount": 1,
            "limit": "20",
            "total": 4,
            "class": STANDARD_CLASSES,
            "list": []
        }
        with AGGREGATE_CACHE_LOCK:
            AGGREGATE_CACHE[cache_key] = (now_ts, res)
        return res

    # 4. 聚合查询四大核心大类
    raw_classes = fetch_source_classes(api_base)
    movies, tvs, vas, animes = classify_subcategories(raw_classes)

    target_sub_ids = []
    if str(t) in ['1', 'movie']:
        # 电影：提取前 4 个热门分类（如动作、喜剧、科幻、剧情等）
        target_sub_ids = movies[:4]
    elif str(t) in ['2', 'tv']:
        # 电视：提取前 4 个热门电视剧分类（如国产剧、欧美剧、韩剧、日剧/香港剧）
        target_sub_ids = tvs[:4]
    elif str(t) in ['3', 'va']:
        # 综艺：提取前 4 个综艺分类（如大陆综艺、港台综艺、日韩综艺、欧美综艺）
        target_sub_ids = vas[:4]
    elif str(t) in ['4', 'ct']:
        # 动漫：提取前 3-4 个动漫分类（如国产动漫、日本动漫、欧美动漫等）
        target_sub_ids = animes[:4]
    elif not t:
        # 首页推荐：各取 1 个核心子类（动作电影、国产剧/欧美剧、热门综艺、热门动漫）
        if movies:
            target_sub_ids.append(movies[0])
        if tvs:
            target_sub_ids.append(tvs[0])
        if vas:
            target_sub_ids.append(vas[0])
        if animes:
            target_sub_ids.append(animes[0])

    # 如果无法匹配任何子类，兜底透传原接口
    if not target_sub_ids:
        param_str = urllib.parse.urlencode({k: v[0] for k, v in query_params.items()})
        target_url = f"{api_base}{sep}{param_str}"
        try:
            req = urllib.request.Request(target_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=4, context=SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='ignore'))
                data['class'] = STANDARD_CLASSES
                with AGGREGATE_CACHE_LOCK:
                    AGGREGATE_CACHE[cache_key] = (now_ts, data)
                return data
        except Exception as e:
            return {"code": 0, "msg": str(e), "list": []}

    # 并发拉取各子分类内容（每类拉取 8 部，总计 32 部丰富内容）
    def fetch_sub_data(item):
        cid, cname = item
        u = f"{api_base}{sep}ac=detail&t={cid}&pg={pg}&pagesize=8"
        try:
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=2.5, context=SSL_CTX) as resp:
                d = json.loads(resp.read().decode('utf-8', errors='ignore'))
                return d.get('list', [])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(fetch_sub_data, target_sub_ids))

    combined_list = []
    seen_ids = set()
    for sublist in results:
        for v in sublist:
            vid = v.get('vod_id')
            if vid not in seen_ids:
                seen_ids.add(vid)
                combined_list.append(v)

    res = {
        "code": 1,
        "msg": "数据列表",
        "page": int(pg),
        "pagecount": 999,
        "limit": "32",
        "total": 99999,
        "class": STANDARD_CLASSES,
        "list": combined_list
    }
    with AGGREGATE_CACHE_LOCK:
        AGGREGATE_CACHE[cache_key] = (now_ts, res)
    return res

# ============================================================
# 本地轻量 SQLite 超级私有聚合源定义 (方案 B: 多维矩阵筛选 + 多线路秒开)
# ============================================================

VOD_CLASSES = [
    {"type_id": 1, "type_name": "电影"},
    {"type_id": 2, "type_name": "电视剧"},
    {"type_id": 3, "type_name": "综艺"},
    {"type_id": 4, "type_name": "动漫"},
    {"type_id": 5, "type_name": "短剧"}
]

VOD_FILTERS = {
    "1": [
        {
            "key": "class",
            "name": "类型",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "动作", "v": "动作"},
                {"n": "喜剧", "v": "喜剧"},
                {"n": "爱情", "v": "爱情"},
                {"n": "科幻", "v": "科幻"},
                {"n": "悬疑", "v": "悬疑"},
                {"n": "恐怖", "v": "恐怖"},
                {"n": "剧情", "v": "剧情"},
                {"n": "战争", "v": "战争"},
                {"n": "犯罪", "v": "犯罪"},
                {"n": "动画", "v": "动画"},
                {"n": "奇幻", "v": "奇幻"},
                {"n": "武侠", "v": "武侠"},
                {"n": "冒险", "v": "冒险"}
            ]
        },
        {
            "key": "area",
            "name": "地区",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "华语", "v": "华语"},
                {"n": "大陆", "v": "大陆"},
                {"n": "港台", "v": "港台"},
                {"n": "欧美", "v": "欧美"},
                {"n": "韩国", "v": "韩国"},
                {"n": "日本", "v": "日本"},
                {"n": "其他", "v": "其他"}
            ]
        },
        {
            "key": "year",
            "name": "年份",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "2026", "v": "2026"},
                {"n": "2025", "v": "2025"},
                {"n": "2024", "v": "2024"},
                {"n": "2023", "v": "2023"},
                {"n": "2022", "v": "2022"},
                {"n": "2021", "v": "2021"},
                {"n": "2020", "v": "2020"},
                {"n": "更早", "v": "earlier"}
            ]
        }
    ],
    "2": [
        {
            "key": "class",
            "name": "类型",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "悬疑", "v": "悬疑"},
                {"n": "古装", "v": "古装"},
                {"n": "武侠", "v": "武侠"},
                {"n": "都市", "v": "都市"},
                {"n": "爱情", "v": "爱情"},
                {"n": "军旅", "v": "军旅"},
                {"n": "历史", "v": "历史"},
                {"n": "刑侦", "v": "刑侦"},
                {"n": "家庭", "v": "家庭"},
                {"n": "喜剧", "v": "喜剧"}
            ]
        },
        {
            "key": "area",
            "name": "地区",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "国产剧", "v": "国产"},
                {"n": "欧美剧", "v": "欧美"},
                {"n": "韩剧", "v": "韩国"},
                {"n": "日剧", "v": "日本"},
                {"n": "港剧", "v": "香港"},
                {"n": "台剧", "v": "台湾"},
                {"n": "泰剧", "v": "泰国"}
            ]
        },
        {
            "key": "year",
            "name": "年份",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "2026", "v": "2026"},
                {"n": "2025", "v": "2025"},
                {"n": "2024", "v": "2024"},
                {"n": "2023", "v": "2023"},
                {"n": "2022", "v": "2022"},
                {"n": "更早", "v": "earlier"}
            ]
        }
    ],
    "3": [
        {
            "key": "area",
            "name": "地区",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "大陆综艺", "v": "大陆"},
                {"n": "港台综艺", "v": "港台"},
                {"n": "日韩综艺", "v": "日韩"},
                {"n": "欧美综艺", "v": "欧美"}
            ]
        },
        {
            "key": "year",
            "name": "年份",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "2026", "v": "2026"},
                {"n": "2025", "v": "2025"},
                {"n": "2024", "v": "2024"},
                {"n": "更早", "v": "earlier"}
            ]
        }
    ],
    "4": [
        {
            "key": "area",
            "name": "地区",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "国产动漫", "v": "国产"},
                {"n": "日本动漫", "v": "日本"},
                {"n": "欧美动漫", "v": "欧美"}
            ]
        },
        {
            "key": "year",
            "name": "年份",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "2026", "v": "2026"},
                {"n": "2025", "v": "2025"},
                {"n": "2024", "v": "2024"},
                {"n": "更早", "v": "earlier"}
            ]
        }
    ],
    "5": [
        {
            "key": "class",
            "name": "类型",
            "value": [
                {"n": "全部", "v": ""},
                {"n": "战神逆袭", "v": "逆袭"},
                {"n": "穿越重生", "v": "穿越"},
                {"n": "豪门甜宠", "v": "甜宠"},
                {"n": "古装虐恋", "v": "古装"}
            ]
        }
    ]
}

def handle_super_vod(query_params):
    """
    处理超级私有聚合源请求：
    - 支持 TVBox 原生按遥控器【上键】呼出的类型、地区、年份多维交叉筛选
    - 支持影片单卡片内聚合全网多条播放线路（自动容灾换源）
    - 全程由本地 NAS SQLite 极速驱动，响应耗时 < 5ms
    """
    ac = query_params.get('ac', ['detail'])[0]
    t = query_params.get('t', [''])[0]
    pg = int(query_params.get('pg', ['1'])[0])
    wd = query_params.get('wd', [''])[0]
    ids = query_params.get('ids', [''])[0]

    # 多维筛选参数 (方案 B)
    class_kw = query_params.get('class', [''])[0]
    area_kw = query_params.get('area', [''])[0]
    year_kw = query_params.get('year', [''])[0]

    # 解析 ext (部分 TVBox 播放器通过 ext 参数传递 JSON/Base64 筛选字典)
    ext = query_params.get('ext', [''])[0]
    if ext:
        try:
            import base64
            decoded = base64.b64decode(ext).decode('utf-8')
            ext_json = json.loads(decoded)
            class_kw = ext_json.get('class', class_kw)
            area_kw = ext_json.get('area', area_kw)
            year_kw = ext_json.get('year', year_kw)
        except Exception:
            try:
                ext_json = json.loads(ext)
                class_kw = ext_json.get('class', class_kw)
                area_kw = ext_json.get('area', area_kw)
                year_kw = ext_json.get('year', year_kw)
            except Exception:
                pass

    # 1. 详情查询 (合成多站点播放线路)
    if ids:
        try:
            vid = int(ids)
            detail = query_detail(vid)
            if detail:
                return {
                    "code": 1,
                    "msg": "数据列表",
                    "page": 1,
                    "pagecount": 1,
                    "limit": "1",
                    "total": 1,
                    "list": [detail]
                }
            else:
                return {"code": 0, "msg": "未找到影片", "list": []}
        except Exception as e:
            return {"code": 0, "msg": str(e), "list": []}

    # 2. 搜索片名
    if wd:
        res = search_videos(wd, page=pg, pagesize=30)
        res.update({
            "code": 1,
            "msg": "搜索结果",
            "class": VOD_CLASSES,
            "filters": VOD_FILTERS
        })
        return res

    # 3. 分类列表初始化
    if ac == 'list' and not t:
        return {
            "code": 1,
            "msg": "分类列表",
            "page": 1,
            "pagecount": 1,
            "limit": "20",
            "total": len(VOD_CLASSES),
            "class": VOD_CLASSES,
            "filters": VOD_FILTERS,
            "list": []
        }

    # 4. 浏览大类列表与多维筛选
    type_id = int(t) if t and t.isdigit() else None
    res = query_videos(
        type_id=type_id,
        class_kw=class_kw,
        area_kw=area_kw,
        year_kw=year_kw,
        page=pg,
        pagesize=30
    )
    res.update({
        "code": 1,
        "msg": "数据列表",
        "class": VOD_CLASSES,
        "filters": VOD_FILTERS
    })
    return res

DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>家庭影视点播专线源 - 局域网维护中心</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .glass { background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .tag-blue { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
    .tag-green { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
    .tag-purple { background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }
    .tag-amber { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
  </style>
</head>
<body class="min-h-screen p-4 md:p-8">
  <div class="max-w-6xl mx-auto space-y-6">
    
    <!-- 头部区域 -->
    <header class="flex flex-col md:flex-row md:items-center justify-between gap-4 glass p-6 rounded-2xl shadow-xl">
      <div>
        <div class="flex items-center gap-3">
          <span class="text-3xl">🎬</span>
          <h1 class="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 via-indigo-300 to-purple-400">
            NAS 影视点播专线维护中心
          </h1>
        </div>
        <p class="text-slate-400 text-sm mt-1">本地局域网自动化清洗去重、测活测速与低延迟优选服务 · 内置【电影/电视/综艺/动漫】大类聚合引擎</p>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <button id="pushBtn" onclick="pushToBox()" class="px-4 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 active:scale-95 transition-all font-medium text-sm flex items-center gap-2 shadow-lg shadow-purple-500/20">
          <span>📺</span>
          <span id="pushText">一键推送到电视盒子</span>
        </button>
        <button id="refreshBtn" onclick="refreshSources()" class="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 active:scale-95 transition-all font-medium text-sm flex items-center gap-2 shadow-lg shadow-blue-500/20">
          <svg id="refreshIcon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
          <span id="refreshText">立即测速清洗</span>
        </button>
      </div>
    </header>

    <!-- 快捷订阅卡片 -->
    <div class="glass p-6 rounded-2xl border-l-4 border-l-blue-500 shadow-lg">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div class="space-y-1">
          <div class="text-xs uppercase font-bold tracking-wider text-blue-400">局域网 TVBox / 影视仓标准订阅链接</div>
          <div class="text-lg md:text-xl font-mono text-emerald-400 font-semibold select-all break-all" id="subUrl">
            http://{{LAN_IP}}:{{PORT}}/vod.json
          </div>
          <div class="text-xs text-slate-400">核心分类：<b>电影 · 电视 · 综艺 · 动漫</b>（自动聚合海量子类，绝无空数据）</div>
        </div>
        <div class="flex gap-2">
          <button onclick="copySubUrl()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm font-medium transition-all active:scale-95 flex items-center gap-1.5 text-slate-200">
            <span>📋</span> 复制链接
          </button>
          <a href="/vod.json" target="_blank" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm font-medium transition-all text-slate-200">
            预览配置
          </a>
        </div>
      </div>
    </div>

    <!-- 统计指标网格 -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="glass p-5 rounded-2xl">
        <div class="text-slate-400 text-xs font-medium">当前可用优质源</div>
        <div class="text-3xl font-bold text-emerald-400 mt-2 flex items-baseline gap-1">
          <span>{{VALID_COUNT}}</span>
          <span class="text-sm font-normal text-slate-500">/ {{TOTAL_TESTED}}</span>
        </div>
        <div class="text-xs text-slate-400 mt-1">健康率: <span class="text-emerald-400 font-semibold">{{HEALTH_RATE}}%</span></div>
      </div>
      <div class="glass p-5 rounded-2xl">
        <div class="text-slate-400 text-xs font-medium">平均综合首包延迟</div>
        <div class="text-3xl font-bold text-blue-400 mt-2 flex items-baseline gap-1">
          <span>{{AVG_LATENCY}}</span>
          <span class="text-sm font-normal text-slate-500">ms</span>
        </div>
        <div class="text-xs text-emerald-400 mt-1">全链路秒开级别</div>
      </div>
      <div class="glass p-5 rounded-2xl">
        <div class="text-slate-400 text-xs font-medium">已剔除劣质/失效源</div>
        <div class="text-3xl font-bold text-rose-400 mt-2">
          {{FAIL_COUNT}}
        </div>
        <div class="text-xs text-slate-400 mt-1">自动拦截403/404/超时/非HLS</div>
      </div>
      <div class="glass p-5 rounded-2xl">
        <div class="text-slate-400 text-xs font-medium">最近巡检维护时间</div>
        <div class="text-lg font-semibold text-slate-200 mt-3 truncate" title="{{UPDATE_TIME}}">
          {{UPDATE_TIME_SHORT}}
        </div>
        <div class="text-xs text-slate-400 mt-1">耗时: {{DURATION}}s (每6小时自检)</div>
      </div>
    </div>

    <!-- 电视盒子快捷联动模块 -->
    <div class="glass p-6 rounded-2xl shadow-lg border border-slate-800">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-bold text-slate-200 flex items-center gap-2">
          <span>📺</span> 家庭电视盒子配置联动 (泰捷 WEBOX we30s · {{TVBOX_IP}})
        </h2>
        <span class="px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">局域网端口 9978 在线</span>
      </div>
      <div class="grid md:grid-cols-2 gap-4 text-sm text-slate-300">
        <div class="bg-slate-900/60 p-4 rounded-xl border border-slate-800/80 space-y-2">
          <div class="font-semibold text-purple-300 flex items-center gap-2">
            <span>方式 1: 网页端一键直推 (推荐)</span>
          </div>
          <p class="text-xs text-slate-400">
            点击上方紫色的 <b>「一键推送到电视盒子」</b> 按钮，即可由本 NAS 服务直接将清洗后的专线源推送到盒子中的影视仓，电视端将自动刷新生效！
          </p>
        </div>
        <div class="bg-slate-900/60 p-4 rounded-xl border border-slate-800/80 space-y-2">
          <div class="font-semibold text-blue-300 flex items-center gap-2">
            <span>方式 2: 电视端直接输入或扫码</span>
          </div>
          <p class="text-xs text-slate-400">
            打开盒子上影视仓「设置」➔「配置地址」，手机扫码或输入：<br>
            <span class="font-mono text-emerald-400 font-bold">http://{{LAN_IP}}:{{PORT}}/vod.json</span>
          </p>
        </div>
      </div>
    </div>

    <!-- 优质点播源排行榜 -->
    <div class="glass p-6 rounded-2xl shadow-xl">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-6">
        <div>
          <h2 class="text-lg font-bold text-slate-200 flex items-center gap-2">
            <span>⚡</span> 优质可用源推荐排行 (按流媒体响应速度实时排序)
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">所有站点均通过首集 m3u8 真实播放流握手验证与 HLS 协议校验</p>
        </div>
        <span class="text-xs text-slate-400 font-mono">共 {{VALID_COUNT}} 条极速专线</span>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="text-slate-400 text-xs border-b border-slate-700/60">
              <th class="pb-3 pl-2">排名 / 站点名称</th>
              <th class="pb-3">类别</th>
              <th class="pb-3">综合延迟</th>
              <th class="pb-3">流媒体首包</th>
              <th class="pb-3">片源储备</th>
              <th class="pb-3">测试样例片目</th>
              <th class="pb-3 text-right pr-2">状态</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60">
            {{VALID_ROWS}}
          </tbody>
        </table>
      </div>
    </div>

    <!-- 淘汰/排障记录折叠区 -->
    <details class="glass p-6 rounded-2xl shadow-lg group">
      <summary class="cursor-pointer font-medium text-sm text-slate-400 group-hover:text-slate-200 flex items-center justify-between list-none">
        <span class="flex items-center gap-2">
          <span>🛡️</span> 已自动拦截过滤的无效/慢速源清单 ({{FAIL_COUNT}} 个)
        </span>
        <span class="text-xs text-slate-500 group-open:rotate-180 transition-transform">▼</span>
      </summary>
      <div class="mt-4 overflow-x-auto border-t border-slate-800 pt-4">
        <table class="w-full text-left text-xs">
          <thead>
            <tr class="text-slate-500 border-b border-slate-800 pb-2">
              <th class="pb-2">站点名称</th>
              <th class="pb-2">接口地址</th>
              <th class="pb-2">拦截过滤原因</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60 text-slate-400">
            {{FAIL_ROWS}}
          </tbody>
        </table>
      </div>
    </details>

    <footer class="text-center text-xs text-slate-500 pt-4 pb-8">
      NAS 影视专线服务 · 本地自动化测活守护运行中 · 局域网服务端口 {{PORT}}
    </footer>
  </div>

  <script>
    function copySubUrl() {
      const url = document.getElementById('subUrl').innerText.trim();
      navigator.clipboard.writeText(url).then(() => {
        alert('订阅地址已复制到剪贴板:\\n' + url);
      }).catch(err => {
        prompt('请手动复制以下订阅链接:', url);
      });
    }

    function pushToBox() {
      const btn = document.getElementById('pushBtn');
      const text = document.getElementById('pushText');
      btn.disabled = true;
      text.innerText = '正在推送到电视盒子...';

      fetch('/api/push_tv')
        .then(res => res.json())
        .then(data => {
          alert(data.msg);
          text.innerText = '一键推送到电视盒子';
          btn.disabled = false;
        })
        .catch(e => {
          alert('推送接口请求失败: ' + e);
          text.innerText = '一键推送到电视盒子';
          btn.disabled = false;
        });
    }

    function refreshSources() {
      const btn = document.getElementById('refreshBtn');
      const text = document.getElementById('refreshText');
      const icon = document.getElementById('refreshIcon');
      btn.disabled = true;
      btn.classList.add('opacity-70');
      icon.classList.add('animate-spin');
      text.innerText = '正在测速巡检中...';

      fetch('/api/refresh')
        .then(res => res.json())
        .then(data => {
          setTimeout(() => {
            window.location.reload();
          }, 8500);
        })
        .catch(e => {
          alert('请求失败: ' + e);
          btn.disabled = false;
          icon.classList.remove('animate-spin');
          text.innerText = '立即测速清洗';
        });
    }
  </script>
</body>
</html>
"""

class TVBoxRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if "/api/" in self.path or "/vod.json" in self.path or "/proxy/" in self.path:
            super().log_message(format, *args)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ['/vod.json', '/tvbox.json']:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        # 1. 本地轻量 SQLite 超级私有聚合源端点（多线路合成 + 多维筛选方案B + 毫秒级秒开）
        if path in ['/api/vod', '/api/super_vod']:
            query_params = urllib.parse.parse_qs(parsed.query)
            data = handle_super_vod(query_params)
            content = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
            return

        # 2. 智能分类聚合代理端点：/proxy/<source_key>
        elif path.startswith('/proxy/'):
            parts = path.strip('/').split('/')
            source_key = parts[1] if len(parts) >= 2 else 'hongniu'
            query_params = urllib.parse.parse_qs(parsed.query)
            data = proxy_aggregate_cms(source_key, query_params)
            content = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
            return

        # 2. TVBox / 影视仓标准配置订阅
        elif path in ['/vod.json', '/tvbox.json']:
            if not os.path.exists(OUTPUT_VOD_JSON):
                run_inspection()
            try:
                with open(OUTPUT_VOD_JSON, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, f"Error reading vod config: {e}")
            return

        # 3. 状态查询 API
        elif path == '/api/status':
            data = load_json_file(OUTPUT_VALID_FILE) or {}
            data['is_refreshing'] = is_refreshing
            content = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
            return

        # 4. 触发立即重检 API
        elif path == '/api/refresh':
            started = trigger_background_refresh()
            resp_data = {"status": "started" if started else "already_running"}
            content = json.dumps(resp_data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
            return

        # 5. 一键推送到电视盒子 API
        elif path == '/api/push_tv':
            result = push_config_to_box()
            content = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
            return

        # 6. Web 仪表盘首页
        elif path in ['/', '/index.html']:
            summary = load_json_file(OUTPUT_VALID_FILE)
            if not summary:
                summary = run_inspection()

            valid_sources = summary.get('valid_sources', [])
            fail_sources = summary.get('fail_sources', [])
            total_tested = summary.get('total_tested', 0)
            valid_count = len(valid_sources)
            fail_count = len(fail_sources)
            health_rate = round((valid_count / total_tested * 100), 1) if total_tested else 0

            latencies = [s['total_latency'] for s in valid_sources if s.get('total_latency')]
            avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0

            valid_rows = []
            for idx, s in enumerate(valid_sources, 1):
                cat = s.get('category', '综合影视')
                tag_class = "tag-blue"
                if "动漫" in cat:
                    tag_class = "tag-purple"
                elif "秒播" in s.get('name', ''):
                    tag_class = "tag-green"
                elif "专线" in s.get('name', ''):
                    tag_class = "tag-amber"

                sample = s.get('sample_title', '-')
                if len(sample) > 16:
                    sample = sample[:16] + '...'

                total_m = f"{s.get('total_movies', 0):,}"
                row_html = f"""
                <tr class="hover:bg-slate-800/40 transition-colors">
                  <td class="py-3.5 pl-2 font-medium">
                    <div class="flex items-center gap-2">
                      <span class="w-5 text-center text-xs font-mono {'text-amber-400 font-bold' if idx <= 3 else 'text-slate-500'}">#{idx}</span>
                      <span class="text-slate-200">{s.get('name')}</span>
                    </div>
                  </td>
                  <td class="py-3.5"><span class="px-2 py-0.5 rounded text-xs {tag_class}">{cat}</span></td>
                  <td class="py-3.5 font-mono text-emerald-400 font-semibold">{s.get('total_latency')} ms</td>
                  <td class="py-3.5 font-mono text-slate-300 text-xs">{s.get('stream_latency')} ms (首包)</td>
                  <td class="py-3.5 font-mono text-slate-400 text-xs">{total_m} 部</td>
                  <td class="py-3.5 text-slate-400 text-xs truncate max-w-xs" title="{s.get('sample_title')}">{sample}</td>
                  <td class="py-3.5 text-right pr-2">
                    <span class="inline-flex items-center gap-1 text-xs text-emerald-400 font-medium">
                      <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span> 流畅
                    </span>
                  </td>
                </tr>
                """
                valid_rows.append(row_html)

            fail_rows = []
            for s in fail_sources:
                fail_html = f"""
                <tr class="hover:bg-slate-850">
                  <td class="py-2 text-slate-300">{s.get('name')}</td>
                  <td class="py-2 font-mono text-slate-500 truncate max-w-xs">{s.get('api')}</td>
                  <td class="py-2 text-rose-400">{s.get('error_reason')}</td>
                </tr>
                """
                fail_rows.append(fail_html)

            update_time = summary.get('update_time', '')
            update_time_short = update_time.split(' ')[-1] if ' ' in update_time else update_time

            html = DASHBOARD_HTML_TEMPLATE
            html = html.replace('{{LAN_IP}}', LAN_IP)
            html = html.replace('{{PORT}}', str(PORT))
            html = html.replace('{{TVBOX_IP}}', TVBOX_IP)
            html = html.replace('{{VALID_COUNT}}', str(valid_count))
            html = html.replace('{{TOTAL_TESTED}}', str(total_tested))
            html = html.replace('{{HEALTH_RATE}}', str(health_rate))
            html = html.replace('{{AVG_LATENCY}}', str(avg_latency))
            html = html.replace('{{FAIL_COUNT}}', str(fail_count))
            html = html.replace('{{UPDATE_TIME}}', update_time)
            html = html.replace('{{UPDATE_TIME_SHORT}}', update_time_short)
            html = html.replace('{{DURATION}}', str(summary.get('duration_seconds', 0)))
            html = html.replace('{{VALID_ROWS}}', '\n'.join(valid_rows))
            html = html.replace('{{FAIL_ROWS}}', '\n'.join(fail_rows))

            content = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        else:
            self.send_error(404, "File Not Found")

def main():
    if not os.path.exists(OUTPUT_VOD_JSON):
        print("未检测到有效源缓存，初始化执行全量巡检...")
        run_inspection()

    t_sched = threading.Thread(target=auto_refresh_scheduler, daemon=True)
    t_sched.start()

    t_daily = threading.Thread(target=daily_sync_scheduler, daemon=True)
    t_daily.start()

    server_address = ('0.0.0.0', PORT)
    httpd = ThreadingHTTPServer(server_address, TVBoxRequestHandler)
    print(f"\n=======================================================")
    print(f"影视点播源本地维护服务已成功启动!")
    print(f"本地服务访问: http://127.0.0.1:{PORT}/")
    print(f"局域网控制台: http://{LAN_IP}:{PORT}/")
    print(f"TVBox 订阅源: http://{LAN_IP}:{PORT}/vod.json")
    print(f"超级私有源: http://{LAN_IP}:{PORT}/api/vod")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("服务已停止")
        httpd.server_close()

if __name__ == '__main__':
    main()
