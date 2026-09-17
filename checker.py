#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能影视点播源巡检、去重、测活与测速清洗引擎
"""

import os
import sys
import json
import time
import re
import ssl
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_FILE = os.path.join(CURRENT_DIR, "sources_pool.json")
OUTPUT_VALID_FILE = os.path.join(CURRENT_DIR, "valid_sources.json")
OUTPUT_VOD_JSON = os.path.join(CURRENT_DIR, "vod.json")
OUTPUT_TVBOX_JSON = os.path.join(CURRENT_DIR, "tvbox.json")

# 忽略不合规的 SSL 证书，防止自签名证书导致误杀
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*'
}

TIMEOUT_API = 4.0      # API 请求超时阈值 (秒)
TIMEOUT_STREAM = 4.0   # 视频流请求超时阈值 (秒)

def normalize_url(url: str) -> str:
    """处理中文域名为 punycode"""
    p = urllib.parse.urlsplit(url)
    try:
        hostname = p.hostname.encode('idna').decode('ascii') if p.hostname else ''
    except Exception:
        hostname = p.hostname or ''
    netloc = hostname
    if p.port:
        netloc += f':{p.port}'
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))

def probe_single_source(source: dict) -> dict:
    """对单个候选源执行深度体检：API连通性、JSON解析、最新片源提取、真实m3u8首包测速与合规验证"""
    name = source.get('name', '未命名')
    raw_api = source.get('api', '').strip()
    play_type = source.get('play_type', 'detail')
    
    result = {
        'key': source.get('key', ''),
        'name': name,
        'type': source.get('type', 1),
        'api': raw_api,
        'category': source.get('category', '综合影视'),
        'searchable': source.get('searchable', 1),
        'quickSearch': source.get('quickSearch', 1),
        'filterable': source.get('filterable', 1),
        'status': 'fail',
        'api_latency': 9999,
        'stream_latency': 9999,
        'total_latency': 9999,
        'total_movies': 0,
        'sample_title': '',
        'sample_m3u8': '',
        'error_reason': ''
    }

    if not raw_api:
        result['error_reason'] = 'API 地址为空'
        return result

    # 1. 拼接探测 URL
    sep = '&' if '?' in raw_api else '?'
    if play_type == 'videolist' or 'bfzyapi' in raw_api or 'lziapi' in raw_api:
        probe_url = f"{raw_api}{sep}ac=videolist&pagesize=1"
    else:
        probe_url = f"{raw_api}{sep}ac=detail&pagesize=1"

    probe_url_norm = normalize_url(probe_url)

    # 2. 阶段一：API 连通性与 JSON 有效性
    t0 = time.time()
    try:
        req = urllib.request.Request(probe_url_norm, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT_API, context=SSL_CTX) as resp:
            content = resp.read()
            api_cost = int((time.time() - t0) * 1000)
            result['api_latency'] = api_cost
            
            if resp.status != 200:
                result['error_reason'] = f'HTTP 状态码异常: {resp.status}'
                return result

            try:
                data = json.loads(content.decode('utf-8', errors='ignore'))
            except Exception as je:
                result['error_reason'] = f'返回非标准 JSON: {je}'
                return result

            vlist = data.get('list', [])
            total = data.get('total') or len(vlist)
            result['total_movies'] = total

            if not vlist or not isinstance(vlist, list):
                result['error_reason'] = '影视列表为空或无内容'
                return result

            item = vlist[0]
            title = item.get('vod_name', '未知片目')
            play_url_str = item.get('vod_play_url', '')
            result['sample_title'] = title

    except Exception as e:
        api_cost = int((time.time() - t0) * 1000)
        result['api_latency'] = api_cost
        result['error_reason'] = f'API 请求失败: {str(e)}'
        return result

    # 3. 阶段二：提取实际播放 m3u8 地址
    m = re.search(r'https?://[^\$,#\s\r\n]+\.m3u8', play_url_str)
    if not m:
        # 有些源可能是 mp4 或纯播放链接
        m_mp4 = re.search(r'https?://[^\$,#\s\r\n]+\.mp4', play_url_str)
        if not m_mp4:
            result['error_reason'] = '未找到直接可播放的流媒体地址 (m3u8/mp4)'
            return result
        stream_url = m_mp4.group(0)
        is_hls_expected = False
    else:
        stream_url = m.group(0)
        is_hls_expected = True

    result['sample_m3u8'] = stream_url

    # 4. 阶段三：测试真实视频流的首包响应与可播性
    t_stream = time.time()
    try:
        stream_url_norm = normalize_url(stream_url)
        sreq = urllib.request.Request(stream_url_norm, headers={
            'User-Agent': HEADERS['User-Agent'],
            'Range': 'bytes=0-4096'
        })
        with urllib.request.urlopen(sreq, timeout=TIMEOUT_STREAM, context=SSL_CTX) as sresp:
            stream_cost = int((time.time() - t_stream) * 1000)
            result['stream_latency'] = stream_cost
            header_bytes = sresp.read(2048)

            if is_hls_expected:
                if b'#EXTM3U' not in header_bytes:
                    result['error_reason'] = '流地址返回非法非 HLS 格式 (防盗链或网页重定向)'
                    return result

            # 通过所有测试！
            result['status'] = 'ok'
            result['total_latency'] = result['api_latency'] + result['stream_latency']

    except Exception as se:
        stream_cost = int((time.time() - t_stream) * 1000)
        result['stream_latency'] = stream_cost
        result['error_reason'] = f'视频流连接超时或无法播放: {str(se)}'
        return result

    return result

def deduplicate_sources(sources: list) -> list:
    """去重逻辑：按 API 主机名 + 路径去重，同域名下只保留一个最优质或最完整的源"""
    seen_endpoints = set()
    seen_keys = set()
    unique = []

    for s in sources:
        api = s.get('api', '').strip()
        if not api:
            continue
        try:
            p = urllib.parse.urlsplit(api)
            # 提取标准化主路径
            clean_host_path = f"{p.hostname}{p.path}".rstrip('/')
        except Exception:
            clean_host_path = api

        key = s.get('key') or clean_host_path
        if clean_host_path in seen_endpoints or key in seen_keys:
            continue

        seen_endpoints.add(clean_host_path)
        seen_keys.add(key)
        unique.append(s)

    return unique

def run_inspection() -> dict:
    """执行完整的源池清洗与检测流程"""
    start_time = time.time()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行点播源清洗与测速任务...")

    if not os.path.exists(POOL_FILE):
        print(f"错误: 种子源池文件 {POOL_FILE} 不存在!")
        return {"ok_sources": [], "fail_sources": [], "timestamp": time.time()}

    with open(POOL_FILE, 'r', encoding='utf-8') as f:
        pool = json.load(f)

    # 1. 静态去重
    unique_pool = deduplicate_sources(pool)
    print(f"种子源总数: {len(pool)}，经唯一性去重后待测源: {len(unique_pool)}")

    valid_results = []
    fail_results = []

    # 2. 多线程高并发探测
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_map = {executor.submit(probe_single_source, s): s for s in unique_pool}
        for future in as_completed(future_map):
            res = future.result()
            if res['status'] == 'ok':
                valid_results.append(res)
                print(f"  [✓ 有效] {res['name']:<14} | 延迟: {res['total_latency']:>4}ms (API:{res['api_latency']}ms + 流:{res['stream_latency']}ms) | 片量: {res['total_movies']}")
            else:
                fail_results.append(res)
                print(f"  [✗ 剔除] {res['name']:<14} | 原因: {res['error_reason']}")

    # 3. 按照综合延迟从低到高排序（最快最流畅的排在最前）
    valid_results.sort(key=lambda x: x['total_latency'])

    duration = round(time.time() - start_time, 2)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = {
        "update_time": now_str,
        "duration_seconds": duration,
        "total_tested": len(unique_pool),
        "valid_count": len(valid_results),
        "fail_count": len(fail_results),
        "valid_sources": valid_results,
        "fail_sources": fail_results
    }

    # 4. 保存体检明细
    with open(OUTPUT_VALID_FILE, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 5. 生成标准 TVBox / 影视仓点播聚合配置 (vod.json & tvbox.json)
    LAN_IP = "192.168.0.116"

    # 第一优先级：智能大类聚合专线（彻底解决采集站父类空数据问题，顶部分类统一为【电影】【电视】【综艺】【动漫】，片目丰盈）
    proxy_sites = []
    for s in valid_results:
        proxy_sites.append({
            "key": f"proxy_{s['key']}",
            "name": f"🎬 {s['name']} [电影·电视·综艺·动漫]",
            "type": 1,
            "api": f"http://{LAN_IP}:5888/proxy/{s['key']}",
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 0
        })

    # 第二优先级：原生直连源（保留供特殊需要使用）
    raw_sites = []
    for s in valid_results:
        raw_sites.append({
            "key": f"raw_{s['key']}",
            "name": f"⚡ {s['name']} [原生细分分类]",
            "type": 1,
            "api": s['api'],
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1
        })

    # 顶级第一优先级：本地轻量 SQLite 超级私有聚合源（单片多线路容灾 + 多维筛选方案B + 毫秒级极速响应）
    super_site = [{
        "key": "nas_super_vod",
        "name": "🎬 我的私有影库 [全网聚合·多线秒播]",
        "type": 1,
        "api": f"http://{LAN_IP}:5888/api/vod",
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 1
    }]

    all_sites = super_site + proxy_sites + raw_sites

    tvbox_config = {
        "spider": "",
        "wallpaper": "https://bing.img.run/rand_uhd.php",
        "warningText": f"NAS本地专线维护源 | 更新时间: {now_str} | 已筛选 {len(valid_results)} 条极速点播源",
        "sites": all_sites,
        "parses": [
            {
                "name": "聚合解析1",
                "type": 0,
                "url": "https://jx.jsonplayer.com/player/?url="
            },
            {
                "name": "聚合解析2",
                "type": 0,
                "url": "https://jx.xmflv.com/?url="
            },
            {
                "name": "聚合解析3",
                "type": 0,
                "url": "https://jx.aidouer.net/?url="
            }
        ],
        "rules": [
            {
                "name": "通用去广告",
                "hosts": [
                    "vip.ffzy-play.com",
                    "vip.ffzy-online.com",
                    "vip.lz-cdn.com"
                ],
                "regex": [
                    "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:6.666667,[\\s\\S]*?#EXT-X-DISCONTINUITY"
                ]
            }
        ],
        "doh": [
            {
                "name": "AliDNS",
                "url": "https://dns.alidns.com/dns-query",
                "ips": ["223.5.5.5", "223.6.6.6"]
            },
            {
                "name": "TencentDNS",
                "url": "https://doh.pub/dns-query",
                "ips": ["119.29.29.29"]
            }
        ]
    }

    with open(OUTPUT_VOD_JSON, 'w', encoding='utf-8') as f:
        json.dump(tvbox_config, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_TVBOX_JSON, 'w', encoding='utf-8') as f:
        json.dump(tvbox_config, f, ensure_ascii=False, indent=2)

    print(f"\n清洗完成! 耗时: {duration}s | 优质可用源: {len(valid_results)} 个 | 淘汰失效/慢速源: {len(fail_results)} 个")
    print(f"已生成 TVBox 点播源: {OUTPUT_VOD_JSON}")

    return summary

if __name__ == '__main__':
    run_inspection()
