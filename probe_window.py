# -*- coding: utf-8 -*-
"""
OpenTask 游戏模式可行性探测脚本（纯 ctypes，无 pywin32 依赖）
探测目标：二重螺旋 (EM-Win64-Shipping.exe)

三件事：
1. 窗口定位 —— EnumWindows 按进程名/标题/类名找到 hWnd，确认窗口类名（是否 UnrealWindow）
2. 后台抓帧 —— PrintWindow 抓窗口画面，判断是否黑屏（被遮挡/非前台也能抓）
3. 后台输入 —— PostMessage 发鼠标移动 + 点击 + 按键，看游戏是否有反应

用法：python probe_window.py
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time
import struct

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---------------- 通用 Win32 常量 ----------------
WM_MOUSEMOVE    = 0x0200
WM_LBUTTONDOWN  = 0x0201
WM_LBUTTONUP    = 0x0202
WM_RBUTTONDOWN  = 0x0204
WM_RBUTTONUP    = 0x0205
WM_KEYDOWN      = 0x0100
WM_KEYUP        = 0x0101
WM_CHAR         = 0x0102
MK_LBUTTON      = 0x0001

# 虚拟键码
VK_W = 0x57
VK_S = 0x53
VK_A = 0x41
VK_D = 0x44
VK_SPACE = 0x20
VK_ESCAPE = 0x1B

# ---------------- 窗口枚举 ----------------
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

def find_windows():
    """枚举所有顶层窗口，返回 [(hwnd, pid, class_name, title)]"""
    result = []
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append((int(hwnd), int(pid.value), cls.value, title.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return result


def get_client_rect(hwnd):
    """获取窗口客户区大小（客户端坐标）"""
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


def get_window_rect(hwnd):
    """获取窗口屏幕矩形"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


# ---------------- PrintWindow 抓帧 ----------------
def capture_window(hwnd):
    """用 PrintWindow 抓窗口画面，返回 (bytes_bgra, w, h)，失败返回 None"""
    w, h = get_client_rect(hwnd)
    if w <= 0 or h <= 0:
        return None, 0, 0

    hwnd_dc = user32.GetWindowDC(hwnd)
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mfc_dc, bmp)

    # PW_RENDERFULLCONTENT = 2，尝试渲染 DirectX 内容
    ok = user32.PrintWindow(hwnd, mfc_dc, 2)
    if not ok:
        # 回退到默认 PrintWindow
        ok = user32.PrintWindow(hwnd, mfc_dc, 0)

    if not ok:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        return None, 0, 0

    # 读取位图数据
    bmi = struct.pack('IiiHHIIiiII', 40, w, h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf_size = w * h * 4
    buf = ctypes.create_string_buffer(buf_size)
    got = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, bmi, 0)  # DIB_RGB_COLORS

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    if got == 0:
        return None, 0, 0
    return buf.raw, w, h


def is_black(raw, w, h):
    """统计画面中非黑像素比例，判断是否黑屏"""
    total = w * h
    if total == 0:
        return True
    sample_step = max(1, total // 20000)  # 抽样，加快速度
    non_black = 0
    sampled = 0
    for i in range(0, total, sample_step):
        off = i * 4
        b, g, r = raw[off], raw[off + 1], raw[off + 2]
        if b > 8 or g > 8 or r > 8:
            non_black += 1
        sampled += 1
    ratio = non_black / sampled
    return ratio < 0.005  # 几乎全黑判定为黑屏


# ---------------- PostMessage 输入 ----------------
def post_mouse_move(hwnd, x, y):
    lparam = (y << 16) | (x & 0xFFFF)
    return user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)

def post_mouse_click(hwnd, x, y, button='left'):
    lparam = (y << 16) | (x & 0xFFFF)
    down = WM_LBUTTONDOWN if button == 'left' else WM_RBUTTONDOWN
    up = WM_LBUTTONUP if button == 'left' else WM_RBUTTONUP
    wparam = MK_LBUTTON if button == 'left' else 0x0002
    user32.PostMessageW(hwnd, down, wparam, lparam)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, up, 0, lparam)

def post_key(hwnd, vk):
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)


