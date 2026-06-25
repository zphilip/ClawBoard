---
name: clawberry-news
description: Chinese news aggregator via RSS. Fetches latest news from 36Kr, IT之家, Solidot, cnBeta, Engadget Chinese, SSpai. Categorizes into current-affairs, finance, tech, sports, entertainment, society. Generates a dated news report. Use when user asks for news, current events, or headlines. 新闻资讯、热点新闻、每日新闻。
version: 1.0.3
license: MIT-0
metadata: {"openclaw": {"emoji": "📰", "requires": {"bins": ["python3"], "env": []}}}
dependencies: ""
---

# clawberry-news

Chinese news aggregator via RSS. Run `python3 fetch_news.py` to collect and categorize the latest news.

## Features

- 📡 **RSS aggregation**: 36Kr, IT之家, Solidot, cnBeta, Engadget Chinese, SSpai
- 📂 **Smart categorization**: current affairs, finance, tech, sports, entertainment, society
- 📰 **Dated report**: writes `news_YYYYMMDD.md` to workspace
- ⚡ **No dependencies**: Python stdlib only (urllib, xml.etree)

## Trigger Conditions

- "给我今天的新闻" / "Today's news"
- "有什么热点新闻" / "What's trending"
- "看看科技新闻" / "Tech news"
- "财经新闻" / "Financial news"
- "收集新闻" / "重新收集新闻"

## News Sources

| Source | URL | Type |
|--------|-----|------|
| 36Kr | 36kr.com/feed | Tech/startup |
| IT之家 | ithome.com/rss/ | Tech |
| Solidot | solidot.org/index.rdf | Tech |
| cnBeta | feeds.feedburner.com/cnbeta | Tech |
| Engadget Chinese | chinese.engadget.com/rss.xml | Tech |
| SSpai | sspai.com/feed | Tech/digital |

## Usage

```bash
python3 fetch_news.py
```

Output is written to `$OPENCLAW_WORKSPACE/news_YYYYMMDD.md` (falls back to current directory).

## Prerequisites

- Python 3 with stdlib only (no pip install needed)
- Internet access for RSS fetching

## Security Notes

- ✅ No API keys required
- ✅ No data uploaded to external servers
- ✅ Local processing only
- ⚠️ Requires internet access to fetch news
