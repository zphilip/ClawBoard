import os
import sys
import time
import signal
import logging
import subprocess
from PIL import Image, ImageDraw, ImageFont

# 驱动与路径配置
os.environ['GPIOZERO_PIN_FACTORY'] = 'rpigpio'
current_dir = os.path.dirname(os.path.realpath(__file__))
libdir = os.path.join(current_dir, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

from waveshare_epd import epd2in13_V4

logging.basicConfig(level=logging.INFO)

# ── Global EPD handle so the signal handler can reach it ──────────────────
epd = None

def _shutdown(signum=None, frame=None):
    """Release SPI + GPIO before exiting so the bus is free immediately."""
    logging.info("Shutdown signal %s — releasing display hardware...", signum)
    if epd is not None:
        try:
            epd.Dev_exit()          # calls GPIO.cleanup() + SPI.close()
        except Exception:
            try:
                epd.module_exit()   # older driver versions
            except Exception as e:
                logging.warning("Could not release display hardware: %s", e)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


def get_ip_address(ifname):
    try:
        cmd = f"ip -4 addr show {ifname} | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){{3}}'"
        return subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
    except:
        return None

def get_service_status(service_name):
    """检查 Systemd 服务状态，返回 Active 或 Inactive"""
    try:
        cmd = f"systemctl is-active {service_name}"
        status = subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
        return "Running" if status == "active" else "Stopped"
    except:
        return "Unknown"


epd = epd2in13_V4.EPD()
logging.info("Starting Monitoring...")

while True:
    # 1. 初始化屏幕 (每次刷新需重新 init，除非使用局部刷新)
    epd.init()

    # 2. 准备画布
    image = Image.new('1', (epd.height, epd.width), 255)
    draw = ImageDraw.Draw(image)

    # 3. 字体加载 (建议使用绝对路径)
    try:
        f_title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 28)
        f_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    except:
        f_title = ImageFont.load_default()
        f_small = ImageFont.load_default()

    # --- 绘制 UI ---
    # 标题
    draw.text((10, 2), "ClawBerry Monitor", font=f_title, fill=0)
    draw.line((10, 32, 240, 32), fill=0)

    # 网络状态
    w_ip = get_ip_address('wlan0') or "Disconnected"
    u_ip = get_ip_address('usb0') or "Not detected"
    draw.text((10, 38), f"WiFi: {w_ip}", font=f_small, fill=0)
    draw.text((10, 56), f"USB:  {u_ip}", font=f_small, fill=0)

    draw.line((10, 76, 240, 76), fill=0)

    # 服务状态
    s1 = get_service_status("zeroclaw")
    s2 = get_service_status("picoclaw")
    draw.text((10, 82), f"zeroclaw: {s1}", font=f_small, fill=0)
    draw.text((10, 100), f"picoclaw: {s2}", font=f_small, fill=0)

    # --- 刷新与休眠 ---
    logging.info("Refreshing Screen...")
    epd.display(epd.getbuffer(image))

    # 进入屏幕物理休眠（必须，否则烧屏）
    epd.sleep()

    # 循环等待间隔 (例如 60 秒)
    logging.info("Waiting for next update...")
    time.sleep(60)
