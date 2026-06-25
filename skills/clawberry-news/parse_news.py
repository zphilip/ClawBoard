"""Parse already-downloaded news data files into a readable summary.

Reads toutiao.json, zhihu.json, and 36kr.xml from the workspace
and prints a categorized summary to stdout.

Usage: python3 parse_news.py
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

WS = Path(os.environ.get('OPENCLAW_WORKSPACE', os.getcwd()))

# 1. Toutiao hot list
print('=== Toutiao Hot Search (Top 15) ===')
try:
    with open(WS / 'toutiao.json') as f:
        data = json.load(f)
    for i, item in enumerate(data.get('data', [])[:15], 1):
        title = item.get('Title', '').strip()
        hot = int(item.get('HotValue', 0))
        print(f'{i:2}. {title}  [hot {hot:,}]')
except FileNotFoundError:
    print('  (toutiao.json not found — run fetch first)')
except Exception as e:
    print(f'  (error reading toutiao.json: {e})')
print()

# 2. Zhihu
print('=== Zhihu Top Stories ===')
try:
    with open(WS / 'zhihu.json') as f:
        data = json.load(f)
    for i, item in enumerate(data.get('top_stories', [])[:5], 1):
        print(f"{i}. {item.get('title', '')}")
except FileNotFoundError:
    print('  (zhihu.json not found)')
except Exception as e:
    print(f'  (error reading zhihu.json: {e})')
print()

# 3. 36Kr
print('=== 36Kr Latest (Top 8) ===')
try:
    with open(WS / '36kr.xml') as f:
        root = ET.fromstring(f.read())
    items = root.findall('.//item')
    for i, item in enumerate(items[:8], 1):
        title = re.sub(r'<[^>]+>', '', item.findtext('title', '')).strip()
        pub = item.findtext('pubDate', '')
        if len(title) > 70:
            title = title[:70] + '...'
        print(f'{i}. {title}')
        print(f'   published: {pub}')
except FileNotFoundError:
    print('  (36kr.xml not found)')
except Exception as e:
    print(f'  (error reading 36kr.xml: {e})')
