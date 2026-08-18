# MEMORY.md — Long-Term Memory

## 雇主基本画像
- **核心焦点:** [填写当前阶段的重心，例如：筹备新公司 / 专注健康管理]
- **高效时间段:** [例：上午 9:00–12:00 深度思考，不排会]

## 关键关系人矩阵
- **家人:** [姓名/关系/特殊提醒]
- **核心业务伙伴:** [姓名/公司/对接习惯]

## 个人生活偏好
- **差旅:** 酒店首选[某品牌]，国内航班只坐[某航]，不吃[某种食物]。
- **设备:** 常用手机为 iOS，工作环境为 macOS，文件传输习惯用 Markdown/PDF。
- 回答前显示「思考中」提示
- 即使执行失败也要尽可能给出结果

## 技术配置
- `uv` v0.11.3 已安装

## 移动设备状态
- **上次检测到的手机设备:** (由心跳任务更新，例：`192.168.1.100:5555` 或 `emulator-5554`)
- **连接状态:** PHONE_CONNECTED=false  ← 心跳任务会自动更新此字段
- **操作规则:** 当 PHONE_CONNECTED=true 时，用户的"打开/操作/发送/截图"等指令默认指向已连接的手机，使用 mobile-control skill 执行

## 智能家居状态
- **ha-lite 服务器:** `http://localhost:8090` (Pi Zero)
- **云端认证:** CLOUD_AUTHED=unknown  ← 心跳任务会自动更新此字段
- **OAuth 认证:** OAUTH_AUTHED=unknown  ← 心跳任务会自动更新此字段
- **设备数量:** N/A  ← 心跳任务会自动更新
- **上次同步:** N/A
- **操作规则:** 当用户发出"打开/关闭/调节/查询"等智能家居指令时，使用 xiaomi-home skill (`halite_control.py`) 通过 ha-lite API 控制设备
- **兜底方案:** 当 `CLOUD_AUTHED=false` 且 `OAUTH_AUTHED=false` 时，使用 Xiaomi-Token-Extractor 两阶段 QR 登录流程（Phase 1 获取 QR → 雇主扫码 → Phase 2 `--collect` 采集 Token → `POST /api/devices/import` 导入 ha-lite）。详见 AGENT.md「Token Refresh Fallback」。

## 持续更新的待办快照 (Todo)
→ 详见 `memory/todo.md`

## Decisions & Preferences
[由凌音逐步积累]

## Lessons Learned
[由凌音逐步积累]