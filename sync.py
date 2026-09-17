#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭私有影库 - 全网采集站多线路同步引擎
支持：
1. bootstrap 模式：初始化快速拉取最近热门精选片单入库
2. daily 模式：每日凌晨 5 点拉取最近 24 小时增量更新 (h=24)
"""

import os
import sys
import json
import time
import urllib.request
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from db import init_db, upsert_item, get_stats

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
VALID_FILE = os.path.join(CURRENT_DIR, "valid_sources.json")
LOG_FILE = os.path.join(CURRENT_DIR, "sync.log")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def fetch_page(api_base, params):
    sep = '&' if '?' in api_base else '?'
    url = f"{api_base}{sep}{params}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            return data
    except Exception as e:
        return None

def sync_single_source(source, hours=24, max_pages=5):
    site_key = source.get('key')
    site_name = source.get('name', '未命名')
    api_base = source.get('api')
    latency = source.get('total_latency', 2000)

    if not api_base:
        return 0

    log(f"开始同步站点: 【{site_name}】 (更新跨度: 最近 {hours} 小时)...")
    total_synced = 0

    # 先拉第 1 页，获取总页数
    p1_data = fetch_page(api_base, f"ac=detail&h={hours}&pg=1&pagesize=50")
    if not p1_data or 'list' not in p1_data:
        # fallback 不带 h，直接拉前几页最新片目
        p1_data = fetch_page(api_base, f"ac=detail&pg=1&pagesize=50")
        if not p1_data or 'list' not in p1_data:
            log(f"站点【{site_name}】连接或解析失败，已跳过")
            return 0

    pagecount = int(p1_data.get('pagecount', 1))
    target_pages = min(pagecount, max_pages)

    # 处理第 1 页
    for item in p1_data.get('list', []):
        if upsert_item(item, site_key, site_name, latency):
            total_synced += 1

    # 处理后续分页
    for pg in range(2, target_pages + 1):
        p_data = fetch_page(api_base, f"ac=detail&h={hours}&pg={pg}&pagesize=50")
        if not p_data or 'list' not in p_data:
            p_data = fetch_page(api_base, f"ac=detail&pg={pg}&pagesize=50")
        if p_data and 'list' in p_data:
            for item in p_data.get('list', []):
                if upsert_item(item, site_key, site_name, latency):
                    total_synced += 1

    log(f"站点【{site_name}】同步完成，处理片目: {total_synced} 条")
    return total_synced

def run_sync(mode="daily"):
    """
    mode:
    - 'bootstrap': 初次建库，拉取前 8 大优质源最近 72 小时热门片目（约 2000+ 部精品）
    - 'daily': 每日凌晨增量更新，拉取最近 24 小时更新 (h=24)
    """
    init_db()
    t0 = time.time()
    log(f"=== 启动影视库多线路同步任务 [模式: {mode}] ===")

    if not os.path.exists(VALID_FILE):
        log(f"未找到有效源列表文件: {VALID_FILE}")
        return

    with open(VALID_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    valid_sources = data.get('valid_sources', [])
    # 选取测速排序靠前的优质源（主站 + 专业动漫站 + 综合站）
    # 包含：红牛、索尼、极速、金鹰、虎牙、魔都动漫、樱花动漫、新浪
    target_sources = valid_sources[:8]

    hours = 72 if mode == "bootstrap" else 24
    max_pages = 8 if mode == "bootstrap" else 4

    grand_total = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(sync_single_source, s, hours, max_pages) for s in target_sources]
        for f in futures:
            try:
                grand_total += f.result()
            except Exception as e:
                log(f"子任务执行异常: {e}")

    cost = round(time.time() - t0, 2)
    stats = get_stats()
    log(f"=== 同步结束! 耗时: {cost}s | 本次拉取: {grand_total} 条 | 数据库当前总影片: {stats['total_videos']} 部 (总计播放线路: {stats['total_routes']} 条) ===")
    log(f"分类分布: {stats['types']}")

if __name__ == '__main__':
    mode = "bootstrap" if "--bootstrap" in sys.argv else "daily"
    run_sync(mode=mode)
