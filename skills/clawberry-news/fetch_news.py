"""clawberry-news — Chinese news aggregator via RSS + web fallback.

Fetches from diverse sources covering general, international, social,
technology, finance, sports, and entertainment news.  RSS-first with
graceful web_fetch fallback for sites without RSS.

Usage: python3 fetch_news.py
Output: news_YYYYMMDD.md in OPENCLAW_WORKSPACE (or current directory)
"""
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
import os
import socket
import re
import json

socket.setdefaulttimeout(12)

# ── RSS sources (reliable feeds, mixed categories) ────────────────────────
RSS_SOURCES = {
    # ── General / Current Affairs ──
    '人民网-国内': 'http://www.people.com.cn/rss/politics.xml',
    '人民网-社会': 'http://www.people.com.cn/rss/society.xml',
    '中新网-国内': 'https://www.chinanews.com.cn/rss/scroll-news.xml',
    # ── International ──
    '环球网-国际': 'https://www.huanqiu.com/rss/world.xml',
    '人民网-国际': 'http://www.people.com.cn/rss/world.xml',
    # ── Finance / Economy ──
    '人民网-财经': 'http://www.people.com.cn/rss/finance.xml',
    # ── Technology ──
    '36氪': 'https://36kr.com/feed',
    'IT之家': 'https://www.ithome.com/rss/',
    'Solidot': 'https://www.solidot.org/index.rdf',
    'cnBeta': 'https://feeds.feedburner.com/cnbeta',
    '少数派': 'https://sspai.com/feed',
    # ── Sports ──
    '人民网-体育': 'http://www.people.com.cn/rss/sports.xml',
    # ── Entertainment / Culture ──
    '人民网-文化': 'http://www.people.com.cn/rss/culture.xml',
}

# ── Web-fetch fallback sources (for sites without RSS) ────────────────────
# Each entry: (display_name, url, category_hint)
WEB_FALLBACK = [
    ('新浪-国内', 'https://news.sina.com.cn/', '时事'),
    ('新浪-国际', 'https://news.sina.com.cn/world/', '时事'),
    ('新浪-社会', 'https://news.sina.com.cn/society/', '社会'),
    ('网易新闻', 'https://news.163.com/', '时事'),
    ('搜狐新闻', 'https://news.sohu.com/', '时事'),
]

# ── Keyword categorisation ─────────────────────────────────────────────────
KEYWORDS = {
    '时事': [
        '政治', '国际', '外交', '政策', '政府', '选举', '中美', '中俄',
        '欧盟', '台湾', '朝鲜', '日本', '韩国', '印度', '俄罗斯', '美国',
        '北约', '联合国', '峰会', '出访', '会谈', '制裁', '协议', '冲突',
        '谈判', '领土', '南海', '台海', '军事', '国防', '军队',
    ],
    '财经': [
        '股市', '基金', '经济', '金融', '投资', '银行', '房产', 'A股',
        '上市', '财报', '营收', '央行', '美元', '人民币', '汇率', 'GDP',
        '通胀', '利率', '债市', '期货', '外汇', '保险', '信托',
    ],
    '科技': [
        'AI', '人工智能', '芯片', '手机', '互联网', '新能源', '大模型',
        'GPT', 'OpenAI', '英伟达', '华为', '苹果', '比亚迪', '小米',
        '机器人', '自动驾驶', '航天', '卫星', '5G', '6G', '量子',
    ],
    '体育': [
        '足球', '篮球', '奥运', '世界杯', 'NBA', '中超', '球员', '比赛',
        '联赛', '冠军', '决赛', '晋级', '转会', '网球', 'F1', '游泳',
    ],
    '娱乐': [
        '明星', '电影', '音乐', '综艺', '热播', '演唱会', '票房',
        '电视剧', '综艺节目', '八卦', '院线', '艺人', '播出',
    ],
    '社会': [
        '事故', '案件', '民生', '教育', '医疗', '暴雨', '地震', '高铁',
        '高考', '房价', '物价', '养老', '医保', '环保', '交通', '天气',
        '灾害', '救援', '犯罪', '判刑', '法院', '公安', '消防',
    ],
}

# ── Sentiment keywords ─────────────────────────────────────────────────────
SENTIMENT = {
    'positive': ['增长', '超预期', '突破', '中标', '创新', '领先', '利好',
                 '新高', '获批', '上市', '盈利', '增长', '提升', '改善'],
    'negative': ['下滑', '不及预期', '亏损', '监管', '处罚', '事故', '暴跌',
                 '裁员', '违约', '暴雷', '造假', '调查', '诉讼', '危机'],
}
SENTIMENT['neutral'] = []  # everything else


def fetch_rss(url, source_name, limit=15):
    """Fetch and parse an RSS/Atom feed. Returns list of {title, url, source}."""
    items = []
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('gbk', errors='ignore')
        root = ET.fromstring(text)
        for item in root.findall('.//item')[:limit]:
            title = item.find('title')
            link = item.find('link')
            if title is not None and title.text and len(title.text.strip()) > 3:
                items.append({
                    'title': title.text.strip(),
                    'url': (link.text or '').strip() if link is not None else '',
                    'source': source_name,
                })
    except Exception as e:
        print(f'WARN {source_name}: {type(e).__name__}: {e}', flush=True)
    return items


