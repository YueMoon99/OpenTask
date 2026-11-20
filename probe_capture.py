# -*- coding: utf-8 -*-
"""抓帧方案诊断：测试多种方式抓取二重螺旋窗口，确定哪种能用"""
import ctypes
import ctypes.wintypes as wt
import struct
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

def find_hwnd():
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    found = []
    def cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        t = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, t, n + 1)
        if '二重螺旋' in t.value:
            found.append(int(hwnd))
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None

def get_client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom

def is_minimized(hwnd):
    return bool(user32.IsIconic(hwnd))

def is_foreground(hwnd):
    return user32.GetForegroundWindow() == hwnd

def bmp_to_raw(hwnd_dc, bmp, w, h):
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    gdi32.SelectObject(mfc_dc, bmp)
    bmi = struct.pack('IiiHHIIiiII', 40, w, h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, bmi, 0)
    gdi32.DeleteDC(mfc_dc)
    if got == 0:
        return None
    return buf.raw

def test_printwindow(hwnd, w, h, flags):
    hdc = user32.GetWindowDC(hwnd)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    ok = user32.PrintWindow(hwnd, hdc, flags)
    raw = bmp_to_raw(hdc, bmp, w, h) if ok else None
    gdi32.DeleteObject(bmp)
    user32.ReleaseDC(hwnd, hdc)
    return ok, raw

def is_black(raw, w, h):
    total = w * h
    step = max(1, total // 20000)
    nonblack = 0
    cnt = 0
    for i in range(0, total, step):
        off = i * 4
        if raw[off] > 8 or raw[off+1] > 8 or raw[off+2] > 8:
            nonblack += 1
        cnt += 1
    return nonblack / cnt < 0.005, nonblack / cnt

def test_bitblt_screen(hwnd, w, h):
    """从屏幕DC BitBlt 抓窗口所在屏幕区域（需要窗口可见未被遮挡）"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    # 客户区左上角在屏幕的坐标
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    screen_dc = user32.GetDC(0)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    mfc_dc = gdi32.CreateCompatibleDC(screen_dc)
    gdi32.SelectObject(mfc_dc, bmp)
    # SRCCOPY
    ok = gdi32.BitBlt(mfc_dc, 0, 0, w, h, screen_dc, sx, sy, 0x00CC0020)
    raw = bmp_to_raw(screen_dc, bmp, w, h) if ok else None
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc_dc)
    user32.ReleaseDC(0, screen_dc)
    return bool(ok), raw, (sx, sy)

def main():
    hwnd = find_hwnd()
    if not hwnd:
        print("[错误] 未找到二重螺旋窗口")
        return
    w, h = get_client_rect(hwnd)
    print(f"hwnd=0x{hwnd:X}, 客户区 {w}×{h}")
    print(f"最小化: {is_minimized(hwnd)} | 前台: {is_foreground(hwnd)}")
    print()

    # 1. PrintWindow 多种 flag
    print("=== PrintWindow 测试 ===")
    for flags, name in [(0, '默认(0)'), (1, 'PW_CLIENTONLY(1)'), (2, 'PW_RENDERFULLCONTENT(2)'), (3, 'PW_RENDERFULLCONTENT|CLIENTONLY(3)')]:
        ok, raw = test_printwindow(hwnd, w, h, flags)
        if ok and raw:
            black, ratio = is_black(raw, w, h)
            print(f"  {name}: 成功, 非黑像素占比 {ratio*100:.1f}% {'✅可用' if not black else '⚠️黑屏'}")
        else:
            print(f"  {name}: 失败 (ok={ok})")

    # 2. BitBlt 屏幕抓取（仅当前台可见时有效）
    print("\n=== BitBlt 屏幕抓取测试 ===")
    ok, raw, pos = test_bitblt_screen(hwnd, w, h)
    if ok and raw:
        black, ratio = is_black(raw, w, h)
        print(f"  成功, 客户区屏幕原点=({pos[0]},{pos[1]}), 非黑像素占比 {ratio*100:.1f}% {'✅可用' if not black else '⚠️黑屏'}")
    else:
        print(f"  失败 (ok={ok})")

    print("\n结论解读：")
    print("  - PrintWindow 某个 flag 非黑屏 → 后台抓帧直接可用（最理想）")
    print("  - 只有 BitBlt 非黑屏 → 窗口必须可见且不被遮挡才能抓（不满足后台需求，需 WGC）")
    print("  - 全部黑屏/失败 → 需 WGC(Windows Graphics Capture) 方案")

if __name__ == '__main__':
    main()
