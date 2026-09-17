#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭私有影库 - 全分类深度填充引擎 (Seed Categories)
目标：
1. 遍历各大主流采集站（红牛、极速、虎牙、iKun、魔都）的全部核心子分类
2. 按子类定向拉取前 3~6 页精品片单（彻底覆盖动作、喜剧、科幻、爱情、悬疑、国产剧、欧美剧、韩剧、日剧、动漫、综艺）
3. 跨站点同名自动合并聚合多线路，快速构建 5,000~10,000 部全维丰盈片库
"""

import os
import sys
import json
import time
import urllib.request
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def fetch_json(url, timeout=6):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode('utf-8', errors='ignore'))
    except Exception:
        return None

# 核心子分类匹配关键词清单
CORE_CATEGORIES = {
    # 电影子类
    "动作片": ["动作", "动作片"],
    "喜剧片": ["喜剧", "喜剧片"],
    "爱情片": ["爱情", "爱情片"],
    "科幻片": ["科幻", "科幻片"],
    "恐怖片": ["恐怖", "惊悚", "恐怖片", "惊悚片"],
    "剧情片": ["剧情", "剧情片"],
    "战争片": ["战争", "战争片"],
    "悬疑片": ["悬疑", "犯罪", "悬疑片", "犯罪片"],
    "奇幻片": ["奇幻", "奇幻片", "灾难片"],
    "动画电影": ["动画片", "动漫电影"],

    # 电视剧子类 (重中之重)
    "国产剧": ["国产剧", "内地剧", "大陆剧"],
    "欧美剧": ["欧美剧", "美剧"],
    "韩剧": ["韩剧", "韩国剧"],
    "日剧": ["日剧", "日本剧"],
    "香港剧": ["香港剧", "港剧", "港澳剧"],
    "台湾剧": ["台湾剧", "台剧"],
    "泰剧": ["泰剧", "马泰剧"],

    # 综艺子类
    "大陆综艺": ["大陆综艺", "内地综艺"],
    "港台综艺": ["港台综艺"],
    "日韩综艺": ["日韩综艺"],
    "欧美综艺": ["欧美综艺"],

    # 动漫子类
    "国产动漫": ["国产动漫", "中国动漫", "国漫"],
    "日本动漫": ["日本动漫", "日漫"],
    "欧美动漫": ["欧美动漫"]
}

def get_category_pages(cat_name):
    """根据分类热门程度分配拉取页数"""
    if cat_name in ["国产剧", "动作片", "喜剧片", "科幻片"]:
        return 6  # 核心大类拉取 6 页 (300 部)
    elif cat_name in ["欧美剧", "韩剧", "爱情片", "悬疑片", "国产动漫", "日本动漫", "大陆综艺"]:
        return 4  # 热门次类拉取 4 页 (200 部)
    else:
        return 3  # 其他常规类拉取 3 页 (150 部)

def crawl_site_category(site, cat_name, type_id, max_pages):
    site_key = site['key']
    site_name = site['name']
    api_base = site['api']
    latency = site.get('total_latency', 2000)
    sep = '&' if '?' in api_base else '?'

    synced_count = 0
    for pg in range(1, max_pages + 1):
        url = f"{api_base}{sep}ac=detail&t={type_id}&pg={pg}&pagesize=50"
        data = fetch_json(url, timeout=7)
        if not data or 'list' not in data or not data['list']:
            break
        
        items = data['list']
        for item in items:
            try:
                if upsert_item(item, site_key, site_name, latency):
                    synced_count += 1
            except Exception:
                pass
        
        # 如果已经到了最后一页
        pagecount = int(data.get('pagecount', 1))
        if pg >= pagecount:
            break
    
    return cat_name, synced_count

def run_deep_seed():
    init_db()
    t0 = time.time()
    log("=== 启动【全分类深度填充引擎】构建海量多线路影视库 ===")

    if not os.path.exists(VALID_FILE):
        log(f"未找到有效源列表文件: {VALID_FILE}")
        return

    with open(VALID_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    valid_sources = data.get('valid_sources', [])
    # 选取优质主力采集站
    priorities = ['hongniu', 'jisu', 'huya', 'ikun', 'jinying']
    target_sources = [s for s in valid_sources if s['key'] in priorities]
    if len(target_sources) < 3:
        target_sources = valid_sources[:5]

    tasks = []
    # 遍历每个站点，发现并绑定核心子分类
    for site in target_sources:
        api_base = site['api']
        sep = '&' if '?' in api_base else '?'
        list_url = f"{api_base}{sep}ac=list"
        data = fetch_json(list_url, timeout=5)
        if not data or 'class' not in data:
            continue
        
        classes = data['class']
        for c in classes:
            c_name = c.get('type_name', '').strip()
            c_id = c.get('type_id')
            if not c_id or not c_name:
                continue
            
            # 排除虚拟大类和成人分类
            if c_name in ['电影', '连续剧', '电视剧', '综艺', '动漫', '体育赛事', '足球', '篮球', '台球', '短剧', '伦理片']:
                continue
            
            # 匹配对应核心分类
            matched_key = None
            for core_cat, kws in CORE_CATEGORIES.items():
                if any(kw == c_name or kw in c_name for kw in kws):
                    matched_key = core_cat
                    break
            
            if matched_key:
                pages = get_category_pages(matched_key)
                tasks.append((site, matched_key, c_id, pages))

    log(f"共生成 {len(tasks)} 个分类抓取子任务，涉及 {len(target_sources)} 个主力站点")

    total_synced = 0
    cat_summary = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {
            executor.submit(crawl_site_category, site, cat_name, cid, pages): (site['name'], cat_name)
            for (site, cat_name, cid, pages) in tasks
        }
        for fut in as_completed(future_map):
            site_n, cat_n = future_map[fut]
            try:
                cat_name, count = fut.result()
                total_synced += count
                cat_summary[cat_name] = cat_summary.get(cat_name, 0) + count
                log(f"  [完成] 站点: {site_n} | 分类: 【{cat_n}】 | 入库/更新: {count} 部")
            except Exception as e:
                log(f"  [失败] 站点: {site_n} | 分类: 【{cat_n}】 | 错误: {e}")

    cost = round(time.time() - t0, 1)
    stats = get_stats()
    log(f"=== 全分类深度填充完成! 耗时: {cost}s ===")
    log(f"当前数据库总影片数: {stats['total_videos']} 部 | 播放线路总数: {stats['total_routes']} 条")
    log(f"各大分类数据分布: {stats['types']}")

if __name__ == '__main__':
    run_deep_seed()