def fetch_web_titles(url, source_name, limit=10):
    """Minimal web-scrape: extract <a> tag text from HTML as headline candidates.
    Used as fallback when no RSS feed is available."""
    items = []
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('gbk', errors='ignore')
        # Extract text from <a> tags that look like headlines (> 10 chars, no JS, http link)
        pattern = re.compile(
            r'<a[^>]*href="(https?://[^"]*)"[^>]*>\s*([^<]{10,80}?)\s*</a>',
            re.IGNORECASE,
        )
        seen = set()
        for m in pattern.finditer(html):
            link = m.group(1)
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            # Skip nav/script/style links that are too generic
            skip_words = ('登录', '注册', '首页', '更多', '查看详情', '点击',
                         'javascript', '关于我们', '广告', 'English', '繁体',
                         '手机版', '客户端', 'APP', '下一页', '上一页')
            if title and len(title) > 8 and title not in seen:
                if not any(w in title for w in skip_words):
                    seen.add(title)
                    items.append({
                        'title': title,
                        'url': link,
                        'source': source_name,
                    })
        items = items[:limit]
    except Exception as e:
        print(f'WARN {source_name}: {type(e).__name__}: {e}', flush=True)
    return items


def classify_sentiment(title):
    """Simple keyword-based sentiment classification."""
    for word in SENTIMENT['positive']:
        if word in title:
            return 'positive'
    for word in SENTIMENT['negative']:
        if word in title:
            return 'negative'
    return 'neutral'


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

print('Fetching news...', flush=True)
all_news = []

# 1. RSS sources
for name, url in RSS_SOURCES.items():
    print(f'  -> RSS {name}', flush=True)
    items = fetch_rss(url, name)
    all_news.extend(items)

# 2. Web fallback for sites without RSS
for name, url, _cat in WEB_FALLBACK:
    print(f'  -> web {name}', flush=True)
    items = fetch_web_titles(url, name)
    all_news.extend(items)

# Dedupe by title similarity (simple exact match + substring containment)
seen = set()
unique = []
for n in all_news:
    key = n['title']
    if key not in seen:
        # Also check if any existing title contains this or vice versa
        duplicate = False
        for existing in unique:
            if key in existing['title'] or existing['title'] in key:
                duplicate = True
                break
        if not duplicate:
            seen.add(key)
            unique.append(n)
all_news = unique

print(f'Total unique: {len(all_news)} items', flush=True)

# Categorise with sentiment
categorized = {k: [] for k in KEYWORDS}
categorized['其他'] = []
for n in all_news:
    n['sentiment'] = classify_sentiment(n['title'])
    matched = False
    for cat, kws in KEYWORDS.items():
        if any(kw in n['title'] for kw in kws):
            categorized[cat].append(n)
            matched = True
            break
    if not matched:
        categorized['其他'].append(n)

# ── Save report ────────────────────────────────────────────────────────────
output_dir = os.environ.get('OPENCLAW_WORKSPACE', os.getcwd())
date_str = datetime.now().strftime('%Y%m%d')
out_path = os.path.join(output_dir, f'news_{date_str}.md')

sent_map = {'positive': '🔺', 'negative': '🔻', 'neutral': '  '}

lines = [
    f'# 📰 ClawBerry News — {datetime.now().strftime("%Y-%m-%d %H:%M")}',
    '',
    f'> RSS + web fetch | {len(all_news)} items | {len(categorized)} categories',
    '',
]

category_order = ['时事', '国际', '财经', '科技', '体育', '娱乐', '社会', '其他']
for cat in category_order:
    items = categorized.get(cat, [])
    if not items:
        continue
    # Count sentiments
    pos = sum(1 for n in items if n['sentiment'] == 'positive')
    neg = sum(1 for n in items if n['sentiment'] == 'negative')
    lines.append(f'## {cat}  ({len(items)} items  🔺{pos} 🔻{neg})')
    lines.append('')
    for i, n in enumerate(items[:8], 1):
        s = sent_map.get(n['sentiment'], '  ')
        lines.append(f'{i}. {s} {n["title"]}')
        lines.append(f'   *{n["source"]}*  ')
    lines.append('')

# ── Sources with zero results ──────────────────────────────────────────────
empty_sources = []
for name in RSS_SOURCES:
    if not any(n['source'] == name for n in all_news):
        empty_sources.append(name)
for name, _, _ in WEB_FALLBACK:
    if not any(n['source'] == name for n in all_news):
        empty_sources.append(f'{name} (web)')
if empty_sources:
    lines.append('## ⚠ Sources with no results')
    lines.append('')
    for s in empty_sources:
        lines.append(f'- {s}')
    lines.append('')

with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f'Saved: {out_path}', flush=True)
print('', flush=True)

# ── Stdout summary ─────────────────────────────────────────────────────────
print('===== CATEGORIZED NEWS =====', flush=True)
for cat in category_order:
    items = categorized.get(cat, [])
    if items:
        print(f'\n[{cat}] {len(items)} items', flush=True)
        for n in items[:6]:
            s = sent_map.get(n['sentiment'], '  ')
            print(f'  {s} {n["title"]} [{n["source"]}]', flush=True)
if empty_sources:
    print(f'\n⚠ No results from: {len(empty_sources)} sources', flush=True)
