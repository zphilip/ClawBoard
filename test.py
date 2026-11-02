import time
from periphery import GPIO, SPI

# --- Radxa Cubie A7Z 引脚定义 ---
# RST -> Pin 15 (gpiochip1, line 7)
# DC  -> Pin 22 (gpiochip1, line 5)
# CS  -> Pin 24 (Hardware SPI1 CS0)
# DIN -> Pin 19 (SPI1 MOSI)
# CLK -> Pin 23 (SPI1 CLK)

RST_CHIP, RST_LINE = "/dev/gpiochip1", 7
DC_CHIP, DC_LINE = "/dev/gpiochip1", 5
SPI_DEV = "/dev/spidev1.0"

def oled_test():
    print("--- 启动最终修正测试 (RST:P15, DC:P22, CS:P24) ---")
    rst = None
    dc = None
    spi = None
    
    try:
        # 1. 初始化硬件接口
        rst = GPIO(RST_CHIP, RST_LINE, "out")
        dc = GPIO(DC_CHIP, DC_LINE, "out")
        spi = SPI(SPI_DEV, 0, 2000000) # 2MHz 速率

        def write_cmd(c):
            dc.write(False)
            spi.transfer([c])

        def write_data(d):
            dc.write(True)
            if isinstance(d, int):
                spi.transfer([d])
            else:
                # 解决 Errno 90: 分段发送（每段 4096 字节）
                for i in range(0, len(d), 4096):
                    spi.transfer(d[i:i + 4096])

        # 2. 深度硬件复位 (解决重启不亮的问题)
        print("正在执行深度复位...")
        rst.write(True);  time.sleep(0.1)
        rst.write(False); time.sleep(1.0) # 强制拉低 1 秒，确保彻底放电
        rst.write(True);  time.sleep(0.5)

        # 3. SSD1357 完整初始化序列 (针对 Waveshare 0.96 优化)
        print("正在初始化寄存器...")
        write_cmd(0xFD); write_data(0x12) # Unlock
        write_cmd(0xAE)                   # 关显示

        write_cmd(0xCA); write_data(0x7F) # Multiplex: 127 (128 行驱动)

        # Remap (0xA0):
        #   bit0=0  水平地址递增
        #   bit1=1  列地址翻转 (SEG 127→0)
        #   bit2=0  RGB 顺序
        #   bit4=1  COM 扫描方向翻转
        #   bit5=1  Odd/Even COM 分割
        #   bit6-7=01 65K 色深
        # 0x72 与 0x62 的区别仅在 bit4 (COM 扫描方向)，两者均可；
        # 若画面上下翻转，在 0x72 和 0x62 之间切换。
        write_cmd(0xA0); write_data(0x72)

        # 列地址：SSD1351 每个列地址对应 1 像素，128 宽 → 0x00~0x7F
        write_cmd(0x15); write_data(0x00); write_data(0x7F)

        # 行地址：128 高 → 0x00~0x7F
        write_cmd(0x75); write_data(0x00); write_data(0x7F)

        # Display Offset: 0x00 = 不偏移，GRAM 行 0 对应屏幕第 0 行
        # 之前 0x40 导致显示从 GRAM 第 64 行开始，
        # 而写入数据在 0~63 行，造成"上半噪点+下半白色"的错位现象。
        write_cmd(0xA2); write_data(0x00)

        # Display Start Line: 也设为 0，防止额外偏移
        write_cmd(0xA1); write_data(0x00)

        # 增强对比度
        write_cmd(0xC1); write_data(0x8F); write_data(0x8F); write_data(0x8F)
        write_cmd(0xC7); write_data(0x0F)

        write_cmd(0xAF) # 开显示
        print("初始化指令已发送。")

        # 先清屏（全黑），确保 GRAM 干净，排除残留噪点
        write_cmd(0x5C)
        print("正在清屏（全黑）...")
        black_data = bytes(128 * 128 * 2)  # 32768 字节，全 0x00
        write_data(black_data)
        time.sleep(0.5)

        # 再刷全白，验证完整 128x128 显存写入
        write_cmd(0x15); write_data(0x00); write_data(0x7F)
        write_cmd(0x75); write_data(0x00); write_data(0x7F)
        write_cmd(0x5C)
        print("正在刷入全屏白色测试...")
        white_data = bytes([0xFF, 0xFF] * (128 * 128))  # 精确 32768 字节
        write_data(white_data)
            
        print("发送完毕。观察：屏幕应全白，无噪点，无偏移。")
        time.sleep(5)

    except Exception as e:
        print(f"运行失败: {e}")
    finally:
        if rst: rst.close()
        if dc: dc.close()
        if spi: spi.close()

if __name__ == "__main__":
    oled_test()