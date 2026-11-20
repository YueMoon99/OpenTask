# -*- coding: utf-8 -*-
"""验证 PostMessage 后台输入是否真的作用于二重螺旋。
方法：用 mss 抓屏幕，对比「发消息前」和「发消息后」窗口区域画面是否发生变化。
如果 PostMessage 生效（例如移动/点击导致画面变化），两次画面会有差异。
"""
import ctypes
import ctypes.wintypes as wt
import time
import numpy as np

user32 = ctypes.windll.user32

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MK_LBUTTON = 0x0001

# 方向键虚拟键码
VK_W = 0x57
VK_SPACE = 0x20
VK_ESCAPE = 0x1B

def find_hwnd():
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    found = []
    def cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd): return True
        n = user32.GetWindowTextLengthW(hwnd)
        t = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, t, n + 1)
        if '二重螺旋' in t.value: found.append(int(hwnd))
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None

def get_client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom

def post_key(hwnd, vk, down_time=0.06):
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(down_time)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)

def post_click(hwnd, x, y):
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.03)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)

def grab_window_region(hwnd):
    """用 mss 抓窗口所在屏幕区域，返回 numpy 灰度数组"""
    import mss
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = get_client_rect(hwnd)
    with mss.mss() as sct:
        region = {'left': pt.x, 'top': pt.y, 'width': w, 'height': h}
        img = np.array(sct.grab(region))
    return img

def img_diff(a, b):
    """返回两帧的差异度 0~1"""
    if a.shape != b.shape:
        return 1.0
    ga = a[:, :, :3].astype(np.int32)
    gb = b[:, :, :3].astype(np.int32)
    diff = np.abs(ga - gb).mean() / 255.0
    return float(diff)

def main():
    hwnd = find_hwnd()
    if not hwnd:
        print("[错误] 未找到二重螺旋窗口")
        return
    w, h = get_client_rect(hwnd)
    print(f"hwnd=0x{hwnd:X}, 客户区 {w}×{h}")
    print("注意：请确保游戏窗口此刻【可见、不被遮挡】，以便 mss 能抓到画面做对比。\n")

    print("等待 1 秒稳定画面...")
    time.sleep(1)

    # 基准帧
    base = grab_window_region(hwnd)
    print("已抓基准帧。开始发送 PostMessage 输入序列（移动+点击+按键）...")

    # 发一串输入
    cx, cy = w // 2, h // 2
    post_click(hwnd, cx, cy)
    time.sleep(0.2)
    post_key(hwnd, VK_W)
    time.sleep(0.2)
    post_key(hwnd, VK_SPACE)
    time.sleep(0.2)
    post_click(hwnd, cx, cy - 50)

    time.sleep(0.5)
    after = grab_window_region(hwnd)
    d = img_diff(base, after)
    print(f"\n画面差异度: {d*100:.2f}%")
    if d > 0.01:
        print("✅ 画面发生变化 → PostMessage 后台输入【很可能生效】")
        print("   （画面有变化说明游戏响应了消息，具体是移动/点击/技能需结合游戏内表现判断）")
    else:
        print("⚠️ 画面几乎无变化 → PostMessage 可能【未生效】，或该输入未引起画面变化")
        print("   （建议：切换到游戏内，肉眼确认角色是否移动/点击是否触发）")

if __name__ == '__main__':
    main()