def main():
    print("=" * 60)
    print("OpenTask 游戏模式可行性探测 —— 目标：二重螺旋")
    print("=" * 60)

    # ---------- 1. 窗口定位 ----------
    print("\n[1/3] 窗口定位 ...")
    wins = find_windows()
    target = None
    for hwnd, pid, cls, title in wins:
        if '二重螺旋' in title or 'EM-Win64' in title or cls == 'UnrealWindow':
            target = (hwnd, pid, cls, title)
            print(f"  找到候选窗口: hwnd={hwnd}, pid={pid}, class='{cls}', title='{title}'")

    # 精确匹配：优先标题含"二重螺旋"
    if target is None:
        print("  [失败] 未找到二重螺旋窗口！请确认游戏正在运行（窗口模式/无边框）")
        sys.exit(1)

    hwnd, pid, cls, title = target
    cw, ch = get_client_rect(hwnd)
    l, t, r, b = get_window_rect(hwnd)
    print(f"\n  定位结果: hwnd={hwnd} (0x{hwnd:X}), pid={pid}")
    print(f"  窗口类名: '{cls}'  {'✅ UnrealWindow，ok-ww 同款，兼容性预期高' if cls == 'UnrealWindow' else '⚠️ 非标准 UnrealWindow，需进一步确认'}")
    print(f"  窗口屏幕矩形: ({l}, {t}) -> ({r}, {b})，尺寸 {r-l}×{b-t}")
    print(f"  客户区尺寸: {cw}×{ch}")

    # ---------- 2. 后台抓帧 ----------
    print("\n[2/3] 后台抓帧 (PrintWindow) ...")
    raw, w, h = capture_window(hwnd)
    if raw is None:
        print("  [失败] PrintWindow 抓帧失败")
    else:
        black = is_black(raw, w, h)
        if black:
            print(f"  [警告] 抓到画面 {w}×{h}，但判定为黑屏 ⚠️")
            print("         → PrintWindow 无法渲染此游戏的 DirectX 内容")
            print("         → 需改用 WGC(Windows Graphics Capture) 方案")
        else:
            print(f"  ✅ 抓到画面 {w}×{h}，非黑屏，PrintWindow 可用")
            # 保存一帧供人工确认
            try:
                save_png(raw, w, h, "probe_capture.png")
                print("  已保存抓帧到 probe_capture.png，请打开确认画面是否正常")
            except Exception as e:
                print(f"  (保存预览失败，不影响结论: {e})")

    # ---------- 3. 后台输入 ----------
    print("\n[3/3] PostMessage 后台输入测试 ...")
    print("  即将在游戏窗口中心位置发送一次【鼠标点击】")
    print("  然后发送【W 键】(前进)，观察游戏角色是否有反应")
    print("  提示：请确保游戏窗口在可见状态，注意观察 3 秒内游戏内反应")
    time.sleep(1)

    cx, cy = cw // 2, ch // 2
    r1 = post_mouse_click(hwnd, cx, cy, 'left')
    print(f"  已发送鼠标左键点击 @ 窗口坐标({cx}, {cy})，PostMessage 返回 {r1}")

    time.sleep(0.5)
    r2 = post_key(hwnd, VK_W)
    print(f"  已发送 W 键(前进)，PostMessage 返回 {r2}")

    print("\n" + "=" * 60)
    print("探测完成。请根据上面的结果判断：")
    print("  - 类名是 UnrealWindow → 输入兼容性预期高")
    print("  - 抓帧非黑屏 → 后台截图可行")
    print("  - 游戏内有反应(点击/移动) → PostMessage 后台输入可行")
    print("=" * 60)


def save_png(raw, w, h, path):
    """将 BGRA 数据保存为 PNG（纯标准库，用 struct + zlib 手写 PNG）"""
    import zlib
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        c += struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        return c

    # BGRA -> RGB 每行，并反转（DIB 是自底向上）
    rows = []
    stride = w * 4
    for y in range(h - 1, -1, -1):
        row = bytearray()
        base = y * stride
        for x in range(w):
            off = base + x * 4
            b, g, r = raw[off], raw[off + 1], raw[off + 2]
            row += bytes((r, g, b))
        rows.append(bytes(row))

    raw_rgb = b''.join(rows)
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', ihdr)
    png += chunk(b'IDAT', zlib.compress(raw_rgb, 9))
    png += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)


if __name__ == '__main__':
    main()
