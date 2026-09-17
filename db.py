#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭私有影库 - 本地轻量 SQLite 镜像数据库与多线路聚合中枢
"""

import os
import re
import time
import sqlite3
import threading

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "vod.db")
db_lock = threading.Lock()

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def normalize_title(title: str) -> str:
    """标题归一化，用于跨采集站同名影片合并"""
    if not title:
        return ""
    t = title.strip()
    # 去除常见副标题、画质标注、语言标注
    t = re.sub(r'\[.*?\]|\(.*?\)|（.*?）|【.*?】', '', t)
    t = re.sub(r'(4K|1080P|HD|BD|TC|抢先版|国语版|粤语版|普通话|超清|正片|无修版|全集)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[\s_·\-\:\：\.\,\，]+', '', t)
    return t.lower()

def init_db():
    """初始化数据库表与高性能索引"""
    with db_lock:
        conn = get_connection()
        c = conn.cursor()
        
        # 1. 影片主表
        c.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norm_title TEXT NOT NULL,
            vod_name TEXT NOT NULL,
            type_id INTEGER NOT NULL,
            type_name TEXT DEFAULT '',
            vod_pic TEXT DEFAULT '',
            vod_remarks TEXT DEFAULT '',
            vod_year TEXT DEFAULT '',
            vod_area TEXT DEFAULT '',
            vod_lang TEXT DEFAULT '',
            vod_class TEXT DEFAULT '',
            vod_actor TEXT DEFAULT '',
            vod_director TEXT DEFAULT '',
            vod_content TEXT DEFAULT '',
            routes_count INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        );
        """)

        # 2. 播放线路表（一个影片对应多个采集站的播放线路）
        c.execute("""
        CREATE TABLE IF NOT EXISTS video_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            site_key TEXT NOT NULL,
            site_name TEXT NOT NULL,
            site_vod_id TEXT NOT NULL,
            play_from TEXT DEFAULT '',
            play_url TEXT DEFAULT '',
            latency INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0,
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
            UNIQUE(video_id, site_key)
        );
        """)

        # 3. 索引优化
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_norm ON videos(norm_title);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_type ON videos(type_id, updated_at DESC);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_search ON videos(vod_name);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_routes_vid ON video_routes(video_id);")

        # 自动纠偏：确保电影各题材片种归入电影(1)，电视剧归入电视剧(2)
        c.execute("""
        UPDATE videos SET type_id = 1, type_name = '电影'
        WHERE type_id = 2 AND (
            vod_class LIKE '%喜剧片%' OR vod_class LIKE '%剧情片%' OR vod_class LIKE '%动作片%' 
            OR vod_class LIKE '%科幻片%' OR vod_class LIKE '%恐怖片%' OR vod_class LIKE '%爱情片%'
            OR vod_class LIKE '%战争片%' OR vod_class LIKE '%悬疑片%' OR vod_class LIKE '%动画片%'
        ) AND vod_class NOT LIKE '%国产剧%' AND vod_class NOT LIKE '%内地剧%' 
          AND vod_class NOT LIKE '%欧美剧%' AND vod_class NOT LIKE '%韩剧%' 
          AND vod_class NOT LIKE '%日剧%' AND vod_class NOT LIKE '%香港剧%' 
          AND vod_class NOT LIKE '%港澳剧%' AND vod_class NOT LIKE '%台湾剧%';
        """)

        conn.commit()
        conn.close()

