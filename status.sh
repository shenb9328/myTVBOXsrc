#!/usr/bin/env bash
# 查看当前影视源维护状态与延迟排行
python3 -c "
import json
with open('/home/shenb/tvbox-service/valid_sources.json') as f:
    d = json.load(f)
print('='*70)
print(f'NAS影视点播源状态 | 更新时间: {d.get(\"update_time\")} | 健康源: {d.get(\"valid_count\")}/{d.get(\"total_tested\")}')
print('='*70)
print(f'{\"排名\":<4} {\"站点名称\":<18} {\"综合延迟\":<10} {\"首包延迟\":<10} {\"片库数量\":<10} {\"测试样例\"}')
print('-'*70)
for idx, s in enumerate(d.get('valid_sources', []), 1):
    print(f'#{idx:<3} {s[\"name\"]: <16} {s[\"total_latency\"]:>5}ms {s[\"stream_latency\"]:>7}ms {s[\"total_movies\"]:>9}条   {s[\"sample_title\"][:12]}')
print('='*70)
"
