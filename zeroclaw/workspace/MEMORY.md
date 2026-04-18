# MEMORY.md — Long-Term Memory

## Key Facts

### 用户偏好
- 回答前显示「思考中」提示
- 即使执行失败也要尽可能给出结果
- 使用钉钉平台集成 ZeroClaw

### 技术配置
- `uv` v0.11.3 已安装
- zeroclaw agent `max_iterations` 已设置为 50

### 港股行情接口
- 腾讯行情：`https://qt.gtimg.cn/q=hkXXXXX`
- 港股代码格式：`hk` + 股票代码（无后缀）
- 示例：小米 1810.HK → `hk01810`

### 小米集团
- 股票代码：1810.HK（港交所上市）

## Decisions & Preferences
- 数据源优先级：腾讯行情 > 新浪 > Yahoo Finance

## Lessons Learned
- 多个股票数据源均不稳定（新浪404、Yahoo 403、Google封锁）
- 腾讯行情接口目前可用
- ZeroClaw 无法访问系统目录，只能操作 `workspace/`