def map_type_id(raw_type_name: str, raw_class: str = "") -> int:
    """将采集站的细分分类智能映射为 1:电影, 2:电视剧, 3:综艺, 4:动漫, 5:短剧"""
    t_name = raw_type_name or ""
    c_name = raw_class or ""
    text = f"{t_name},{c_name}"

    # 1. 优先排除短剧
    if any(k in text for k in ['短剧', '爽剧', '微短剧', '反转爽剧', '总裁', '穿越年代', '现代都市短剧']):
        return 5

    # 2. 动漫与动画
    if any(k in text for k in ['动漫', '动画', '日漫', '国漫', '新番', '番剧', '剧场版']):
        return 4

    # 3. 综艺
    if any(k in text for k in ['综艺', '真人秀', '脱口秀', '选秀', '晚会']):
        return 3

    # 4. 明确的电影类型（动作片、喜剧片、科幻片、爱情片、恐怖片、剧情片、战争片、微电影等）
    movie_words = ['动作片', '喜剧片', '科幻片', '爱情片', '恐怖片', '剧情片', '战争片', '惊悚片', '纪录片', '灾难片', '悬疑片', '犯罪片', '奇幻片', '预告片', '电影']
    if any(k in t_name for k in movie_words):
        return 1

    # 5. 明确的电视剧分类（国产剧、欧美剧、韩剧、日剧、港剧、台剧、泰剧等）
    tv_words = ['国产剧', '内地剧', '大陆剧', '欧美剧', '韩剧', '日剧', '港剧', '台剧', '泰剧', '英剧', '海外剧', '香港剧', '台湾剧', '港澳剧', '马泰剧', '连续剧', '电视剧']
    if any(k in t_name for k in tv_words) or any(k in c_name for k in tv_words):
        return 2

    # 6. 兜底判定
    if any(k in t_name for k in ['动作', '喜剧', '爱情', '科幻', '恐怖', '惊悚', '战争', '悬疑', '犯罪', '奇幻', '剧情']):
        return 1
    if any(k in t_name for k in ['剧', '连续']):
        return 2

    # 兜底：电影
    return 1

