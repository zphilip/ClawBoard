---
name: clawberry-news
description: Chinese news aggregator — RSS + web fallback. Covers general affairs, international, social, city, tech, finance, sports, entertainment. Sources include 人民网, 环球网, 中新网, 新浪, 网易, 36Kr, IT之家 and more. Generates a categorized, sentiment-tagged news report. 新闻资讯、时事热点、国际新闻、社会新闻、城市新闻。
version: 1.1.0
license: MIT-0
metadata: {"openclaw": {"emoji": "📰", "requires": {"bins": ["python3"], "env": []}}}
dependencies: ""
---

# clawberry-news

Multi-source Chinese news aggregator. RSS-first with web-fallback for sites without feeds. Run `python3 fetch_news.py`.

## Features

- 📡 **RSS + web dual-mode**: 16 sources across 7 categories, with HTML scraping fallback
- 📂 **Smart categorization**: current-affairs, international, finance, tech, sports, entertainment, society
- 🔺🔻 **Sentiment tagging**: positive/negative/neutral per headline
- 📰 **Dated markdown report**: writes `news_YYYYMMDD.md` to workspace
- ⚠ **Empty-source report**: lists sources that returned no results
- ⚡ **No dependencies**: Python 3 stdlib only

## Trigger Conditions

- "给我今天的新闻" / "Today's news"
- "有什么热点新闻" / "What's trending"
- "国际新闻" / "International news"
- "社会新闻" / "Social news"
- "财经新闻" / "Financial news"
- "科技新闻" / "Tech news"
- "收集新闻" / "重新收集新闻"

## News Sources

### RSS (reliable feeds)

| Source | Focus |
|--------|-------|
| 人民网 (people.com.cn) | 国内·社会·国际·财经·体育·文化 |
| 环球网 (huanqiu.com) | 国际新闻 |
| 中新网 (chinanews.com.cn) | 综合国内新闻 |
| 36Kr (36kr.com) | 科技/创投 |
| IT之家 (ithome.com) | 科技数码 |
| Solidot (solidot.org) | 科技 |
| cnBeta | 科技 |
| 少数派 (sspai.com) | 数字生活 |

### Web fallback (HTML scrape for sites without RSS)

| Source | Focus |
|--------|-------|
| 新浪新闻 (news.sina.com.cn) | 国内·国际·社会 |
| 网易新闻 (news.163.com) | 综合新闻 |
| 搜狐新闻 (news.sohu.com) | 综合新闻 |

## Usage

```bash
python3 fetch_news.py
```

Output: `$OPENCLAW_WORKSPACE/news_YYYYMMDD.md` (falls back to current directory).

## Security

- ✅ No API keys required
- ✅ No data uploaded to external servers
- ✅ Local processing only
- ⚠️ Requires internet access
