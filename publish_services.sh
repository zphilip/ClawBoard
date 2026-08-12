#!/bin/bash
# publish_services.sh — ClawBerry mDNS service publisher
#
# WHY XML FILES instead of avahi-publish:
#   avahi-publish is a D-Bus client. When avahi-daemon restarts (DHCP renewal,
#   network interface bounce), all avahi-publish processes lose their connection
#   and exit, causing services to vanish from the network until the parent script
#   is restarted by systemd (5-second gap minimum). avahi-daemon reads XML service
#   files directly via inotify — services reappear the instant avahi-daemon is up,
#   with zero gap and no client process that can die.

# --- 配置区 ---
PICOCLAW_CONFIG="/var/lib/picoclaw/.picoclaw/config.json"
PICOCLAW_SECURITY="/var/lib/picoclaw/.picoclaw/.security.yml"
DASHBOARD_PORT=8080
ZEROCLAW_PORT=42617
PICOCLAW_PORT=18790
AVAHI_SERVICES_DIR="/etc/avahi/services"
REFRESH_INTERVAL=60   # seconds between dynamic-value refresh

# --- 清理：退出时删除我们写的 XML 服务文件 ---
cleanup() {
    echo "ClawBerry Discovery: 正在清理 avahi 服务文件..."
    rm -f \
        "$AVAHI_SERVICES_DIR/clawberry-dashboard.service" \
        "$AVAHI_SERVICES_DIR/clawberry-zeroclaw.service" \
        "$AVAHI_SERVICES_DIR/clawberry-picoclaw.service"
}
trap cleanup EXIT

# --- 写入 avahi XML 服务文件（avahi-daemon 通过 inotify 立即生效）---
publish_services() {
    # 1. 提取 ZeroClaw Pair Code
    ZEROCLAW_OUTPUT=$(zeroclaw gateway get-paircode 2>&1)
    PAIR_CODE=$(echo "$ZEROCLAW_OUTPUT" | grep -o '[0-9]\{6\}' | head -n 1)

    if [ -z "$PAIR_CODE" ]; then
        PAIR_TXT="<txt-record>status=paired_or_locked</txt-record>"
        echo "ZeroClaw: 未发现配对码，设置为锁定状态。"
    else
        PAIR_TXT="<txt-record>pair_code=$PAIR_CODE</txt-record>"
        echo "ZeroClaw: 提取到配对码 $PAIR_CODE"
    fi

    # 2. 提取 Picoclaw Token（token 在 .security.yml 中，不在 config.json 中）
    if [ -f "$PICOCLAW_SECURITY" ]; then
        PICO_TOKEN=$(python3 -c "
import yaml, sys
try:
    with open('$PICOCLAW_SECURITY') as f:
        data = yaml.safe_load(f)
    token = data.get('channel_list', {}).get('pico', {}).get('settings', {}).get('token', '')
    if token and str(token) != 'None':
        print(token)
except Exception:
    pass
" 2>/dev/null)
        if [ -z "$PICO_TOKEN" ]; then
            PICO_TXT="<txt-record>status=no_token</txt-record>"
            echo "Picoclaw: 配置文件存在，但未找到 Token 字段。"
        else
            PICO_TXT="<txt-record>token=$PICO_TOKEN</txt-record>"
            echo "Picoclaw: 提取到 Token: ${PICO_TOKEN:0:8}..."
        fi
    else
        PICO_TXT="<txt-record>status=config_missing</txt-record>"
        echo "Picoclaw: 未找到配置文件 $PICOCLAW_SECURITY"
    fi

    # 3. 写入 XML 服务文件（avahi-daemon 通过 inotify 自动加载，无需重启）
    cat > "$AVAHI_SERVICES_DIR/clawberry-dashboard.service" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>ClawBerry Dashboard</name>
  <service>
    <type>_clawberry._tcp</type>
    <port>$DASHBOARD_PORT</port>
    <txt-record>type=web_ui</txt-record>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
EOF

    cat > "$AVAHI_SERVICES_DIR/clawberry-zeroclaw.service" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>ZeroClaw Gateway</name>
  <service>
    <type>_clawberry._tcp</type>
    <port>$ZEROCLAW_PORT</port>
    <txt-record>type=zeroclaw</txt-record>
    $PAIR_TXT
  </service>
</service-group>
EOF

    cat > "$AVAHI_SERVICES_DIR/clawberry-picoclaw.service" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Picoclaw Gateway</name>
  <service>
    <type>_clawberry._tcp</type>
    <port>$PICOCLAW_PORT</port>
    <txt-record>type=picoclaw</txt-record>
    $PICO_TXT
  </service>
</service-group>
EOF

    echo "ClawBerry Discovery: avahi 服务文件已写入。"
}

# --- 首次写入 ---
echo "正在写入 ClawBerry avahi 服务文件..."
publish_services

# --- 定期刷新动态值（pair_code 可能变化）---
while true; do
    sleep "$REFRESH_INTERVAL"
    publish_services
done
