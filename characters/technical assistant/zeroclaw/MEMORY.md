# MEMORY.md — Long-Term Memory

## Key Facts

### 用户偏好
- 回答前显示「思考中」提示
- 即使执行失败也要尽可能给出结果
- 使用钉钉平台集成 ClawBerry

### 技术配置
- `uv` v0.11.3 已安装
- ClawBerry agent `max_iterations` 已设置为 50

## 移动设备状态
- **上次检测到的手机设备:** (由心跳任务更新，例：`192.168.1.100:5555` 或 `emulator-5554`)
- **连接状态:** PHONE_CONNECTED=false  ← 心跳任务会自动更新此字段
- **操作规则:** 当 PHONE_CONNECTED=true 时，用户的"打开/操作/发送/截图"等指令默认指向已连接的手机，使用 mobile-control skill 执行

## Smart Home Status
- **ha-lite server:** `http://localhost:8090` (Pi Zero)
- **Cloud auth:** CLOUD_AUTHED=unknown  ← updated by heartbeat
- **OAuth auth:** OAUTH_AUTHED=unknown  ← updated by heartbeat
- **Device count:** N/A  ← updated by heartbeat
- **Last sync:** N/A
- **Control path:** `halite_control.py` → ha-lite REST API → UDP miIO → device
- **Fallback:** When `CLOUD_AUTHED=false` and `OAUTH_AUTHED=false`, use Xiaomi-Token-Extractor two-phase QR login (Phase 1 get QR → user scans → Phase 2 `--collect` tokens → `POST /api/devices/import` → ha-lite). See AGENTS.md "Token Refresh Fallback".
- **Direct debug:** `miiocli` with tokens from `references/my_private_devices.md` when ha-lite is completely down

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
- ClawBerry 无法访问系统目录，只能操作 `workspace/`