def upsert_item(item: dict, site_key: str, site_name: str, latency: int = 0):
    """
    插入或聚合合并一部影片：
    - 若标题已存在，则将该源追加为新的一条播放线路（多源容灾）；
    - 若标题不存在，则新建影片条目及首选线路。
    """
    raw_title = item.get('vod_name', '').strip()
    if not raw_title:
        return None
    
    norm = normalize_title(raw_title)
    if not norm:
        norm = raw_title.lower()

    raw_type = item.get('type_name', '')
    raw_class = item.get('vod_class', '')
    type_id = map_type_id(raw_type, raw_class)

    type_name_map = {1: '电影', 2: '电视剧', 3: '综艺', 4: '动漫', 5: '短剧'}
    type_name = type_name_map.get(type_id, '电影')

    pic = item.get('vod_pic', '')
    remarks = item.get('vod_remarks', '')
    year = str(item.get('vod_year', '')).strip()
    area = item.get('vod_area', '').strip()
    lang = item.get('vod_lang', '').strip()
    actor = item.get('vod_actor', '').strip()
    director = item.get('vod_director', '').strip()
    content = item.get('vod_content', '').strip()
    
    # 规范化 class 标签，将 type_name 也混入作为标签备选
    classes = set()
    for c in raw_class.split(','):
        if c.strip():
            classes.add(c.strip())
    if raw_type:
        classes.add(raw_type)
    vod_class_str = ','.join(list(classes)[:6])

    play_url = item.get('vod_play_url', '')
    play_from = item.get('vod_play_from', '')
    site_vod_id = str(item.get('vod_id', ''))
    now = int(time.time())

    with db_lock:
        conn = get_connection()
        c = conn.cursor()
        
        # 检查是否已存在同名影片
        c.execute("SELECT id, vod_pic, vod_remarks, vod_class FROM videos WHERE norm_title = ? AND type_id = ? LIMIT 1", (norm, type_id))
        row = c.fetchone()
        
        if row:
            video_id = row['id']
            # 更新已有影片的海报或集数信息（如有更新的集数）
            update_fields = []
            params = []
            if not row['vod_pic'] and pic:
                update_fields.append("vod_pic = ?")
                params.append(pic)
            if remarks:
                update_fields.append("vod_remarks = ?")
                params.append(remarks)
            update_fields.append("updated_at = ?")
            params.append(now)
            params.append(video_id)
            
            c.execute(f"UPDATE videos SET {', '.join(update_fields)} WHERE id = ?", params)
        else:
            c.execute("""
            INSERT INTO videos (
                norm_title, vod_name, type_id, type_name, vod_pic, vod_remarks,
                vod_year, vod_area, vod_lang, vod_class, vod_actor, vod_director,
                vod_content, routes_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                norm, raw_title, type_id, type_name, pic, remarks,
                year, area, lang, vod_class_str, actor, director,
                content, now, now
            ))
            video_id = c.lastrowid

        # 插入或更新对应站点的播放线路
        c.execute("""
        INSERT INTO video_routes (
            video_id, site_key, site_name, site_vod_id, play_from, play_url, latency, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id, site_key) DO UPDATE SET
            play_url = excluded.play_url,
            site_vod_id = excluded.site_vod_id,
            latency = excluded.latency,
            updated_at = excluded.updated_at
        """, (
            video_id, site_key, site_name, site_vod_id, play_from, play_url, latency, now
        ))

        # 更新线路数量
        c.execute("SELECT COUNT(*) as cnt FROM video_routes WHERE video_id = ?", (video_id,))
        cnt = c.fetchone()['cnt']
        c.execute("UPDATE videos SET routes_count = ? WHERE id = ?", (cnt, video_id))

        conn.commit()
        conn.close()
        return video_id

def query_videos(type_id=None, class_kw='', area_kw='', year_kw='', page=1, pagesize=30):
    """
    根据一级大类、二级题材、地区、年份进行多维交集筛选查询，毫秒级响应
    """
    conn = get_connection()
    c = conn.cursor()

    conditions = []
    params = []

    if type_id:
        conditions.append("type_id = ?")
        params.append(int(type_id))

    # 二级题材筛选 (结合中国影视题材同义词扩展，确保满屏数据)
    if class_kw:
        SYNONYMS = {
            '都市': "(vod_class LIKE '%都市%' OR vod_class LIKE '%现代%' OR vod_content LIKE '%都市%' OR vod_content LIKE '%职场%')",
            '古装': "(vod_class LIKE '%古装%' OR vod_class LIKE '%武侠%' OR vod_class LIKE '%仙侠%' OR vod_content LIKE '%古装%')",
            '武侠': "(vod_class LIKE '%武侠%' OR vod_class LIKE '%古装%' OR vod_class LIKE '%仙侠%')",
            '军旅': "(vod_class LIKE '%军旅%' OR vod_class LIKE '%战争%' OR vod_class LIKE '%抗战%' OR vod_content LIKE '%军旅%' OR vod_content LIKE '%部队%')",
            '刑侦': "(vod_class LIKE '%刑侦%' OR vod_class LIKE '%犯罪%' OR vod_class LIKE '%警匪%' OR vod_content LIKE '%刑侦%' OR vod_content LIKE '%警察%')",
            '家庭': "(vod_class LIKE '%家庭%' OR vod_class LIKE '%伦理%' OR vod_class LIKE '%生活%' OR vod_content LIKE '%家庭%')",
            '爱情': "(vod_class LIKE '%爱情%' OR vod_class LIKE '%言情%' OR vod_content LIKE '%恋爱%')",
            '历史': "(vod_class LIKE '%历史%' OR vod_class LIKE '%传记%' OR vod_content LIKE '%历史%')",
            '喜剧': "(vod_class LIKE '%喜剧%' OR vod_content LIKE '%喜剧%' OR vod_content LIKE '%搞笑%')",
            '悬疑': "(vod_class LIKE '%悬疑%' OR vod_class LIKE '%惊悚%' OR vod_content LIKE '%悬疑%')"
        }
        if class_kw in SYNONYMS:
            conditions.append(SYNONYMS[class_kw])
        else:
            conditions.append("(vod_class LIKE ? OR vod_name LIKE ? OR vod_content LIKE ?)")
            params.append(f"%{class_kw}%")
            params.append(f"%{class_kw}%")
            params.append(f"%{class_kw}%")

    # 地区筛选 (支持 '华语', '国产', '欧美', '日本', '韩国', '香港', '台湾', '泰国' 等)
    if area_kw:
        if area_kw in ['华语', '国产']:
            conditions.append("(vod_area LIKE '%大陆%' OR vod_area LIKE '%内地%' OR vod_area LIKE '%香港%' OR vod_area LIKE '%台湾%' OR vod_area LIKE '%中国%' OR vod_area LIKE '%华语%' OR vod_class LIKE '%国产%' OR vod_class LIKE '%内地%' OR type_name LIKE '%国产%')")
        elif area_kw in ['港台', '香港', '台湾']:
            conditions.append("(vod_area LIKE '%香港%' OR vod_area LIKE '%台湾%' OR vod_class LIKE '%香港%' OR vod_class LIKE '%港%' OR vod_class LIKE '%台湾%')")
        elif area_kw in ['欧美']:
            conditions.append("(vod_area LIKE '%欧美%' OR vod_area LIKE '%美国%' OR vod_area LIKE '%英国%' OR vod_area LIKE '%法国%' OR vod_area LIKE '%加拿大%' OR vod_class LIKE '%欧美%')")
        elif area_kw in ['日韩']:
            conditions.append("(vod_area LIKE '%日本%' OR vod_area LIKE '%韩国%' OR vod_class LIKE '%日%' OR vod_class LIKE '%韩%')")
        elif area_kw in ['韩国']:
            conditions.append("(vod_area LIKE '%韩国%' OR vod_class LIKE '%韩%')")
        elif area_kw in ['日本']:
            conditions.append("(vod_area LIKE '%日本%' OR vod_class LIKE '%日%')")
        elif area_kw in ['泰国']:
            conditions.append("(vod_area LIKE '%泰%' OR vod_class LIKE '%泰%')")
        else:
            conditions.append("(vod_area LIKE ? OR vod_class LIKE ?)")
            params.append(f"%{area_kw}%")
            params.append(f"%{area_kw}%")

    # 年份筛选
    if year_kw:
        if year_kw == 'earlier':
            conditions.append("(CAST(vod_year AS INTEGER) < 2020 AND vod_year != '')")
        else:
            conditions.append("vod_year LIKE ?")
            params.append(f"%{year_kw}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    
    # 统计总数
    c.execute(f"SELECT COUNT(*) as total FROM videos {where_clause}", params)
    total = c.fetchone()['total']

    # 分页查询
    offset = (page - 1) * pagesize
    query_sql = f"""
    SELECT id, vod_name, type_id, type_name, vod_pic, vod_remarks, vod_year, vod_area, vod_class, routes_count
    FROM videos
    {where_clause}
    ORDER BY updated_at DESC, id DESC
    LIMIT ? OFFSET ?
    """
    params.extend([pagesize, offset])
    c.execute(query_sql, params)
    rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "vod_id": r['id'],
            "vod_name": r['vod_name'],
            "type_id": r['type_id'],
            "type_name": r['type_name'],
            "vod_pic": r['vod_pic'],
            "vod_remarks": f"{r['vod_remarks']} [{r['routes_count']}线]" if r['routes_count'] > 1 else r['vod_remarks'],
            "vod_year": r['vod_year'],
            "vod_area": r['vod_area'],
            "vod_class": r['vod_class']
        })

    conn.close()
    pagecount = (total + pagesize - 1) // pagesize if total > 0 else 1
    return {
        "page": page,
        "pagecount": pagecount,
        "limit": str(pagesize),
        "total": total,
        "list": results
    }

def query_detail(video_id: int):
    """
    查询影片详情，并合成多线路播放源（红牛专线$$$索尼4K$$$极速蓝光...）
    """
    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT * FROM videos WHERE id = ? LIMIT 1", (video_id,))
    v = c.fetchone()
    if not v:
        conn.close()
        return None

    # 获取所有关联的播放线路，按延迟和更新时间排序
    c.execute("SELECT * FROM video_routes WHERE video_id = ? ORDER BY latency ASC, id ASC", (video_id,))
    routes = c.fetchall()
    conn.close()

    if not routes:
        return None

    play_from_list = []
    play_url_list = []

    for r in routes:
        from_parts = r['play_from'].split('$$$')
        url_parts = r['play_url'].split('$$$')
        site_name = r['site_name']

        # 优先提取纯净 m3u8 直连线路
        m3u8_idx = -1
        for idx, f in enumerate(from_parts):
            if 'm3u8' in f.lower():
                m3u8_idx = idx
                break

        if m3u8_idx >= 0 and m3u8_idx < len(url_parts) and url_parts[m3u8_idx].strip():
            play_from_list.append(site_name)
            play_url_list.append(url_parts[m3u8_idx])
        else:
            # 兜底取第一个非空有效线路
            for idx, u in enumerate(url_parts):
                if u.strip():
                    play_from_list.append(site_name)
                    play_url_list.append(u)
                    break

    return {
        "vod_id": v['id'],
        "vod_name": v['vod_name'],
        "type_id": v['type_id'],
        "type_name": v['type_name'],
        "vod_pic": v['vod_pic'],
        "vod_remarks": v['vod_remarks'],
        "vod_year": v['vod_year'],
        "vod_area": v['vod_area'],
        "vod_lang": v['vod_lang'],
        "vod_class": v['vod_class'],
        "vod_actor": v['vod_actor'],
        "vod_director": v['vod_director'],
        "vod_content": v['vod_content'],
        "vod_play_from": "$$$".join(play_from_list),
        "vod_play_url": "$$$".join(play_url_list)
    }

def search_videos(keyword: str, page=1, pagesize=30):
    """全库搜索影片，支持首字母或片名模糊匹配"""
    conn = get_connection()
    c = conn.cursor()

    kw = keyword.strip()
    c.execute("SELECT COUNT(*) as total FROM videos WHERE vod_name LIKE ?", (f"%{kw}%",))
    total = c.fetchone()['total']

    offset = (page - 1) * pagesize
    c.execute("""
    SELECT id, vod_name, type_id, type_name, vod_pic, vod_remarks, vod_year, vod_area, vod_class, routes_count
    FROM videos
    WHERE vod_name LIKE ?
    ORDER BY updated_at DESC
    LIMIT ? OFFSET ?
    """, (f"%{kw}%", pagesize, offset))
    rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "vod_id": r['id'],
            "vod_name": r['vod_name'],
            "type_id": r['type_id'],
            "type_name": r['type_name'],
            "vod_pic": r['vod_pic'],
            "vod_remarks": f"{r['vod_remarks']} [{r['routes_count']}线]" if r['routes_count'] > 1 else r['vod_remarks'],
            "vod_year": r['vod_year'],
            "vod_area": r['vod_area']
        })

    conn.close()
    pagecount = (total + pagesize - 1) // pagesize if total > 0 else 1
    return {
        "page": page,
        "pagecount": pagecount,
        "limit": str(pagesize),
        "total": total,
        "list": results
    }

def get_stats():
    """获取数据库统计汇总信息"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total_v FROM videos")
    total_v = c.fetchone()['total_v']
    c.execute("SELECT COUNT(*) as total_r FROM video_routes")
    total_r = c.fetchone()['total_r']

    c.execute("SELECT type_id, type_name, COUNT(*) as cnt FROM videos GROUP BY type_id")
    type_stats = {}
    for r in c.fetchall():
        type_stats[r['type_name']] = r['cnt']

    conn.close()
    return {
        "total_videos": total_v,
        "total_routes": total_r,
        "types": type_stats
    }

if __name__ == '__main__':
    init_db()
    print("SQLite 数据库初始化完成: ", DB_PATH)
    print("当前统计:", get_stats())
