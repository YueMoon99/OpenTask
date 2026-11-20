# -*- coding: utf-8 -*-
"""对照测试：SendInput(硬件级) vs PostMessage(后台消息)
判断二重螺旋是否响应后台 PostMessage 输入。

SendInput 会真实移动鼠标/模拟按键，属于前台输入。
PostMessage 直接投递到窗口消息队列，属于后台输入。

若 SendInput 有效而 PostMessage 无效 → 该游戏不吃后台消息，后台输入方案受限。
"""
import ctypes
import ctypes.wintypes as wt
import time
import numpy as np

user32 = ctypes.windll.user32

# SendInput 结构
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_W = 0x57

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", INPUTUNION)]

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

def send_input_key(vk):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(vk, 0, 0, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.06)
    inp.union.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def post_key(hwnd, vk):
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(0.06)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)

def grab_region(hwnd):
    import mss
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = get_client_rect(hwnd)
    with mss.MSS() as sct:
        return np.array(sct.grab({'left': pt.x, 'top': pt.y, 'width': w, 'height': h}))

def diff(a, b):
    if a.shape != b.shape: return 1.0
    return float(np.abs(a[:, :, :3].astype(np.int32) - b[:, :, :3].astype(np.int32)).mean() / 255.0)

def main():
    hwnd = find_hwnd()
    if not hwnd: print("[错误] 未找到窗口"); return
    w, h = get_client_rect(hwnd)
    print(f"hwnd=0x{hwnd:X} {w}×{h}")
    print("请确保游戏窗口可见。建议让角色处于【可移动场景】而非纯静态菜单。\n")
    time.sleep(1)

    # --- 测试1: PostMessage W键 ---
    base = grab_region(hwnd)
    post_key(hwnd, VK_W)
    time.sleep(0.4)
    after = grab_region(hwnd)
    d1 = diff(base, after)
    print(f"[PostMessage W键] 画面差异 {d1*100:.2f}%  {'✅生效' if d1 > 0.01 else '❌无变化'}")

    time.sleep(0.5)

    # --- 测试2: SendInput W键（硬件级，会真实作用于前台）---
    base = grab_region(hwnd)
    send_input_key(VK_W)
    time.sleep(0.4)
    after = grab_region(hwnd)
    d2 = diff(base, after)
    print(f"[SendInput W键]  画面差异 {d2*100:.2f}%  {'✅生效' if d2 > 0.01 else '❌无变化'}")

    print("\n结论：")
    if d1 > 0.01:
        print("  ✅ PostMessage 生效 → 后台输入完全可用，可直接开发游戏模式")
    elif d2 > 0.01:
        print("  ⚠️ PostMessage 无效，但 SendInput 有效 → 该游戏只吃硬件级输入")
        print("     后台输入需改用其它方案（如绑定到窗口的 SendInput + 保持窗口前台，或 DXGI/驱动级注入）")
    else:
        print("  ❓ 两者都无变化 → 可能游戏当前在静态界面/未响应，或输入被反作弊拦截")
        print("     建议：切到游戏内一个会动的场景（角色站着能前后移动）再重试")

if __name__ == '__main__':
    main()
