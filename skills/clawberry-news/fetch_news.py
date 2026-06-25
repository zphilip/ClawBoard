import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
import os
import socket

socket.setdefaulttimeout(12)

RSS_SOURCES = {
    '36氪': 'https://36kr.com/feed',
    'IT之家': 'https://www.ithome.com/rss/',
    'Solidot': 'https://www.solidot.org/index.rdf',
    'cnBeta': 'https://feeds.feedburner.com/cnbeta',
    'Engadget中文': 'https://chinese.engadget.com/rss.xml',
    '少数派': 'https://sspai.com/feed',
}

KEYWORDS = {
    '时事': ['政治', '国际', '外交', '政策', '政府', '选举', '中美', '中俄', '欧盟', '台湾', '朝鲜', '日本'],
    '财经': ['股市', '基金', '经济', '金融', '投资', '银行', '房产', 'A股', '上市', '财报', '营收', '央行', '美元'],
    '科技': ['AI', '人工智能', '芯片', '手机', '互联网', '新能源', '模型', 'GPT', '大模型', '英伟达', '华为', '苹果', '比亚迪', '小米', 'OpenAI'],
    '体育': ['足球', '篮球', '奥运', '世界杯', 'NBA', '中超', '球员', '比赛'],
    '娱乐': ['明星', '电影', '音乐', '综艺', '热播', '演唱会', '票房'],
    '社会': ['事故', '案件', '民生', '教育', '医疗', '暴雨', '地震', '高铁'],
}

def fetch_rss(url, source_name, limit=15):
    items = []
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
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
            if title is not None and title.text:
                items.append({
                    'title': title.text.strip(),
                    'url': (link.text or '').strip() if link is not None else '',
                    'source': source_name,
                })
    except Exception as e:
        print(f'WARN {source_name}: {type(e).__name__}: {e}', flush=True)
    return items

print('Fetching news...', flush=True)
all_news = []
for name, url in RSS_SOURCES.items():
    print(f'  -> {name}', flush=True)
    items = fetch_rss(url, name)
    all_news.extend(items)

# dedupe
seen = set()
unique = []
for n in all_news:
    if n['title'] not in seen:
        seen.add(n['title'])
        unique.append(n)
all_news = unique

print(f'Total unique: {len(all_news)} items', flush=True)

# categorize
categorized = {k: [] for k in KEYWORDS}
categorized['其他'] = []
for n in all_news:
    matched = False
    for cat, kws in KEYWORDS.items():
        if any(kw in n['title'] for kw in kws):
            categorized[cat].append(n)
            matched = True
            break
    if not matched:
        categorized['其他'].append(n)

# save — use OPENCLAW_WORKSPACE if set, otherwise current directory
output_dir = os.environ.get('OPENCLAW_WORKSPACE', os.getcwd())
date_str = datetime.now().strftime('%Y%m%d')
out_path = os.path.join(output_dir, f'news_{date_str}.md')

lines = [f'News Report - {datetime.now().strftime("%Y-%m-%d %H:%M")}', '']
for cat in ['时事', '财经', '科技', '体育', '娱乐', '社会', '其他']:
    items = categorized.get(cat, [])
    if items:
        lines.append(f'【{cat}】')
        for i, n in enumerate(items[:8], 1):
            lines.append(f'{i}. {n["title"]} ({n["source"]})')
        lines.append('')

with open(out_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f'Saved: {out_path}', flush=True)
print('', flush=True)
print('===== CATEGORIZED NEWS =====', flush=True)
for cat in ['时事', '财经', '科技', '体育', '娱乐', '社会', '其他']:
    items = categorized.get(cat, [])
    if items:
        print(f'\n[{cat}] {len(items)} items', flush=True)
        for n in items[:6]:
            print(f'  - {n["title"]} [{n["source"]}]', flush=True)