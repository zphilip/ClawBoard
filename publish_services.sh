#!/bin/bash

# --- 配置区 ---
PICOCLAW_CONFIG="/var/lib/picoclaw/.picoclaw/config.json" # 请确认实际路径
DASHBOARD_PORT=8080
ZEROCLAW_PORT=42617
PICOCLAW_PORT=18790

# --- 1. 提取 ZeroClaw Pair Code ---
# 执行命令并精准提取被 │ 包围的 6 位数字
ZEROCLAW_OUTPUT=$(zeroclaw gateway get-paircode 2>&1)
PAIR_CODE=$(echo "$ZEROCLAW_OUTPUT" | grep -o '[0-9]\{6\}' | head -n 1)

if [ -z "$PAIR_CODE" ]; then
    PAIR_ATTR="status=paired_or_locked"
    echo "ZeroClaw: 未发现配对码，设置为锁定状态。"
else
    PAIR_ATTR="pair_code=$PAIR_CODE"
    echo "ZeroClaw: 提取到配对码 $PAIR_CODE"
fi

# --- 2. 提取 Picoclaw Token ---
if [ -f "$PICOCLAW_CONFIG" ]; then
    # 注意这里改成了 .pico.token
    PICO_TOKEN=$(jq -r '.channels.pico.token' "$PICOCLAW_CONFIG")
    
    if [ "$PICO_TOKEN" == "null" ] || [ -z "$PICO_TOKEN" ]; then
        PICO_ATTR="status=no_token"
        echo "Picoclaw: 配置文件存在，但未找到 Token 字段。"
    else
        PICO_ATTR="token=$PICO_TOKEN"
        # 添加下面这一行用于显示调试信息
        echo "Picoclaw: 提取到 Token: ${PICO_TOKEN:0:8}..." 
    fi
else
    PICO_ATTR="status=config_missing"
    echo "Picoclaw: 未找到配置文件 $PICOCLAW_CONFIG"
fi

# --- 3. 启动后台广播 ---
# 清理可能存在的旧进程
pkill -f avahi-publish || true

echo "正在启动 ClawBerry 专属服务发现..."

# 服务 1: Dashboard (HTTP 控制台)
avahi-publish -s "ClawBerry Dashboard" _clawberry._tcp $DASHBOARD_PORT "type=web_ui" "path=/" &

# 服务 2: ZeroClaw Gateway (含动态 Pair Code)
avahi-publish -s "ZeroClaw Gateway" _clawberry._tcp $ZEROCLAW_PORT "type=gateway" "$PAIR_ATTR" &

# 服务 3: Picoclaw Gateway (含动态 Token)
avahi-publish -s "Picoclaw Gateway" _clawberry._tcp $PICOCLAW_PORT "type=picoclaw" "$PICO_ATTR" &

# 保持脚本运行
wait
