import os
import sys
from typing import Any
import ctypes

# ---------- Windows 提权：修复"录制器无法捕获被提权（管理员）运行的游戏窗口输入" ----------
# pynput 的全局低层钩子（WH_MOUSE_LL / WH_KEYBOARD_LL）受 UIPI 约束：低完整性级别(IL)的进程
# 无法接收发往更高 IL 窗口的输入。许多游戏（反作弊 / 提权启动器）以 High/System IL 运行，
# 导致普通启动的 OpenTask 录不到它们，但桌面/浏览器（Medium IL）正常。
# 解决：以管理员（High IL）身份运行本进程，钩子即可拦截所有窗口输入。
_ELEVATE_CHILD_FLAG = "--opentask-elevated-child"   # 标记"这是提权后重启的合法子进程"，避免重复提权
if sys.platform == "win32" and not os.environ.get("OPEN_TASK_NO_ELEVATE"):
    try:
        if (not ctypes.windll.shell32.IsUserAnAdmin()
                and _ELEVATE_CHILD_FLAG not in sys.argv):
            print("【后端】未以管理员身份运行，正在请求提权以捕获被提权游戏窗口的输入...")
            if getattr(sys, "frozen", False):
                # 打包成 exe 后：sys.executable 就是 exe 自身，参数只需带提权标记
                params = _ELEVATE_CHILD_FLAG
            else:
                # 源码模式：sys.executable 是 python.exe，必须把脚本路径一起传进去
                params = " ".join('"%s"' % a for a in (sys.argv + [_ELEVATE_CHILD_FLAG]))
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, os.getcwd(), 1)
            # 【修复】ShellExecuteW 返回值 <=32 表示系统没有成功创建提权进程
            # （例如用户点了“否”、UAC 被策略禁用）。此时绝不能退出原进程，
            # 否则新电脑上会表现为“双击后什么都没有发生”。
            if int(result) > 32:
                sys.exit(0)
            print("【后端】提权被取消或失败，已降级为普通权限继续运行。")
    except Exception as exc:
        # 提权失败（如被组策略禁止）时降级为普通运行，录制器启动时会提示
        print("【后端】提权失败，已降级为普通权限继续运行:", exc)

IS_ADMIN = (
    True if sys.platform != "win32"
    else bool(ctypes.windll.shell32.IsUserAnAdmin())
)


# ---------- 单实例保护：防止多开导致退出后残留孤儿 python 进程占用程序目录 ----------
# 背景：本程序提权会启动一个新 python 进程；若用户多次双击 run.bat，会产生多个互不通信的
# 进程，Windows 托盘把它们合并成一个图标。托盘退出只清理了响应菜单的那一个，其余进程没有
# 托盘图标却仍活着，导致文件夹被占用、任务管理器里残留多个 python.exe。
# 方案：用命名互斥体保证全局只有一个实例；检测到已有实例时激活其窗口并退出本次启动。
_SINGLE_INSTANCE_HANDLE = None

def _acquire_single_instance():
    """创建命名互斥体实现单实例。返回 True=获取成功；False=已有实例在运行。"""
    global _SINGLE_INSTANCE_HANDLE
    if sys.platform != "win32":
        return True
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = k32.CreateMutexW(None, True, "Local\\OpenTask_SingleInstance_v1")
        _SINGLE_INSTANCE_HANDLE = handle
        ERROR_ALREADY_EXISTS = 183
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            # 已有实例持有该互斥体；关掉本次新建的句柄（不影响对方的），判定为重复启动
            k32.CloseHandle(handle)
            _SINGLE_INSTANCE_HANDLE = None
            return False
        return True
    except Exception as exc:
        print("【系统】单实例检测不可用（降级为允许多开）:", exc)
        return True

def _activate_existing_window():
    """把已运行实例的窗口切到前台（标题在运行中会被前端改写，故用包含匹配）。"""
    try:
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        found = []
        PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            n = u32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if "OpenTask" in buf.value and u32.IsWindowVisible(hwnd):
                    found.append(hwnd)
            return True
        u32.EnumWindows(PROC(_cb), 0)
        if found:
            u32.ShowWindow(found[0], 9)   # SW_RESTORE
            u32.SetForegroundWindow(found[0])
            return True
    except Exception:
        pass
    return False

if (not os.environ.get("OPEN_TASK_NO_SINGLE_INSTANCE")
        and not _acquire_single_instance()):
    print("【系统】检测到已有 OpenTask 实例正在运行，本次启动退出。")
    if not _activate_existing_window():
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                "OpenTask 已在运行，请勿重复启动。\n"
                "请切换到已打开的窗口操作；若需重启，请先从托盘菜单退出。",
                "OpenTask", 0x00000040)
        except Exception:
            pass
    sys.exit(0)

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import mss
import cv2
import numpy as np
import pyautogui
import json
import threading
import queue
import time
import asyncio

# ====================== 全局唯一 Tk 服务线程 ======================
# tkinter 不允许跨线程调用：整个进程只能由一个线程持有 Tk 解释器，
# 否则会抛出 "main thread is not in main loop"。因此所有原生对话框
# （保存、坐标拾取、区域框选、宏录制器）统一交给这个常驻后台线程处理。
class TkService:
    def __init__(self):
        self._root: Any = None
        self._thread: Any = None
        self._lock = threading.Lock()
        self._jobs = queue.Queue()
        self._ready = threading.Event()

    def _ensure(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                if not self._ready.wait(10):
                    raise RuntimeError("Tk 服务线程启动失败")

    def _run(self):
        import tkinter as tk
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.attributes("-topmost", True)
        self._ready.set()
        self._poll()
        self._root.mainloop()

    def _poll(self):
        try:
            while True:
                job = self._jobs.get_nowait()
                try:
                    job()
                except Exception as exc:
                    print("【TkService】任务异常:", exc)
                self._jobs.task_done()
        except queue.Empty:
            pass
        self._root.after(30, self._poll)

    def submit(self, job):
        """在 Tk 主线程中执行 job（无返回值需求，例如交互式窗口）。"""
        self._ensure()
        self._jobs.put(job)

    def call_sync(self, func, *args, **kwargs):
        """在 Tk 主线程中同步执行 func 并返回结果（会阻塞调用线程）。"""
        self._ensure()
        box: list[Any] = [None, None]
        ev = threading.Event()

        def job():
            try:
                box[0] = func(*args, **kwargs)
            except Exception as exc:
                box[1] = exc
            ev.set()

        self._jobs.put(job)
        ev.wait()
        if box[1] is not None:
            raise box[1]
        return box[0]

    def root(self):
        """返回常驻的隐藏 Tk 根窗口，用作其它 Toplevel / 对话框的 parent。"""
        self._ensure()
        return self._root

_tk_service = TkService()

# ====================== 未保存工程确认（退出 / 切换工程前的三按钮对话框） ======================
# 工程图数据只存在于前端画布中，后端无法直接保存。因此退出类操作由后端发 WebSocket
# 请求，前端判断 isDirty 后显示“保存 / 不保存 / 取消”弹窗，并把决策回传。
# 若前端连接不可用，则用 Tk 原生三按钮对话框兜底。
_PENDING_UNSAVED = {}
_PENDING_UNSAVED_LOCK = threading.Lock()
_UNSAVED_SEQ = 0


def _unsaved_dialog_text(reason):
    if reason == "close":
        return ("关闭窗口前保存", "当前工程尚未保存", "关闭窗口后软件会收起到系统托盘。是否先保存修改？")
    if reason == "exit":
        return ("退出软件前保存", "当前工程尚未保存", "退出软件会结束任务并关闭程序。是否先保存修改？")
    if reason == "new":
        return ("新建工程前保存", "当前工程尚未保存", "新建工程会清空当前画布。是否先保存修改？")
    return ("打开工程前保存", "当前工程尚未保存", "打开其他工程会替换当前画布。是否先保存修改？")


def _native_unsaved_choice(reason):
    """无前端连接时的兜底原生对话框（必须运行在 TkService 线程）。"""
    import tkinter as tk
    title, sub, msg = _unsaved_dialog_text(reason)
    result = {"value": "cancel"}
    root = _tk_service.root()
    dlg = tk.Toplevel(root)
    dlg.title("OpenTask - " + title)
    dlg.geometry("430x170+420+260")
    dlg.resizable(False, False)
    dlg.transient(root)
    dlg.attributes("-topmost", True)

    frame = tk.Frame(dlg, padx=18, pady=14)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=title, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
    tk.Label(frame, text=sub, foreground="#9aa5bd").pack(anchor="w", pady=(2, 8))
    tk.Label(frame, text=msg, wraplength=390, justify="left").pack(anchor="w")

    def choose(value):
        result["value"] = value
        dlg.destroy()

    btn_frame = tk.Frame(frame)
    btn_frame.pack(fill="x", side="bottom", pady=(14, 0))
    for text, value in (("保存", "save"), ("不保存", "discard"), ("取消", "cancel")):
        tk.Button(btn_frame, text=text, width=10, command=lambda v=value: choose(v)).pack(side="right", padx=(6, 0))

    dlg.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
    dlg.grab_set()
    dlg.focus_force()
    root.wait_window(dlg)
    return result["value"]


def _resolve_unsaved_decision(request_id, decision):
    """前端回传决策：唤醒正在等待的退出/关闭线程。"""
    if not request_id:
        return
    with _PENDING_UNSAVED_LOCK:
        entry = _PENDING_UNSAVED.pop(request_id, None)
    if entry is not None:
        entry["decision"] = decision
        entry["event"].set()


def _request_unsaved_decision(reason, timeout=300):
    """请求前端确认；返回 save/discard/cancel/clean，连接不可用时返回 None。"""
    global _UNSAVED_SEQ
    ws = _LOG_WS
    loop = _LOG_LOOP
    if ws is None or loop is None:
        return None

    with _PENDING_UNSAVED_LOCK:
        _UNSAVED_SEQ += 1
        request_id = "u_%d" % _UNSAVED_SEQ
        entry = {"event": threading.Event(), "decision": None}
        _PENDING_UNSAVED[request_id] = entry

    try:
        message = json.dumps({
            "status": "request_unsaved_decision",
            "requestId": request_id,
            "reason": reason,
        }, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(ws.send_text(message), loop).result(timeout=5)

        deadline = time.monotonic() + timeout
        while not entry["event"].is_set():
            if _LOG_WS is not ws:
                return None
            if time.monotonic() >= deadline:
                return None
            entry["event"].wait(0.2)
        return entry["decision"]
    except Exception as exc:
        print("【系统】请求未保存确认失败:", exc)
        return None
    finally:
        with _PENDING_UNSAVED_LOCK:
            _PENDING_UNSAVED.pop(request_id, None)


def _ensure_main_window_visible():
    """托盘退出时窗口通常是隐藏的，先显示出来，用户才能看到确认弹窗。"""
    try:
        if _main_window is not None:
            _main_window.show()
            _main_window.restore()
    except Exception as exc:
        print("【系统】显示主窗口失败:", exc)


def _confirm_before_exit_like_action(reason, allow_native_fallback=True):
    """退出/关闭前确认。返回 True=继续，False=取消本次操作。"""
    _ensure_main_window_visible()
    decision = _request_unsaved_decision(reason)
    if decision is None:
        if not allow_native_fallback:
            print("【系统】无法连接界面读取未保存状态，已取消本次操作。")
            return False
        try:
            native = _tk_service.call_sync(_native_unsaved_choice, reason)
        except Exception as exc:
            print("【系统】原生未保存确认框失败:", exc)
            return False
        if native == "save":
            # 原生提示里的“保存”无法读取画布；唤起界面后再走一次前端确认
            _ensure_main_window_visible()
            decision = _request_unsaved_decision(reason)
            return decision in ("clean", "discard", "save")
        return native in ("clean", "discard")
    return decision in ("clean", "discard", "save")


# ====================== 后端日志 → 前端「运行日志」面板 ======================
# 把标准输出(stdout)实时镜像到前端的运行日志面板，确保控制台里出现过的提示
# （启动信息、导出 EXE 进度、任务执行日志等）都能在面板中看到，解决“显示不全”问题。
_LOG_WS = None
_LOG_LOOP = None
_TEE_INSTALLED = False


def _forward_log(msg):
    ws = _LOG_WS
    loop = _LOG_LOOP
    if ws is None or loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            ws.send_text(json.dumps({"status": "log", "msg": msg})), loop
        )
    except Exception:
        pass


class _UiLogTee:
    """包裹原始 stdout，写回原输出的同时，把整行转发到前端日志面板。"""

    def __init__(self, original):
        self._orig = original
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        try:
            self._orig.write(s)
            self._orig.flush()
        except Exception:
            pass
        if s:
            with self._lock:
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line.strip():
                        _forward_log(line.rstrip())
        return len(s)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._orig.isatty()
        except Exception:
            return False


def _install_ui_log_tee():
    global _TEE_INSTALLED
    if not _TEE_INSTALLED:
        sys.stdout = _UiLogTee(sys.stdout)
        _TEE_INSTALLED = True


_install_ui_log_tee()

app = FastAPI()

# 资源路径：源码模式用 main.py 所在目录；打包模式用 PyInstaller 的临时解压目录(_MEIPASS)
# static 等只读资源会随 --add-data 打进包里，位于 _MEIPASS/static
_meipass = getattr(sys, "_MEIPASS", None)
if getattr(sys, "frozen", False) and _meipass:
    BASE_DIR = _meipass
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(BASE_DIR, "static")

# 程序所在目录（打包后 = exe 所在目录），供"打开程序目录"等操作使用
# 用 sys.executable 而非 __file__，后者在打包后指向临时解压目录
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))

# ---------- 最近使用的工程 ----------
# 持久化到用户目录，避免打包成单文件后写到只读/临时目录而丢失
RECENT_FILE = os.path.join(os.path.expanduser("~"), ".opentask_recent.json")
RECENT_MAX = 10


def _load_recent():
    try:
        with open(RECENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str)]
    except Exception:
        pass
    return []


def _save_recent(items):
    try:
        with open(RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump(items[:RECENT_MAX], f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("保存最近项目失败:", exc)


def _add_recent(path):
    path = os.path.abspath(path)
    items = _load_recent()
    items = [p for p in items if os.path.normcase(p) != os.path.normcase(path)]
    items.insert(0, path)
    del items[RECENT_MAX:]
    _save_recent(items)
    return items

# ---------- 应用元信息与资源 ----------
APP_NAME = "OpenTask"
APP_VERSION = "1.1.0"
APP_PORT = 0                                   # 运行时赋值，供「关于」对话框显示
ICON_PNG = os.path.join(static_dir, "icon.png")
ICON_ICO = os.path.join(static_dir, "icon.ico")
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "OpenTaskAutomation"

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")

running = False
run_id = 0                 # 【修复】每次启动自增；线程只认自己的 run_id，避免“停止后立刻启动”造成双线程抢键鼠
global_editor_listener = None

# ---------- 通用工具函数 ----------
def parse_hotkey(hk):
    """把 'ctrl+shift+f8' 转成 pynput 的 '<ctrl>+<shift>+<f8>'。
    【修复】pynput 没有 Key.win，Windows 下必须映射成 'cmd'，否则热键会静默失效。"""
    if not hk:
        return ""
    out = []
    for k in [x.strip().lower() for x in str(hk).split('+') if x.strip()]:
        if k in ('ctrl', 'alt', 'shift', 'cmd', 'win'):
            out.append('<cmd>' if k == 'win' else '<%s>' % k)
        elif len(k) > 1:
            out.append('<%s>' % k)
        else:
            out.append(k)
    return '+'.join(out)

def to_point(value):
    """【修复】把上游输出统一成 [x, y]。图片 base64(str)、模长(float)、空值都会被判为非法坐标，
    避免后面 start_pos[0] 直接 TypeError 崩掉整个工作流。"""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def compute_aim_move(cur_x, cur_y, target_x, target_y, mode, gain, speed_coef, prev_distance, distance):
    """【智能置中核心算法】根据当前图像中心与目标位置，算出这一步该怎么移动鼠标。

    FPS 场景的关键：鼠标移动会带动"视角"旋转，画面内容朝相反方向移动。
      - 视角模式(mode='view')：想让画面里的图像右移 dx，鼠标要向左移 → 鼠标位移 = -dx * gain
      - 光标模式(mode='cursor')：直接拖动/移动光标，方向一致 → 鼠标位移 = +dx * gain

    参数：
      cur_x,cur_y        当前识别到的图像中心（屏幕绝对坐标）
      target_x,target_y  期望图像中心落到的位置（屏幕绝对坐标）
      mode               'view'(视角反向) / 'cursor'(光标同向)
      gain               有效灵敏度：鼠标移动1像素 ≈ 画面移动 gain 像素（由主循环在线辨识得到）
      speed_coef         步进比例(0<coef<=1)，小于1时分步逼近减少过冲
      prev_distance      上一步的距离（用于过冲/震荡检测）
      distance           当前距离
    返回：(move_x, move_y, new_speed_coef, stalled)
    """
    dx = target_x - cur_x   # 图像还需要移动的量
    dy = target_y - cur_y
    direction = -1.0 if mode == 'view' else 1.0

    # 自适应步长：过冲或无进展时减半；确实在收敛时才缓慢恢复。
    # （旧版 bug：只要"距离减少不足1px"就减半，导致正常收敛时越调越小卡死）
    new_speed = speed_coef
    if prev_distance is not None and distance > prev_distance - 0.5:
        new_speed = max(speed_coef * 0.5, 0.08)   # 过冲/停滞 → 减半
    else:
        new_speed = min(speed_coef * 1.05, 1.0)   # 真正在收敛 → 温和恢复

    raw_x = direction * dx * gain * new_speed
    raw_y = direction * dy * gain * new_speed

    # 用四舍五入而非 int() 截断，保证 ±1px 的微调也能生效；但仍保留符号
    def _rounded(v):
        r = int(round(v))
        if r == 0 and abs(v) >= 0.4:
            r = 1 if v > 0 else -1
        return r

    move_x = _rounded(raw_x)
    move_y = _rounded(raw_y)

    # 卡住判定：数值上几乎不动了（受限于鼠标 1px 精度）
    stalled = (move_x == 0 and move_y == 0)
    return move_x, move_y, new_speed, stalled


def estimate_effective_gain(prev_ix, prev_iy, ix, iy, prev_mx, prev_my, old_gain):
    """【在线灵敏度辨识】根据"上一步鼠标移动量"与"画面中图像实际位移"，
    反推出真实的游戏灵敏度（鼠标1px对应画面多少px）。这样即使用户 gain 填错，
    闭环几轮后也能自动校准到正确值。返回新的有效 gain。"""
    # 选位移较大的那个轴来估计，抗抖动
    if abs(prev_mx) >= abs(prev_my) and abs(prev_mx) > 0:
        moved = prev_ix - ix          # 鼠标 mx 后图像位移（view 模式符号已含在 mx 中）
        mouse = prev_mx
    elif abs(prev_my) > 0:
        moved = prev_iy - iy
        mouse = prev_my
    else:
        return old_gain
    if abs(mouse) < 1:
        return old_gain
    measured = abs(moved) / abs(mouse)
    # 只接受合理的测量值，避免识别跳变导致误校准；低通平滑
    if 0.05 <= measured <= 20.0:
        return old_gain * 0.5 + measured * 0.5
    return old_gain


def build_scan_monitor(node, sct, full_monitor):
    """【新增】图像识别节点勾选「识别范围」后，只在框选区域内截图：
    既省性能（截图与模板匹配面积骤减），又能避免屏幕其它相似区域造成误匹配。
    区域会被裁剪进虚拟桌面范围，防止 mss 越界报错。"""
    props = node.get('properties', {}) or {}
    if not props.get('use_region'):
        return full_monitor
    region = props.get('region')
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return full_monitor
    try:
        x1, y1, x2, y2 = [int(v) for v in region]
    except (TypeError, ValueError):
        return full_monitor
    if x2 - x1 < 2 or y2 - y1 < 2:
        return full_monitor
    try:
        mon0 = sct.monitors[0]
        l, t = int(mon0['left']), int(mon0['top'])
        r, b = l + int(mon0['width']), t + int(mon0['height'])
    except Exception:
        return full_monitor
    x1, y1 = max(x1, l), max(y1, t)
    x2, y2 = min(x2, r), min(y2, b)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return full_monitor
    return {'left': x1, 'top': y1, 'width': x2 - x1, 'height': y2 - y1}


def find_multi_color(img_bgr, points, tolerance):
    """【多点找色核心算法】在截图中定位一组带相对偏移的特征色点。

    参数：
      img_bgr   : 截图 numpy 数组，BGR 三通道
      points    : [{'dx','dy','color'}, ...]，第 0 个为主锚点(dx=dy=0)，color 为 [B,G,R]
                  其余点的 dx/dy 是相对主锚点的像素偏移
      tolerance : 每个颜色通道允许的绝对误差（0~255）
    返回：(found: bool, center_abs: (x,y) 相对截图左上角 或 None)
          找到时返回主锚点在截图中的坐标；调用方再加上 monitor 偏移即绝对坐标。
    """
    if not points or tolerance is None:
        return False, None
    try:
        tol = max(0, int(tolerance))
        pts = []
        for p in points:
            dx, dy = int(p.get('dx', 0)), int(p.get('dy', 0))
            c = p.get('color')
            if not isinstance(c, (list, tuple)) or len(c) < 3:
                return False, None
            b, g, r = int(c[0]), int(c[1]), int(c[2])
            pts.append((dx, dy, b, g, r))
    except (TypeError, ValueError, KeyError):
        return False, None

    h, w = img_bgr.shape[:2]
    main_dx, main_dy, mb, mg, mr = pts[0]
    # 相对偏移以第 0 个主锚点为基准，兼容手工编辑过 dx/dy 的旧工程
    rel_points = [(dx - main_dx, dy - main_dy, b, g, r) for dx, dy, b, g, r in pts[1:]]
    # 主点颜色掩膜：三通道都在容差内
    lo = np.array([max(0, mb - tol), max(0, mg - tol), max(0, mr - tol)], dtype=np.int16)
    hi = np.array([min(255, mb + tol), min(255, mg + tol), min(255, mr + tol)], dtype=np.int16)
    img_i = img_bgr.astype(np.int16)
    main_mask = (
        (img_i[:, :, 0] >= lo[0]) & (img_i[:, :, 0] <= hi[0]) &
        (img_i[:, :, 1] >= lo[1]) & (img_i[:, :, 1] <= hi[1]) &
        (img_i[:, :, 2] >= lo[2]) & (img_i[:, :, 2] <= hi[2])
    )
    # 主锚点理论偏移（相对截图左上角）：把主点自身 dx/dy 当作相对基准原点，通常 0
    ys, xs = np.nonzero(main_mask)
    if ys.size == 0:
        return False, None

    def color_ok(px, py, b, g, r):
        if px < 0 or px >= w or py < 0 or py >= h:
            return False
        pb, pg, pr = img_bgr[py, px]
        return (abs(int(pb) - b) <= tol and abs(int(pg) - g) <= tol and abs(int(pr) - r) <= tol)

    # 逐一校验候选主点：其余偏移点颜色都吻合才算命中
    for idx, (my, mx) in enumerate(zip(ys, xs)):
        # 大屏 + 低容差时候选点可能高达百万级，必须能随时响应托盘/热键停止
        if idx % 256 == 0 and not running:
            return False, None
        ok = True
        for dx, dy, b, g, r in rel_points:
            if not color_ok(mx + dx, my + dy, b, g, r):
                ok = False
                break
        if ok:
            return True, (int(mx), int(my))
    return False, None


def release_all_inputs(pdi):
    """【修复】任务被强制掐断时松开所有可能仍处于按下状态的键鼠，避免卡住 Ctrl / 左键。"""
    for k in ('ctrlleft', 'shiftleft', 'altleft', 'winleft', 'ctrl', 'shift', 'alt'):
        try: pdi.keyUp(k)
        except Exception: pass
    try:
        pyautogui.keyUp('ctrl'); pyautogui.keyUp('shift'); pyautogui.keyUp('alt'); pyautogui.keyUp('winleft')
    except Exception: pass
    # 【修复】只松开"当前确实处于按下状态"的鼠标键。
    # 之前无条件对 left/right/middle 各发一次 mouseUp，会向从未按下的右键发送孤立的
    # RIGHTUP —— Windows 桌面会把这当成一次右键点击并弹出右键菜单，
    # 表现为"明明点的是左键，桌面却冒出右键菜单"。
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for b, vk in (('left', 0x01), ('right', 0x02), ('middle', 0x04)):
            if user32.GetAsyncKeyState(vk) & 0x8000:
                try: pdi.mouseUp(button=b)
                except Exception: pass
    except Exception:
        # 非 Windows 环境：退化为只松开最常被卡住的左键
        try: pdi.mouseUp(button='left')
        except Exception: pass

def get_next_node_id(node, output_slot_index, links_dict):
    outputs = node.get('outputs', [])
    if len(outputs) > output_slot_index:
        out_links = outputs[output_slot_index].get('links', [])
        if out_links and len(out_links) > 0:
            link_id = out_links[0]
            if isinstance(link_id, dict): link_id = link_id.get('id') # 防御性转换：防止出现传入字典导致 unhashable 报错
            if not isinstance(link_id, (int, str)): return None
            link = links_dict.get(link_id)
            if link:
                return link[3]
    return None

def get_input_node_id(node, input_slot_index, links_dict):
    inputs = node.get('inputs', [])
    if len(inputs) > input_slot_index:
        link_id = inputs[input_slot_index].get('link')
        if link_id is not None:
            link = links_dict.get(link_id)
            if link:
                return link[1]
    return None

def execute_graph(graph_data, loop=None, ws=None, token=0):
    import time as _orig_time
    
    global running

    # 【修复】只有“全局未停止 且 自己仍是最新一次运行”才算存活，杜绝新旧线程互相干扰
    def is_active():
        return running and token == run_id

    # 【核心逻辑】局部挟持并重写 sleep，将长睡眠切分为高频轮询的 10ms 微切片
    class TaskStoppedException(BaseException): pass
    class TimeWrapper:
        @staticmethod
        def time(): return _orig_time.time()
        @staticmethod
        def sleep(secs):
            t0 = _orig_time.time()
            while _orig_time.time() - t0 < secs:
                if not is_active(): raise TaskStoppedException()
                _orig_time.sleep(0.01)
            if not is_active(): raise TaskStoppedException()
    time = TimeWrapper()

    print("【后端】开始解析任务流...")
    

    nodes_list = graph_data.get('nodes', [])
    links_list = graph_data.get('links', [])
    
    nodes = {n['id']: n for n in nodes_list}
    links = {l[0]: l for l in links_list if l}
    context = {} 

    def eval_input(node, slot_index):
        inputs = node.get('inputs', [])
        if len(inputs) > slot_index:
            link_id = inputs[slot_index].get('link')
            if isinstance(link_id, dict): link_id = link_id.get('id') # 防御性转换
            if link_id is not None:
                link = links.get(link_id)
                if link:
                    orig_node = nodes.get(link[1])
                    return eval_output(orig_node, link[2])
        return None

    def eval_output(node, slot_index):
        if not node: return None
        ntype = node.get('type')
        if ntype == 'AutoTask/Coordinate' or ntype == 'AutoTask/Position':
            return [node.get('properties', {}).get('x', 0), node.get('properties', {}).get('y', 0)]
        elif ntype == 'AutoTask/VectorMath':
            a = eval_input(node, 0) or [0, 0]
            b = eval_input(node, 1) or [0, 0]
            mtype = node.get('properties', {}).get('math_type', '差向量')
            if mtype == '差向量': return [a[0]-b[0], a[1]-b[1]]
            elif mtype == '和向量': return [a[0]+b[0], a[1]+b[1]]
            elif mtype == '模长': return ((a[0])**2 + (a[1])**2)**0.5
        elif ntype == 'AutoTask/CurrentMouse':
            return list(pyautogui.position())
        elif ntype == 'AutoTask/Image':
            return node.get('properties', {}).get('image_base64', '')
        elif ntype == 'AutoTask/Recognize':
            return context.get('match_coords', [0, 0])
        elif ntype == 'AutoTask/MultiColor':
            return context.get('multicolor_coords', [0, 0])
        return None

    def decode_base64(b64_str):
        if not b64_str: return None
        if ',' in b64_str: b64_str = b64_str.split(',')[1]
        import base64
        try:
            img_data = base64.b64decode(b64_str)
            nparr = np.frombuffer(img_data, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_UNCHANGED)
        except: return None
        
    def match_template_logic(node_data, confidence, sct, monitor):
        if not node_data: return False, None
        if isinstance(node_data, str):
            template = decode_base64(node_data)
            if template is None: return False, None
            h, w = template.shape[:2]
            img = np.array(sct.grab(monitor))
            img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            
            # 【修复 2026-11-20】原实现把 RGBA 模板一律交给 CCORR_NORMED + alpha 当 mask；
            # 但截图模板的 alpha 通常是全 255（完全不透明），mask 形同虚设，
            # 而 CCORR 只比较亮度乘积、不减均值，纯白/浅色区域也能拿到 0.93+，
            # 这就是相似度调到 0.95 仍然误判的根因。改为：
            #   ① alpha 全不透明 → 剥掉 alpha，灰度 + CCOEFF_NORMED（只看结构，免疫亮度/色偏）
            #   ② alpha 确实有透明 → 保留 mask，用彩色 CCOEFF_NORMED
            #   ③ CCOEFF 在纯色区域可能产生 nan → 统一压成 -1，宁可漏判不误判
            try:
                template_alpha = None
                if len(template.shape) == 3 and template.shape[2] == 4:
                    alpha = template[:, :, 3]
                    if int(alpha.min()) < 255:
                        template_alpha = alpha
                    template = template[:, :, :3]

                if template_alpha is not None:
                    res = cv2.matchTemplate(img_bgr, template, cv2.TM_CCOEFF_NORMED, mask=template_alpha)
                else:
                    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
                    tpl_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template
                    res = cv2.matchTemplate(img_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)

                if not np.isfinite(res).all():
                    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
            except cv2.error:
                # 模板比屏幕大 / 通道异常时不再让整个工作流崩溃
                return False, None

            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            if max_val >= confidence:
                # 【修复】截图坐标是相对裁剪区域的，必须叠加显示器左上角偏移，多屏/主屏非 0,0 时否则全偏
                ox = int(monitor.get('left', 0)) if isinstance(monitor, dict) else 0
                oy = int(monitor.get('top', 0)) if isinstance(monitor, dict) else 0
                return True, [int(max_loc[0] + w // 2) + ox, int(max_loc[1] + h // 2) + oy]
            return False, None

    current_node = next((n for n in nodes.values() if n['type'] == 'AutoTask/Start'), None)
    if not current_node:
        print("【后端】未找到“开始”节点，任务终止。")
        running = False
        return

    end_node = next((n for n in nodes.values() if n['type'] == 'AutoTask/End'), None)
    stop_hotkey_str = end_node.get('properties', {}).get('hotkey', '') if end_node else ''
    stop_listener = None
    
    if stop_hotkey_str:
        from pynput import keyboard
        def on_activate():
            global running
            running = False
            print("【后端】🛑 触发结束热键，强制停止工作流！")
            
        parsed_hk = parse_hotkey(stop_hotkey_str)
        try:
            stop_listener = keyboard.GlobalHotKeys({parsed_hk: on_activate})
            stop_listener.start()
        except Exception as e:
            # 【修复】原来这里是 except: pass，热键写错（比如 WIN）时会静默失效
            print(f"【后端】[警告] 结束热键 [{stop_hotkey_str}] 注册失败: {e}")

    try:
        import pydirectinput
        pydirectinput.PAUSE = False
        pdi = pydirectinput
    except ImportError:
        pyautogui.PAUSE = 0
        pdi = pyautogui

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        
        try:
            while is_active() and current_node:
                if loop and ws:
                    try:
                        import asyncio
                        asyncio.run_coroutine_threadsafe(
                            ws.send_text(json.dumps({"status": "executing_node", "node_id": current_node['id']})),
                            loop
                        )
                    except Exception: pass

                node_type = current_node['type']
                next_node_id = None
                
                if node_type == 'AutoTask/Start':
                    print("\n【执行】任务开始")
                    next_node_id = get_next_node_id(current_node, 0, links)
                    
                elif node_type == 'AutoTask/Recognize':
                    print("【执行】图像识别节点")
                    confidence = current_node.get('properties', {}).get('confidence', 0.8)
                    image_input_data = eval_input(current_node, 1)
                    
                    # 【新增】勾选「识别范围」后只在框选区域内截图
                    scan_monitor = build_scan_monitor(current_node, sct, monitor)
                    if scan_monitor is not monitor:
                        print(f"  -> 限定识别范围: [{scan_monitor['left']}, {scan_monitor['top']}] "
                              f"{scan_monitor['width']}×{scan_monitor['height']}")
                    
                    if not image_input_data:
                        # 没有图片输入时 match_template_logic 会每轮都返回 False，
                        # 循环工程里会疯狂刷日志；直接走“未识别”分支更合理
                        print("  -> [警告] 图像识别节点未连接图片输入，按未识别处理")
                        next_node_id = get_next_node_id(current_node, 1, links)
                    else:
                        found, coords = match_template_logic(image_input_data, confidence, sct, scan_monitor)
                        if found:
                            context['match_coords'] = coords
                            print(f"  -> ✅ 识别成功，坐标: {coords}")
                            next_node_id = get_next_node_id(current_node, 0, links)
                        else:
                            print("  -> ❌ 未识别到目标")
                            next_node_id = get_next_node_id(current_node, 1, links)

                elif node_type == 'AutoTask/MultiColor':
                    print("【执行】多点找色节点")
                    props = current_node.get('properties', {})
                    points = props.get('points', [])
                    tolerance = props.get('tolerance', 10)
                    scan_monitor = build_scan_monitor(current_node, sct, monitor)
                    if not points:
                        print("  -> [警告] 尚未拾取任何特征点，已跳过")
                        next_node_id = get_next_node_id(current_node, 1, links)
                    else:
                        img = np.array(sct.grab(scan_monitor))
                        img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                        found, local = find_multi_color(img_bgr, points, tolerance)
                        if found:
                            ox = int(scan_monitor.get('left', 0)) if isinstance(scan_monitor, dict) else 0
                            oy = int(scan_monitor.get('top', 0)) if isinstance(scan_monitor, dict) else 0
                            coords = [local[0] + ox, local[1] + oy]
                            context['multicolor_coords'] = coords
                            print(f"  -> ✅ 多点找色命中，主点坐标: {coords}")
                            if loop and ws:
                                try:
                                    import asyncio
                                    asyncio.run_coroutine_threadsafe(
                                        ws.send_text(json.dumps({"status": "multicolor_result", "node_id": current_node['id'],
                                                                 "found": True, "coords": coords}, ensure_ascii=False)), loop)
                                except Exception: pass
                            next_node_id = get_next_node_id(current_node, 0, links)
                        else:
                            print("  -> ❌ 多点找色未命中")
                            if loop and ws:
                                try:
                                    import asyncio
                                    asyncio.run_coroutine_threadsafe(
                                        ws.send_text(json.dumps({"status": "multicolor_result", "node_id": current_node['id'],
                                                                 "found": False}, ensure_ascii=False)), loop)
                                except Exception: pass
                            next_node_id = get_next_node_id(current_node, 1, links)

                elif node_type == 'AutoTask/MouseDown' or node_type == 'AutoTask/MouseUp':
                    btn = current_node.get('properties', {}).get('button', 'left')
                    is_down = (node_type == 'AutoTask/MouseDown')
                    print(f"【执行】硬件级鼠标{'按下' if is_down else '抬起'} ({btn})")
                    if is_down:
                        pdi.mouseDown(button=btn)
                    else:
                        pdi.mouseUp(button=btn)
                    time.sleep(0.05)
                    next_node_id = get_next_node_id(current_node, 0, links)

                elif node_type == 'AutoTask/MouseClick':
                    btn = current_node.get('properties', {}).get('button', 'left')
                    pre_delay = current_node.get('properties', {}).get('pre_delay', 0) / 1000.0
                    duration = current_node.get('properties', {}).get('duration', 50) / 1000.0
                    post_delay = current_node.get('properties', {}).get('post_delay', 50) / 1000.0
                    
                    print(f"【执行】硬件级鼠标点击 ({btn})")
                    if pre_delay > 0: time.sleep(pre_delay)
                    pdi.mouseDown(button=btn)
                    time.sleep(duration)
                    pdi.mouseUp(button=btn)
                    if post_delay > 0: time.sleep(post_delay)
                    
                    next_node_id = get_next_node_id(current_node, 0, links)

                elif node_type == 'AutoTask/KeyPress':
                    key = current_node.get('properties', {}).get('key', 'space').lower()
                    pre_delay = current_node.get('properties', {}).get('pre_delay', 0) / 1000.0
                    duration = current_node.get('properties', {}).get('duration', 50) / 1000.0
                    post_delay = current_node.get('properties', {}).get('post_delay', 50) / 1000.0
                    
                    print(f"【执行】硬件级键盘点击: {key}")
                    if pre_delay > 0: time.sleep(pre_delay)
                    if key:
                        keys = [k.strip() if k.strip() != 'win' else 'winleft' for k in key.lower().split('+')]
                        for k in keys:
                            if k == 'winleft': pyautogui.keyDown(k)
                            else:
                                try: pdi.keyDown(k)
                                except Exception: pyautogui.keyDown(k)
                        time.sleep(duration)
                        for k in reversed(keys):
                            if k == 'winleft': pyautogui.keyUp(k)
                            else:
                                try: pdi.keyUp(k)
                                except Exception: pyautogui.keyUp(k)
                    if post_delay > 0: time.sleep(post_delay)
                    
                    next_node_id = get_next_node_id(current_node, 0, links)

                elif node_type == 'AutoTask/KeyDown':
                    key = current_node.get('properties', {}).get('key', 'space').lower()
                    print(f"【执行】硬件级键盘按下: {key}")
                    if key:
                        keys = [k.strip() if k.strip() != 'win' else 'winleft' for k in key.lower().split('+')]
                        for k in keys:
                            if k == 'winleft': pyautogui.keyDown(k)
                            else:
                                try: pdi.keyDown(k)
                                except Exception: pyautogui.keyDown(k)
                    time.sleep(0.05)
                    next_node_id = get_next_node_id(current_node, 0, links)

                elif node_type == 'AutoTask/KeyUp':
                    key = current_node.get('properties', {}).get('key', 'space').lower()
                    print(f"【执行】硬件级键盘抬起: {key}")
                    if key:
                        keys = [k.strip() if k.strip() != 'win' else 'winleft' for k in key.lower().split('+')]
                        # 组合键必须按按下顺序的逆序释放，否则 Ctrl/Shift 会先于普通键抬起
                        for k in reversed(keys):
                            if k == 'winleft': pyautogui.keyUp(k)
                            else:
                                try: pdi.keyUp(k)
                                except Exception: pyautogui.keyUp(k)
                    time.sleep(0.05)
                    next_node_id = get_next_node_id(current_node, 0, links)

                elif node_type == 'AutoTask/MouseMove':
                    print("【执行】硬件级鼠标指针移动")
                    # 【修复】统一校验成 [x, y]，接错类型（图片字符串 / 模长标量）不再 TypeError 崩溃
                    start_pos = to_point(eval_input(current_node, 1))
                    if not start_pos:
                        start_pos = list(pyautogui.position())
                    end_pos = to_point(eval_input(current_node, 2))
                    if not end_pos:
                        end_pos = start_pos
                        
                    duration = current_node.get('properties', {}).get('duration', 1000) / 1000.0
                    jitter = current_node.get('properties', {}).get('jitter', 0.0)

                    import random
                    steps = max(1, int(duration * 50))
                    for i in range(steps):
                        t = (i + 1) / steps
                        cx = start_pos[0] + (end_pos[0] - start_pos[0]) * t
                        cy = start_pos[1] + (end_pos[1] - start_pos[1]) * t
                        if jitter > 0: cx += random.uniform(-jitter, jitter); cy += random.uniform(-jitter, jitter)
                        pdi.moveTo(int(cx), int(cy))
                        if duration > 0: time.sleep(duration / steps)
                    next_node_id = get_next_node_id(current_node, 0, links)
                
                elif node_type == 'AutoTask/CenterAim':
                    print("【执行】智能置中节点开始工作...")
                    props = current_node.get('properties', {})
                    mode = props.get('mode', 'view')                       # view=视角反向(FPS) / cursor=光标同向
                    gain = float(props.get('gain', 1.0))                   # 灵敏度补偿：鼠标1px≈画面gain px
                    speed_coef = float(props.get('speed_coef', 0.8))       # 步进比例
                    tolerance = float(props.get('tolerance', 10))
                    confidence = float(props.get('confidence', 0.8))
                    max_iterations = max(1, int(props.get('max_iterations', 25)))
                    settle_ms = int(props.get('settle_ms', 120))           # 每次移动后等画面稳定
                    screen_w, screen_h = pyautogui.size()
                    target_x = int(props.get('target_x', screen_w // 2))
                    target_y = int(props.get('target_y', screen_h // 2))
                    speed_coef = max(0.05, min(speed_coef, 1.0))

                    img_input = eval_input(current_node, 1)
                    reached = False
                    if not img_input:
                        print("  -> [警告] 智能置中缺少图像输入，已跳过")
                    else:
                        print(f"  -> 目标位置 ({target_x}, {target_y}) 模式={'视角反向' if mode=='view' else '光标同向'} 灵敏度补偿={gain}")
                        prev_distance = None
                        same_count = 0
                        last_distance = None
                        gain_eff = gain                 # 有效灵敏度，随闭环在线校准
                        prev_ix = prev_iy = None
                        prev_mx = prev_my = 0
                        for attempt in range(max_iterations):
                            found, coords = match_template_logic(img_input, confidence, sct, monitor)
                            coords = to_point(coords) if found else None
                            if not coords:
                                print("  -> [警告] 画面中未识别到该图像，无法置中")
                                break
                            ix, iy = int(coords[0]), int(coords[1])

                            # 用上一轮的"鼠标移动 vs 画面位移"在线校准真实灵敏度
                            if prev_ix is not None and (prev_mx or prev_my):
                                new_gain = estimate_effective_gain(prev_ix, prev_iy, ix, iy,
                                                                   prev_mx, prev_my, gain_eff)
                                if abs(new_gain - gain_eff) > 0.01:
                                    print(f"  -> [校准] 有效灵敏度 {gain_eff:.2f} → {new_gain:.2f}")
                                gain_eff = new_gain

                            dx, dy = target_x - ix, target_y - iy
                            distance = (dx * dx + dy * dy) ** 0.5
                            print(f"  -> 第 {attempt + 1}/{max_iterations} 次: 图像中心({ix},{iy}) 偏差({dx},{dy}) 距离{distance:.1f}px")
                            if distance <= tolerance:
                                print(f"  -> ✅ 图像已置于指定位置（误差 {distance:.1f}px）")
                                reached = True
                                break

                            mx, my, speed_coef, stalled = compute_aim_move(
                                ix, iy, target_x, target_y, mode, gain_eff, speed_coef, prev_distance, distance)
                            prev_distance = distance
                            if stalled:
                                print("  -> [提示] 已到鼠标精度极限，无法再微调")
                                break

                            pdi.moveRel(mx, my)
                            prev_ix, prev_iy = ix, iy
                            prev_mx, prev_my = mx, my
                            time.sleep(settle_ms / 1000.0)

                            # 卡死检测：连续多次距离几乎不变且未收敛 → 判定失败，避免空转
                            if last_distance is not None and abs(distance - last_distance) < 0.5:
                                same_count += 1
                            else:
                                same_count = 0
                            last_distance = distance
                            if same_count >= 5:
                                print("  -> [警告] 连续多次无进展，疑似方向/灵敏度设置不当，已中止")
                                break
                        else:
                            print(f"  -> [警告] 达到最大迭代次数 {max_iterations} 仍未到位（最终误差 {distance:.1f}px）")

                    # 输出0=到位/完成，输出1=未到位（失败时可接分支处理）
                    next_node_id = get_next_node_id(current_node, 0, links) if reached else get_next_node_id(current_node, 1, links)
                    
                elif node_type == 'AutoTask/Delay':
                    props = current_node.get('properties', {})
                    if props.get('random', False):
                        import random
                        min_ms = props.get('min_ms', 500)
                        max_ms = props.get('max_ms', 2000)
                        delay_ms = random.uniform(min_ms, max_ms)
                        print(f"【执行】延迟节点，随机等待 {min_ms}~{max_ms} ms，本次: {delay_ms:.0f} ms...")
                    else:
                        delay_ms = props.get('delay_ms', 1000)
                        print(f"【执行】延迟节点，等待 {delay_ms} ms...")
                    time.sleep(delay_ms / 1000.0)
                    next_node_id = get_next_node_id(current_node, 0, links)
                    
                elif node_type == 'AutoTask/Counter':
                    node_id = current_node['id']
                    max_count = int(current_node.get('properties', {}).get('max_count', 5))  # 整数化：防旧工程残留小数
                    # 【修复】真正读取「数字输入」端口，之前这个端口是摆设
                    dyn = eval_input(current_node, 1)
                    if isinstance(dyn, (int, float)):
                        max_count = int(dyn)
                    elif isinstance(dyn, (list, tuple)) and len(dyn) >= 1 and isinstance(dyn[0], (int, float)):
                        max_count = int(dyn[0])
                    if node_id not in context:
                        context[node_id] = 0
                    context[node_id] += 1
                    print(f"【执行】计数节点: 当前 {context[node_id]} / 目标 {max_count}")
                    # 推送当前计数到前端节点显示
                    if loop and ws:
                        try:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                ws.send_text(json.dumps({
                                    "status": "counter_update",
                                    "node_id": node_id,
                                    "value": context[node_id],
                                    "max": max_count
                                }, ensure_ascii=False)), loop)
                        except Exception: pass
                    
                    if context[node_id] <= max_count:
                        next_node_id = get_next_node_id(current_node, 0, links) 
                    else:
                        next_node_id = get_next_node_id(current_node, 1, links)
                        context[node_id] = 0
                        
                elif node_type in ['AutoTask/ReturnTo', 'AutoTask/Return']:
                    print(f"【执行】循环跳转节点 ({node_type})")
                    if node_type == 'AutoTask/Return':
                        _r_delay = current_node.get('properties', {}).get('delay_ms', 0) / 1000.0
                        if _r_delay > 0: time.sleep(_r_delay)
                    next_node_id = get_next_node_id(current_node, 0, links)
                    
                elif node_type == 'AutoTask/MacroScript':
                    script_content = current_node.get('properties', {}).get('script_text', '')
                    print("【执行】动作宏节点，开始逐行解析宏指令...")
                    lines = [l.strip() for l in script_content.split('\n') if l.strip()]
                    for line in lines:
                        if not is_active(): break
                        print(f"  -> 执行宏指令: {line}")
                        # 录制器可能写入连续空格或多空格，用 split() 统一切分
                        parts = line.split()
                        if not parts: continue
                        cmd = parts[0].upper()
                        try:
                            if cmd == 'DELAY':
                                ms = int(parts[1].replace('ms', ''))
                                time.sleep(ms / 1000.0)
                            elif cmd == 'CLICK':
                                btn = parts[1].lower()
                                coords = [int(c) for c in parts[2].split(',')]
                                duration = int(parts[3].replace('ms', '')) / 1000.0
                                pdi.moveTo(coords[0], coords[1])
                                time.sleep(0.04)
                                pdi.mouseDown(button=btn)
                                time.sleep(duration)
                                pdi.mouseUp(button=btn)
                            elif cmd == 'DRAG':
                                btn = parts[1].lower()
                                s_coords = [int(c) for c in parts[2].replace('S', '').split(',')]
                                e_coords = [int(c) for c in parts[3].replace('E', '').split(',')]
                                duration = int(parts[4].replace('ms', '')) / 1000.0
                                pdi.moveTo(s_coords[0], s_coords[1])
                                time.sleep(0.04)
                                pdi.mouseDown(button=btn)
                                time.sleep(0.05)
                                steps = max(1, int(duration * 80))
                                for s in range(steps):
                                    t = (s + 1) / steps
                                    cx = s_coords[0] + (e_coords[0] - s_coords[0]) * t
                                    cy = s_coords[1] + (e_coords[1] - s_coords[1]) * t
                                    pdi.moveTo(int(cx), int(cy))
                                    time.sleep(duration / steps)
                                pdi.mouseUp(button=btn)
                            elif cmd == 'KEYDOWN':
                                k = parts[1].lower()
                                if k in ['ctrl', 'ctrl_l', 'ctrl_r']: k = 'ctrlleft'
                                elif k in ['shift', 'shift_l', 'shift_r']: k = 'shiftleft'
                                elif k in ['alt', 'alt_l', 'alt_r']: k = 'altleft'
                                try: 
                                    pdi.keyDown(k)
                                    if 'ctrl' in k or 'shift' in k or 'alt' in k:
                                        time.sleep(0.03)
                                except: 
                                    try: pyautogui.keyDown(k)
                                    except: pass
                            elif cmd == 'KEYUP':
                                k = parts[1].lower()
                                if k in ['ctrl', 'ctrl_l', 'ctrl_r']: k = 'ctrlleft'
                                elif k in ['shift', 'shift_l', 'shift_r']: k = 'shiftleft'
                                elif k in ['alt', 'alt_l', 'alt_r']: k = 'altleft'
                                try: pdi.keyUp(k)
                                except: 
                                    try: pyautogui.keyUp(k)
                                    except: pass
                            elif cmd == 'SCROLL':
                                amt = int(parts[1].split(',')[1])
                                # 【修复】pydirectinput 没有 scroll()，原来这里必定抛 AttributeError 被吞掉
                                pyautogui.scroll(amt * 100)
                        except Exception as ex:
                            print(f"  -> [警告] 宏指令解析出错 [{line}]: {ex}")
                    next_node_id = get_next_node_id(current_node, 0, links)
                    
                elif node_type == 'AutoTask/End':
                    print("【执行】结束节点，直接终止任务流。")
                    break
                
                if next_node_id:
                    current_node = nodes.get(next_node_id)
                else:
                    print("【后端】任务流执行完毕或断开连接，暂停。")
                    break
                time.sleep(0.05)
                
        except TaskStoppedException:
            print("【后端】🛑 检测到停止热键，正在运行的长时节点已被强制掐断。")
        except Exception as e:
            import traceback
            err_msg = f"❌ 【致命错误】工作流执行崩溃: {str(e)}\n{traceback.format_exc()}"
            print(err_msg)
            
        finally:
            # 【修复】无论正常结束还是被热键掐断，都松开可能残留的按键/鼠标
            if stop_listener: stop_listener.stop()
            release_all_inputs(pdi)
            
    if token == run_id:
        # 【修复】只有最新一次运行才有权把全局 running 置回 False，避免旧线程掐死新任务
        running = False
    print("【后端】任务已停止。")
    if loop and ws:
        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(ws.send_text(json.dumps({"status": "stopped"})), loop)
        except Exception: pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    import asyncio
    global running, global_editor_listener, run_id, _LOG_WS, _LOG_LOOP
    await websocket.accept()
    _LOG_WS = websocket
    _LOG_LOOP = asyncio.get_running_loop()
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            if payload.get("action") == "unsaved_decision":
                # 未保存工程确认结果：唤醒等待中的托盘退出/窗口关闭线程
                _resolve_unsaved_decision(payload.get("requestId"), payload.get("decision"))
            elif payload.get("action") == "start":
                if not running:
                    run_id += 1              # 【修复】开启新的一代，旧线程收尾时会被判定为已失效
                    running = True
                    import asyncio
                    loop = asyncio.get_running_loop()
                    threading.Thread(target=execute_graph, args=(payload.get("graph"), loop, websocket, run_id), daemon=True).start()
                    await websocket.send_text(json.dumps({"status": "running"}))
            elif payload.get("action") == "stop":
                running = False
                await websocket.send_text(json.dumps({"status": "stopped"}))
                
            elif payload.get("action") == "update_hotkeys":
                if global_editor_listener:
                    global_editor_listener.stop()
                
                from pynput import keyboard
                start_hk = payload.get("start", "")
                stop_hk = payload.get("stop", "")
                
                import asyncio
                current_loop = asyncio.get_running_loop()
                
                def on_start_hk():
                    try: asyncio.run_coroutine_threadsafe(websocket.send_text(json.dumps({"status": "hotkey_start"})), current_loop)
                    except: pass
                    
                def on_stop_hk():
                    try: asyncio.run_coroutine_threadsafe(websocket.send_text(json.dumps({"status": "hotkey_stop"})), current_loop)
                    except: pass
                    
                mapping = {}
                try:
                    p_start, p_stop = parse_hotkey(start_hk), parse_hotkey(stop_hk)
                    if start_hk and stop_hk and p_start == p_stop:
                        print("【后端】[警告] 启动热键与停止热键相同，启动将被忽略，请改成不同按键！")
                        p_start = ""
                    if p_start: mapping[p_start] = on_start_hk
                    if p_stop: mapping[p_stop] = on_stop_hk
                    if mapping:
                        global_editor_listener = keyboard.GlobalHotKeys(mapping)
                        global_editor_listener.start()
                except Exception as e:
                    # 【修复】原来静默 pass，热键写错时用户完全不知情
                    print(f"【后端】[警告] 编辑器热键注册失败 (start={start_hk}, stop={stop_hk}): {e}")
                
            elif payload.get("action") == "pick_coordinate":
                import asyncio
                import tkinter as tk
                print("\n【后端】进入全局坐标拾取模式，已生成系统级全屏覆盖层...")
                loop = asyncio.get_running_loop()
                future = loop.create_future()

                def show_fullscreen_overlay():
                    root = tk.Toplevel(_tk_service.root())
                    root.attributes("-alpha", 0.5)
                    root.overrideredirect(True)
                    root.attributes("-topmost", True)
                    root.configure(bg='black', cursor="crosshair")

                    with mss.mss() as sct:
                        mon0 = sct.monitors[0]
                        root.geometry(f"{mon0['width']}x{mon0['height']}+{mon0['left']}+{mon0['top']}")

                        for i in range(1, len(sct.monitors)):
                            m = sct.monitors[i]
                            center_x = m['left'] + m['width'] // 2 - mon0['left']
                            center_y = m['top'] + m['height'] // 2 - mon0['top']

                            lbl = tk.Label(root, text=f"🎯 坐标拾取模式\n请在当前屏幕 [{i}] 点击获取坐标 (按 ESC 取消)", font=("Microsoft YaHei", 32, "bold"), fg="white", bg="black")
                            lbl.place(x=center_x, y=center_y, anchor="center")

                    def on_click(event):
                        x, y = event.x_root, event.y_root
                        root.destroy()
                        if not future.done():
                            loop.call_soon_threadsafe(future.set_result, (x, y))

                    def on_esc(event):
                        root.destroy()
                        if not future.done():
                            loop.call_soon_threadsafe(future.set_result, (None, None))

                    root.bind("<Button-1>", on_click)
                    root.bind("<Escape>", on_esc)

                    root.focus_force()

                _tk_service.submit(show_fullscreen_overlay)

                x, y = await future
                if x is not None and y is not None:
                    print(f"【后端】已捕获全局点击坐标: X={x}, Y={y}，正传回...")
                    await websocket.send_text(json.dumps({"status": "coordinate_picked", "x": x, "y": y}))
                else:
                    print("【后端】已取消坐标拾取。")
                    await websocket.send_text(json.dumps({"status": "coordinate_picked", "x": None, "y": None}))

            elif payload.get("action") == "pick_region":
                # 【新增】图像识别节点的「识别范围」框选：全屏置灰 + 左键拖拽出矩形
                import asyncio
                import tkinter as tk
                print("\n【后端】进入识别范围框选模式：按住左键拖出矩形，松开确认 (按 ESC 取消)...")
                loop = asyncio.get_running_loop()
                future = loop.create_future()

                def show_region_overlay():
                    root = tk.Toplevel(_tk_service.root())
                    root.attributes("-alpha", 0.5)
                    root.overrideredirect(True)
                    root.attributes("-topmost", True)
                    root.configure(bg='black', cursor="crosshair")

                    with mss.mss() as sct:
                        mon0 = sct.monitors[0]
                        root.geometry(f"{mon0['width']}x{mon0['height']}+{mon0['left']}+{mon0['top']}")

                    canvas = tk.Canvas(root, bg='black', highlightthickness=0, cursor="crosshair")
                    canvas.pack(fill=tk.BOTH, expand=True)

                    hint_id = canvas.create_text(
                        mon0['width'] / 2, mon0['height'] / 2,
                        text="🎯 框选识别范围\n按住鼠标左键拖出矩形，松开确认\n（按 ESC 取消，拖动小于 5px 视为取消）",
                        fill="white", font=("Microsoft YaHei", 26, "bold"), justify="center")

                    start_pos = {"x": 0, "y": 0}
                    rect = {"id": 0, "size_id": 0}   # 0 表示尚未绘制

                    def finish(result):
                        root.destroy()
                        if not future.done():
                            loop.call_soon_threadsafe(future.set_result, result)

                    def on_press(event):
                        start_pos["x"], start_pos["y"] = event.x_root, event.y_root
                        canvas.delete(hint_id)
                        for key in ("id", "size_id"):
                            if rect[key]:
                                canvas.delete(rect[key])
                                rect[key] = 0

                    def on_drag(event):
                        for key in ("id", "size_id"):
                            if rect[key]:
                                canvas.delete(rect[key])
                                rect[key] = 0
                        x0, y0 = start_pos["x"], start_pos["y"]
                        x1, y1 = event.x_root, event.y_root
                        # 画布坐标 = 屏幕坐标 - 虚拟桌面原点
                        cx0, cy0 = x0 - mon0['left'], y0 - mon0['top']
                        cx1, cy1 = x1 - mon0['left'], y1 - mon0['top']
                        rect["id"] = canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="#4CAF50", width=2,
                                                             fill="#4CAF50", stipple="gray25")
                        rect["size_id"] = canvas.create_text((cx0 + cx1) / 2, (cy0 + cy1) / 2,
                                                             text=f"{abs(x1 - x0)} × {abs(y1 - y0)}",
                                                             fill="white", font=("Microsoft YaHei", 16, "bold"))

                    def on_release(event):
                        x0, y0 = start_pos["x"], start_pos["y"]
                        x1, y1 = event.x_root, event.y_root
                        if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
                            print("【后端】框选区域过小，视为取消。")
                            finish(None)
                            return
                        finish([int(min(x0, x1)), int(min(y0, y1)), int(max(x0, x1)), int(max(y0, y1))])

                    def on_esc(event):
                        finish(None)

                    canvas.bind("<ButtonPress-1>", on_press)
                    canvas.bind("<B1-Motion>", on_drag)
                    canvas.bind("<ButtonRelease-1>", on_release)
                    root.bind("<Escape>", on_esc)
                    canvas.bind("<Escape>", on_esc)

                    root.focus_force()

                _tk_service.submit(show_region_overlay)

                region = await future
                if region:
                    print(f"【后端】已捕获识别范围: {region}（{region[2]-region[0]}×{region[3]-region[1]}）")
                    await websocket.send_text(json.dumps({"status": "region_picked", "region": region}))
                else:
                    print("【后端】已取消区域框选。")
                    await websocket.send_text(json.dumps({"status": "region_picked", "region": None}))

            elif payload.get("action") == "pick_colors":
                # 【多点找色】半透明全屏，依次点击拾取特征点（第1个为主锚点）；右键/回车结束，ESC 取消
                import asyncio
                import tkinter as tk
                print("\n【后端】进入多点找色拾取模式：依次左键点击特征点（第1个为主点），右键或回车结束，ESC 取消...")
                loop = asyncio.get_running_loop()
                future = loop.create_future()

                def show_color_picker():
                    root = tk.Toplevel(_tk_service.root())
                    root.attributes("-alpha", 0.45)
                    root.overrideredirect(True)
                    root.attributes("-topmost", True)
                    root.configure(bg='black', cursor="crosshair")
                    with mss.mss() as sct:
                        mon0 = sct.monitors[0]
                        root.geometry(f"{mon0['width']}x{mon0['height']}+{mon0['left']}+{mon0['top']}")

                    canvas = tk.Canvas(root, bg='black', highlightthickness=0, cursor="crosshair")
                    canvas.pack(fill=tk.BOTH, expand=True)
                    hint = canvas.create_text(
                        mon0['width'] / 2, 40,
                        text="🎯 多点找色拾取：左键依次点击特征点（第 1 个是主锚点）\n右键 / 回车 完成，ESC 取消",
                        fill="white", font=("Microsoft YaHei", 22, "bold"), justify="center")

                    picked = []   # [(x_root, y_root, (b,g,r), marker_id, text_id)]

                    def finish(result):
                        root.destroy()
                        if not future.done():
                            loop.call_soon_threadsafe(future.set_result, result)

                    def pixel_color(x, y):
                        with mss.mss() as sct:
                            mon = sct.monitors[0]
                            px = x - mon['left']; py = y - mon['top']
                            shot = sct.grab({'left': x, 'top': y, 'width': 1, 'height': 1})
                            # mss 返回 BGRA
                            return int(shot.pixel(0, 0)[0]), int(shot.pixel(0, 0)[1]), int(shot.pixel(0, 0)[2])

                    def on_click(event):
                        canvas.delete(hint)
                        x, y = event.x_root, event.y_root
                        b, g, r = pixel_color(x, y)
                        cx, cy = x - mon0['left'], y - mon0['top']
                        idx = len(picked) + 1
                        hexcol = '#%02x%02x%02x' % (r, g, b)
                        mid = canvas.create_oval(cx-8, cy-8, cx+8, cy+8, outline=hexcol, width=2)
                        tid = canvas.create_text(cx, cy-18, text=str(idx), fill=hexcol,
                                                 font=("Microsoft YaHei", 14, "bold"))
                        picked.append((x, y, (b, g, r), mid, tid))
                        print(f"【拾色】点 {idx}: ({x},{y}) BGR=({b},{g},{r})")

                    def on_finish(event):
                        if not picked:
                            finish(None)
                            return
                        result = []
                        ax, ay, _, _, _ = picked[0]
                        for x, y, (b, g, r), _, _ in picked:
                            result.append({'x': x, 'y': y, 'dx': x - ax, 'dy': y - ay,
                                           'color': [b, g, r]})
                        finish(result)

                    def on_esc(event):
                        finish(None)

                    canvas.bind("<Button-1>", on_click)
                    canvas.bind("<Button-3>", on_finish)   # 右键完成
                    root.bind("<Return>", on_finish)        # 回车完成
                    root.bind("<Escape>", on_esc)
                    root.focus_force()

                _tk_service.submit(show_color_picker)

                picked_points = await future
                if picked_points:
                    print(f"【后端】已拾取 {len(picked_points)} 个特征点，传回前端...")
                    await websocket.send_text(json.dumps({"status": "colors_picked", "points": picked_points}, ensure_ascii=False))
                else:
                    print("【后端】已取消多点找色拾取。")
                    await websocket.send_text(json.dumps({"status": "colors_picked", "points": None}, ensure_ascii=False))

            elif payload.get("action") == "start_macro_record":
                import asyncio
                import math
                import queue
                import tkinter as tk
                from pynput import mouse, keyboard

                loop = asyncio.get_running_loop()
                future = loop.create_future()

                def run_recorder_window():
                    if not IS_ADMIN:
                        # UIPI 限制：当前进程 IL 较低，无法捕获以管理员/反作弊提权运行的游戏窗口输入
                        print("【后端】[警告] 当前非管理员运行，部分以管理员启动的游戏窗口可能无法被录制"
                              "（UIPI 限制）。请以管理员身份启动 OpenTask 后再录制。")
                    result = {"action": "discard", "text": ""}
                    win = None
                    listeners = []
                    try:
                        # ================= 配色（与主界面一致） =================
                        C_BG = "#0b0e14"
                        C_CARD = "#1a2130"
                        C_LINE = "#2f3a4f"
                        C_TXT = "#e8ecf5"
                        C_DIM = "#9aa5bd"
                        C_MUTE = "#6b7893"
                        C_ACCENT = "#6366f1"
                        C_GREEN = "#22c55e"
                        C_RED = "#ef4444"
                        C_AMBER = "#f59e0b"
                        C_CYAN = "#22d3ee"
                        FONT = "Microsoft YaHei UI"
                        HINT = "录制全程本窗口置顶；开始 / 结束热键本身不会被录入动作。"

                        # ================= 状态（监听线程只改数据，不碰 UI）=================
                        recorded = []
                        phase = ["idle"]                     # idle / recording / finished
                        hotkeys = {"start": "ctrl+alt+f9", "stop": "ctrl+alt+f10"}
                        pending_capture = [None]
                        pressed_raw = set()
                        pressed_mod = set()
                        active_keys = {}
                        suppressed = {}
                        last_t = [time.time()]
                        rec_start = [0.0]
                        mouse_down = {"pos": None, "t": 0.0}
                        self_rect: Any = [None]      # 录制器窗口在屏幕上的位置，避免把自己的按钮点录进去
                        ui_queue = queue.Queue()

                        MOD_MAP = {
                            "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
                            "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "alt": "alt",
                            "shift": "shift", "shift_l": "shift", "shift_r": "shift",
                            "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd", "win": "cmd",
                        }
                        MOD_ORDER = ("ctrl", "alt", "shift", "cmd")

                        # ================= 绘图小工具 =================
                        def rr(canvas, x1, y1, x2, y2, r, **kw):
                            pts = []
                            for cx, cy, a0 in ((x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90),
                                               (x1 + r, y1 + r, 180), (x2 - r, y1 + r, 270)):
                                for a in range(a0, a0 + 91, 15):
                                    rad = math.radians(a)
                                    pts.extend([cx + r * math.cos(rad), cy + r * math.sin(rad)])
                            return canvas.create_polygon(pts, smooth=True, **kw)

                        class RoundButton(tk.Canvas):
                            def __init__(self, parent, text, command, w=110, h=34, bg=C_CARD,
                                         fg=C_TXT, hover="#26304a", radius=9,
                                         font=(FONT, 10, "bold")):
                                super().__init__(parent, width=w, height=h, bg=parent.cget("bg"),
                                                 highlightthickness=0, bd=0, cursor="hand2")
                                self._cmd = command
                                self._bg, self._fg, self._hover = bg, fg, hover
                                # 注意：不能叫 self._w，tkinter 用它保存控件路径名
                                self._btn_w, self._btn_h, self._r = w, h, radius
                                self._font, self._text = font, text
                                self._enabled, self._over = True, False
                                self._render()
                                self.bind("<Enter>", lambda e: self._enter())
                                self.bind("<Leave>", lambda e: self._leave())
                                self.bind("<Button-1>", lambda e: self._press())
                                self.bind("<ButtonRelease-1>", lambda e: self._release())

                            def _render(self):
                                self.delete("all")
                                fill = self._bg if self._enabled else "#1e2637"
                                if self._over and self._enabled:
                                    fill = self._hover
                                rr(self, 1, 1, self._btn_w - 1, self._btn_h - 1, self._r,
                                   fill=fill, outline="")
                                self.create_text(self._btn_w / 2, self._btn_h / 2 + 1, text=self._text,
                                                 fill=self._fg if self._enabled else "#54607a",
                                                 font=self._font)

                            def set_text(self, text):
                                if text != self._text:
                                    self._text = text
                                    self._render()

                            def set_style(self, bg, hover, fg=None):
                                if (bg, hover) == (self._bg, self._hover) and (fg is None or fg == self._fg):
                                    return
                                self._bg, self._hover = bg, hover
                                if fg:
                                    self._fg = fg
                                self._render()

                            def set_enabled(self, enabled):
                                enabled = bool(enabled)
                                if enabled != self._enabled:
                                    self._enabled = enabled
                                    self.configure(cursor="hand2" if enabled else "arrow")
                                    self._render()

                            def _enter(self):
                                self._over = True
                                self._render()

                            def _leave(self):
                                self._over = False
                                self._render()

                            def _press(self):
                                if self._enabled:
                                    self.move("all", 0, 1)

                            def _release(self):
                                if not self._enabled:
                                    return
                                self._render()
                                try:
                                    self._cmd()
                                except Exception as exc:
                                    print("【录制器】按钮回调异常:", exc)

                        # ================= 键名与热键 =================
                        def get_key_name(key):
                            try:
                                if hasattr(key, 'vk') and key.vk is not None:
                                    vk = key.vk
                                    vk_map = {
                                        65: 'a', 66: 'b', 67: 'c', 68: 'd', 69: 'e', 70: 'f', 71: 'g',
                                        72: 'h', 73: 'i', 74: 'j', 75: 'k', 76: 'l', 77: 'm', 78: 'n',
                                        79: 'o', 80: 'p', 81: 'q', 82: 'r', 83: 's', 84: 't',
                                        85: 'u', 86: 'v', 87: 'w', 88: 'x', 89: 'y', 90: 'z',
                                        48: '0', 49: '1', 50: '2', 51: '3', 52: '4',
                                        53: '5', 54: '6', 55: '7', 56: '8', 57: '9',
                                        32: 'space', 13: 'enter', 27: 'esc', 9: 'tab',
                                        8: 'backspace', 17: 'ctrl', 18: 'alt'
                                    }
                                    if vk in vk_map:
                                        return vk_map[vk]
                                if hasattr(key, 'char') and key.char:
                                    return key.char.lower()
                                if hasattr(key, 'name') and key.name:
                                    return key.name.lower()
                                return str(key).replace("Key.", "").lower()
                            except Exception:
                                return str(key).lower()

                        def norm_key(name):
                            return MOD_MAP.get(name, name)

                        def combo_of(name):
                            return frozenset(pressed_mod | {norm_key(name)})

                        def hk_combo(text):
                            parts = [p.strip().lower() for p in str(text or "").split("+") if p.strip()]
                            if not parts:
                                return None
                            mods, main = [], []
                            for p in parts:
                                if p in MOD_ORDER:
                                    mods.append(p)
                                else:
                                    main.append(p)
                            if not main:
                                return None
                            return frozenset(mods + [main[-1]])

                        def combo_text(combo):
                            mods = [m for m in MOD_ORDER if m in combo]
                            main = [k for k in combo if k not in MOD_ORDER]
                            label = main[0] if main else ""
                            if label.startswith("f") and label[1:].isdigit():
                                label = label.upper()
                            return "+".join([m.upper() for m in mods] + [label])

                        # ================= 录制数据操作 =================
                        def record_delay(now=None):
                            now = now if now is not None else time.time()
                            diff = int((now - last_t[0]) * 1000)
                            if diff > 50:
                                recorded.append("Delay %dms" % diff)
                            last_t[0] = now

                        def consume_chord(main_name):
                            """热键触发后：抑制该组合的抬起事件，并抹掉已写进缓冲的热键按键。"""
                            raw = set(pressed_raw) | {main_name}
                            names = set()
                            for r in raw:
                                names.add(norm_key(r))
                                suppressed[norm_key(r)] = True
                                active_keys[r] = True
                            while recorded:
                                tail = recorded[-1]
                                if tail.startswith("Delay "):
                                    recorded.pop()
                                    continue
                                if tail.startswith("KeyDown "):
                                    if norm_key(tail.split(" ", 1)[1].strip().lower()) in names:
                                        recorded.pop()
                                        continue
                                break
                            last_t[0] = time.time()

                        def reset_capture_state():
                            pressed_raw.clear()
                            pressed_mod.clear()
                            active_keys.clear()
                            suppressed.clear()
                            mouse_down["pos"] = None

                        # ================= 鼠标 / 键盘回调（纯数据） =================
                        def is_self_click(x, y):
                            rect = self_rect[0]
                            if not rect:
                                return False
                            return (rect[0] <= x <= rect[0] + rect[2]
                                    and rect[1] <= y <= rect[1] + rect[3])

                        def on_click(x, y, button, pressed):
                            if phase[0] != "recording":
                                return
                            if is_self_click(x, y):
                                mouse_down["pos"] = None
                                return
                            btn = button.name.capitalize()
                            if pressed:
                                mouse_down["pos"] = (x, y)
                                mouse_down["t"] = time.time()
                                return
                            record_delay()
                            dur = int((time.time() - mouse_down["t"]) * 1000)
                            sx, sy = mouse_down["pos"] or (x, y)
                            if abs(sx - x) < 3 and abs(sy - y) < 3:
                                recorded.append("Click %s %d,%d %dms" % (btn, x, y, dur))
                            else:
                                recorded.append("Drag %s S%d,%d E%d,%d %dms" % (btn, sx, sy, x, y, dur))
                            last_t[0] = time.time()

                        def on_scroll(x, y, dx, dy):
                            if phase[0] != "recording" or is_self_click(x, y):
                                return
                            record_delay()
                            recorded.append("Scroll %s,%s %d,%d" % (dx, dy, x, y))
                            last_t[0] = time.time()

                        def on_press(key):
                            name = get_key_name(key)
                            if not name:
                                return
                            mod = MOD_MAP.get(name)
                            if mod:
                                pressed_raw.add(name)
                                pressed_mod.add(mod)

                            # ① 设置热键时的捕获模式
                            if pending_capture[0]:
                                if name == "esc":
                                    ui_queue.put(lambda: finish_capture(None))
                                    return
                                if mod:
                                    return
                                ui_queue.put(lambda c=combo_of(name): finish_capture(c))
                                return

                            # ② 过滤系统连发
                            if active_keys.get(name):
                                return
                            active_keys[name] = True

                            # ③ 开始 / 结束热键
                            if not mod:
                                combo = combo_of(name)
                                if phase[0] != "recording" and combo == hk_combo(hotkeys["start"]):
                                    consume_chord(name)
                                    _start_recording()
                                    return
                                if phase[0] == "recording" and combo == hk_combo(hotkeys["stop"]):
                                    consume_chord(name)
                                    phase[0] = "finished"
                                    return

                            # ④ 正常录制
                            if phase[0] != "recording":
                                return
                            if suppressed.get(norm_key(name)):
                                return
                            record_delay()
                            recorded.append("KeyDown %s" % name)
                            last_t[0] = time.time()

                        def on_release(key):
                            name = get_key_name(key)
                            if not name:
                                return
                            mod = MOD_MAP.get(name)
                            if mod:
                                pressed_raw.discard(name)
                                pressed_mod.discard(mod)
                            was_active = active_keys.pop(name, None)
                            if suppressed.pop(norm_key(name), None):
                                return
                            if phase[0] != "recording" or not was_active:
                                return
                            record_delay()
                            recorded.append("KeyUp %s" % name)
                            last_t[0] = time.time()

                        # ================= 界面 =================
                        win = tk.Toplevel(_tk_service.root())
                        win.title("OpenTask · 动作宏录制器")
                        win.configure(bg=C_BG)
                        win.resizable(False, False)
                        win.attributes("-topmost", True)
                        try:
                            if os.path.exists(ICON_ICO):
                                win.iconbitmap(ICON_ICO)
                        except Exception:
                            pass

                        head = tk.Frame(win, bg=C_BG)
                        head.pack(fill="x", padx=20, pady=(18, 10))
                        logo = tk.Canvas(head, width=36, height=36, bg=C_BG, highlightthickness=0)
                        logo.pack(side="left")
                        try:
                            _logo_img = tk.PhotoImage(file=ICON_PNG).subsample(14, 14)
                            logo.create_image(18, 18, image=_logo_img)
                            setattr(logo, "image", _logo_img)
                        except Exception:
                            logo.create_rectangle(2, 2, 34, 34, fill=C_ACCENT, outline="")
                            logo.create_text(18, 18, text="◈", fill="white", font=(FONT, 14, "bold"))
                        tt = tk.Frame(head, bg=C_BG)
                        tt.pack(side="left", padx=(11, 0))
                        tk.Label(tt, text="动作宏录制器", bg=C_BG, fg=C_TXT,
                                 font=(FONT, 13, "bold")).pack(anchor="w")
                        tk.Label(tt, text="OpenTask · 全局键鼠动作捕获", bg=C_BG, fg=C_MUTE,
                                 font=(FONT, 8)).pack(anchor="w")

                        card = tk.Frame(win, bg=C_CARD, highlightbackground=C_LINE, highlightthickness=1)
                        card.pack(fill="x", padx=20)
                        status_row = tk.Frame(card, bg=C_CARD)
                        status_row.pack(fill="x", padx=15, pady=13)
                        dot = tk.Canvas(status_row, width=12, height=12, bg=C_CARD, highlightthickness=0)
                        dot.pack(side="left")
                        dot_id = dot.create_oval(1, 1, 11, 11, fill=C_AMBER, outline="")
                        status_lbl = tk.Label(status_row, text="待开始", bg=C_CARD, fg=C_TXT,
                                              font=(FONT, 11, "bold"))
                        status_lbl.pack(side="left", padx=(10, 0))
                        count_lbl = tk.Label(status_row, text="0 行", bg=C_CARD, fg=C_DIM,
                                             font=(FONT, 10))
                        count_lbl.pack(side="right")

                        hkbox = tk.Frame(win, bg=C_BG)
                        hkbox.pack(fill="x", padx=20, pady=(14, 0))

                        def hk_row(parent, caption, which):
                            line = tk.Frame(parent, bg=C_BG)
                            line.pack(fill="x", pady=4)
                            tk.Label(line, text=caption, bg=C_BG, fg=C_DIM, font=(FONT, 9),
                                     width=9, anchor="w").pack(side="left")
                            chip = tk.Label(line, text="", bg="#1d2433", fg=C_CYAN,
                                            font=("Consolas", 9, "bold"), padx=10, pady=4)
                            chip.pack(side="left", padx=(2, 0))
                            RoundButton(line, "重设", lambda w=which: begin_capture(w), w=60, h=26,
                                        bg="#232b3d", fg=C_DIM, hover="#2d3750", radius=7,
                                        font=(FONT, 8)).pack(side="right")
                            return chip

                        start_chip = hk_row(hkbox, "开始录制", "start")
                        stop_chip = hk_row(hkbox, "结束录制", "stop")

                        hk_hint = tk.Label(win, text=HINT, bg=C_BG, fg=C_MUTE, font=(FONT, 8),
                                           wraplength=440, justify="left")
                        hk_hint.pack(fill="x", padx=20, pady=(10, 0))

                        btns = tk.Frame(win, bg=C_BG)
                        btns.pack(fill="x", padx=20, pady=(16, 0))
                        action_btn = RoundButton(btns, "开始录制", lambda: on_action(), w=152, h=40,
                                                 bg=C_GREEN, fg="white", hover="#25d065", radius=11,
                                                 font=(FONT, 10, "bold"))
                        action_btn.pack(side="left")
                        discard_btn = RoundButton(btns, "抛弃", lambda: on_discard(), w=92, h=40,
                                                  bg="#2a1a1f", fg="#f87171", hover="#3a2028",
                                                  radius=11, font=(FONT, 10, "bold"))
                        discard_btn.pack(side="right")
                        confirm_btn = RoundButton(btns, "写入节点", lambda: on_confirm(), w=126, h=40,
                                                  bg=C_ACCENT, fg="white", hover="#7c7ff2",
                                                  radius=11, font=(FONT, 10, "bold"))
                        confirm_btn.pack(side="right", padx=(0, 8))

                        tk.Label(win, text="「写入节点」会把动作追加到当前动作宏节点的脚本末尾。",
                                 bg=C_BG, fg=C_MUTE, font=(FONT, 8)).pack(pady=(14, 0))

                        # ================= 状态刷新（仅 Tk 主线程） =================
                        def update_hk_labels():
                            start_chip.config(text=hotkeys["start"].upper())
                            stop_chip.config(text=hotkeys["stop"].upper())

                        def reset_hk_hint():
                            if not win.winfo_exists():
                                return
                            hk_hint.config(text=HINT, fg=C_MUTE)

                        def refresh():
                            ph = phase[0]
                            count_lbl.config(text="%d 行" % len(recorded))
                            if ph == "recording":
                                elapsed = int(time.time() - rec_start[0])
                                dot.itemconfigure(dot_id, fill=C_RED)
                                status_lbl.config(text="录制中  %02d:%02d" % (elapsed // 60, elapsed % 60))
                                action_btn.set_text("结束录制")
                                action_btn.set_style(C_AMBER, "#fbbf24")
                                confirm_btn.set_enabled(True)
                            elif ph == "finished":
                                dot.itemconfigure(dot_id, fill=C_GREEN)
                                status_lbl.config(text="已结束 · 共 %d 行" % len(recorded))
                                action_btn.set_text("重新录制")
                                action_btn.set_style(C_ACCENT, "#7c7ff2")
                                confirm_btn.set_enabled(bool(recorded))
                            else:
                                dot.itemconfigure(dot_id, fill=C_AMBER)
                                status_lbl.config(text="待开始 · 按热键或点按钮")
                                action_btn.set_text("开始录制")
                                action_btn.set_style(C_GREEN, "#25d065")
                                confirm_btn.set_enabled(False)

                        def tick():
                            if not win.winfo_exists():
                                return
                            try:
                                while True:
                                    job = ui_queue.get_nowait()
                                    try:
                                        job()
                                    except Exception as exc:
                                        print("【录制器】UI 任务异常:", exc)
                            except queue.Empty:
                                pass
                            refresh()
                            # 实时维护录制器窗口的屏幕矩形，用于忽略"点击自身按钮"（如结束录制/抛弃/写入节点）
                            try:
                                self_rect[0] = (win.winfo_rootx(), win.winfo_rooty(),
                                                win.winfo_width(), win.winfo_height())
                            except Exception:
                                pass
                            win.after(120, tick)

                        # ================= 控制逻辑 =================
                        def _start_recording():
                            recorded.clear()
                            reset_capture_state()
                            last_t[0] = time.time()
                            rec_start[0] = time.time()
                            phase[0] = "recording"

                        def begin_recording(delay_ms=0):
                            if phase[0] == "recording":
                                return
                            if delay_ms > 0:
                                win.after(delay_ms, _start_recording)
                            else:
                                _start_recording()

                        def finish_recording():
                            if phase[0] == "recording":
                                phase[0] = "finished"

                        def on_action():
                            if phase[0] == "recording":
                                finish_recording()
                            else:
                                begin_recording(350)

                        def stop_listeners():
                            for lis in listeners:
                                try:
                                    lis.stop()
                                except Exception:
                                    pass

                        def finish_recorder(action, text):
                            # 统一收尾：停止监听、写入结果、销毁窗口并唤醒等待协程
                            if future.done():
                                return
                            stop_listeners()
                            result["action"] = action
                            result["text"] = text
                            try:
                                win.destroy()
                            except Exception:
                                pass
                            loop.call_soon_threadsafe(future.set_result, result)

                        def on_confirm():
                            if phase[0] == "recording":
                                phase[0] = "finished"
                            finish_recorder("confirm", "\n".join(recorded))

                        def on_discard():
                            finish_recorder("discard", "")

                        def begin_capture(which):
                            if phase[0] == "recording":
                                hk_hint.config(text="录制中无法修改热键，请先结束录制", fg=C_RED)
                                win.after(1600, reset_hk_hint)
                                return
                            pending_capture[0] = which
                            pressed_raw.clear()
                            pressed_mod.clear()
                            hk_hint.config(text="请按下新的组合键（Esc 取消）…", fg=C_AMBER)

                        def finish_capture(combo):
                            which = pending_capture[0]
                            pending_capture[0] = None
                            if which is None:
                                return
                            if combo is None:
                                hk_hint.config(text="已取消设置", fg=C_MUTE)
                                win.after(1200, reset_hk_hint)
                                return
                            other = "stop" if which == "start" else "start"
                            if combo == hk_combo(hotkeys[other]):
                                hk_hint.config(text="⚠ 与「%s」热键冲突，请换一个组合" %
                                               ("结束录制" if other == "stop" else "开始录制"), fg=C_RED)
                                win.after(1800, reset_hk_hint)
                                return
                            hotkeys[which] = combo_text(combo).lower()
                            update_hk_labels()
                            hk_hint.config(text="已更新为 %s" % combo_text(combo), fg=C_GREEN)
                            win.after(1400, reset_hk_hint)

                        # ================= 启动监听 =================
                        listeners.append(mouse.Listener(on_click=on_click, on_scroll=on_scroll))
                        listeners.append(keyboard.Listener(on_press=on_press, on_release=on_release))
                        for lis in listeners:
                            lis.start()

                        win.protocol("WM_DELETE_WINDOW", on_discard)
                        update_hk_labels()
                        refresh()
                        win.update_idletasks()
                        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                        win.geometry("488x452+%d+%d" % ((sw - 488) // 2, max(60, (sh - 452) // 3)))
                        win.after(150, tick)
                        # 不再调用 win.mainloop()：由 TkService 常驻主循环接管事件分发；
                        # 窗口关闭时由 finish_recorder() 通过 future 唤醒等待协程。
                    except Exception as exc:
                        print("【录制器】窗口异常:", exc)
                        import traceback
                        traceback.print_exc()
                        stop_listeners()
                        if not future.done():
                            loop.call_soon_threadsafe(future.set_result, result)

                _tk_service.submit(run_recorder_window)

                res = await future
                await websocket.send_text(json.dumps({
                    "status": "macro_recorded",
                    "action": res["action"],
                    "text": res["text"]
                }))

            elif payload.get("action") == "choose_save_path":
                import asyncio
                import tkinter.filedialog as filedialog
                loop = asyncio.get_running_loop()

                def do_save():
                    return filedialog.asksaveasfilename(
                        parent=_tk_service.root(),
                        defaultextension=".otk",
                        initialfile=payload.get("defaultName", "Task.otk"),
                        filetypes=[("OpenTask Files", "*.otk"), ("JSON Files", "*.json"), ("All Files", "*.*")],
                        title="另存为"
                    )

                # 统一交给常驻 Tk 服务线程执行，避免跨线程调用报错
                file_path = await loop.run_in_executor(None, _tk_service.call_sync, do_save)
                if file_path:
                    await websocket.send_text(json.dumps({"status": "save_path_chosen", "path": file_path}))
                else:
                    await websocket.send_text(json.dumps({"status": "save_path_cancelled"}))

            elif payload.get("action") == "choose_load_path":
                # 由后端弹出原生“打开”对话框：浏览器拿不到本地绝对路径，
                # 交给后端选择即可同时得到绝对路径，保存时才能直接覆盖原文件。
                import asyncio
                import tkinter.filedialog as filedialog
                loop = asyncio.get_running_loop()

                def do_open():
                    return filedialog.askopenfilename(
                        parent=_tk_service.root(),
                        filetypes=[("OpenTask Files", "*.otk"), ("JSON Files", "*.json"), ("All Files", "*.*")],
                        title="打开工程"
                    )

                file_path = await loop.run_in_executor(None, _tk_service.call_sync, do_open)
                if file_path:
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        recent = _add_recent(file_path)
                        await websocket.send_text(json.dumps({
                            "status": "loaded",
                            "path": os.path.abspath(file_path),
                            "data": content,
                            "recent": recent,
                        }, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send_text(json.dumps({"status": "log", "msg": f"❌ 打开失败: {e}"}))

            elif payload.get("action") == "load_recent":
                file_path = payload.get("filePath")
                if file_path:
                    try:
                        if not os.path.exists(file_path):
                            raise FileNotFoundError("文件不存在或已被移动")
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        recent = _add_recent(file_path)
                        await websocket.send_text(json.dumps({
                            "status": "loaded",
                            "path": os.path.abspath(file_path),
                            "data": content,
                            "recent": recent,
                        }, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send_text(json.dumps({"status": "log", "msg": f"❌ 打开最近项目失败: {e}"}))

            elif payload.get("action") == "get_recent":
                await websocket.send_text(json.dumps({
                    "status": "recent_list",
                    "items": _load_recent(),
                }, ensure_ascii=False))

            elif payload.get("action") == "save_graph":
                file_path = payload.get("filePath")
                data_str = payload.get("data")
                if not file_path or not data_str:
                    await websocket.send_text(json.dumps({"status": "save_failed", "msg": "缺少文件路径或内容"}, ensure_ascii=False))
                else:
                    try:
                        # 【修复】相对路径一律落到程序目录，避免写进 C:\Windows\System32 这类进程 CWD
                        abs_path = os.path.abspath(file_path)
                        dir_name = os.path.dirname(abs_path)
                        if dir_name: os.makedirs(dir_name, exist_ok=True)
                        with open(abs_path, "w", encoding="utf-8") as f:
                            f.write(data_str)
                        # 【修复】直接使用 await，避免同线程使用 threadsafe 导致协程死锁
                        await websocket.send_text(json.dumps({"status": "log", "msg": f"✅ 成功保存至: {abs_path}"}))
                        # 记录到“最近使用”列表
                        recent = _add_recent(abs_path)
                        # 通知前端：保存完成，可清除“未保存”标记
                        await websocket.send_text(json.dumps({
                            "status": "saved",
                            "path": abs_path,
                            "recent": recent,
                        }, ensure_ascii=False))
                    except Exception as e:
                        # 【修复】原来是 except: pass，保存失败时用户毫无反馈
                        await websocket.send_text(json.dumps({"status": "save_failed", "msg": str(e)}, ensure_ascii=False))
                        await websocket.send_text(json.dumps({"status": "log", "msg": f"❌ 保存失败: {e}"}))



    except Exception as e:
        print("WebSocket 断开连接:", e)
        running = False
    finally:
        # 断开后停止向已失效的连接转发日志，避免异常
        _LOG_WS = None
        _LOG_LOOP = None


def find_free_port(start_port=8000, max_tries=20):
    """从 start_port 起找一个可用端口，避免端口被占用导致启动失败。"""
    import socket
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


# ============================================================
#  系统托盘 / 窗口图标 / 开机自启 / 全局停止
# ============================================================
_tray_icon = None
_tray_thread = None   # 【修复】保存运行 icon.run() 的线程，退出时 join 等待它执行 NIM_DELETE 清理托盘图标
_main_window = None
_quitting = False     # 退出流程已启动，避免 destroy 时再次触发 closing 弹窗


def tray_notify(title, message):
    """通过托盘气泡发系统通知；失败不影响主流程。"""
    icon = _tray_icon
    if icon is None:
        return
    try:
        icon.notify(message, title)
    except Exception as exc:
        print("【系统】托盘通知发送失败:", exc)


def stop_current_task():
    """托盘「停止当前任务」：让执行线程失效并松开所有按键。返回是否真的在运行。"""
    global running, run_id
    was_running = running
    running = False
    run_id += 1
    try:
        import pydirectinput as _pdi
        release_all_inputs(_pdi)
    except Exception:
        pass
    if was_running:
        print("【后端】🛑 托盘请求停止当前任务")
    return was_running


def _autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def is_autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except OSError:
                pass


def _set_app_user_model_id():
    """让任务栏使用本程序自己的图标，而不是 python.exe 的图标。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenTask.Automation.Desktop")
    except Exception as exc:
        print("【系统】[警告] 设置 AppUserModelID 失败:", exc)


def _apply_window_icon(title, retries=25):
    """pywebview 的 Windows 后端不支持 icon 参数，这里用 WM_SETICON 直接设置窗口/任务栏图标。"""
    if not os.path.exists(ICON_ICO):
        print("【系统】[警告] 缺少图标文件:", ICON_ICO)
        return False
    import ctypes
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    try:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        user32.FindWindowW.restype = ctypes.c_void_p
    except Exception as exc:
        print("【系统】[警告] 初始化图标 API 失败:", exc)
        return False

    for _ in range(retries):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            for index, size in ((ICON_BIG, 32), (ICON_SMALL, 16)):
                hicon = user32.LoadImageW(None, ICON_ICO, IMAGE_ICON, size, size, LR_LOADFROMFILE)
                if hicon:
                    user32.SendMessageW(hwnd, WM_SETICON, index, hicon)
            print("【系统】已应用窗口与任务栏图标。")
            return True
        time.sleep(0.2)
    print("【系统】[警告] 未找到主窗口句柄，图标未能应用。")
    return False


def _hide_console_window():
    """隐藏当前进程的控制台窗口（仅在 Windows 下有效）。

    只有当前进程独占控制台时才隐藏，避免把用户共享的终端窗口也一起藏掉。
    """
    if sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        # 检查挂在该控制台上的进程数量：=1 表示是本程序独占的控制台
        pids = (ctypes.c_uint * 8)()
        count = k32.GetConsoleProcessList(pids, 8)
        if count <= 1:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception as exc:
        print("【系统】[警告] 隐藏控制台窗口失败:", exc)


def create_tray(window):
    """创建系统托盘图标与右键菜单。"""
    global _tray_icon, _tray_thread
    try:
        import pystray
        from PIL import Image
    except Exception as exc:
        print("【系统】[警告] 未安装 pystray / Pillow，托盘功能不可用:", exc)
        return None

    try:
        image = Image.open(ICON_PNG)
    except Exception as exc:
        print("【系统】[警告] 托盘图标加载失败:", exc)
        return None

    def _show_window(icon=None, item=None):
        try:
            window.show()
            window.restore()
        except Exception as exc:
            print("【系统】显示主窗口失败:", exc)

    def _stop_task(icon=None, item=None):
        stopped = stop_current_task()
        tray_notify("已停止当前任务" if stopped else "当前没有正在运行的任务", APP_NAME)

    def _toggle_autostart(icon=None, item=None):
        enable = not is_autostart_enabled()
        try:
            set_autostart(enable)
            tray_notify("开机自启已%s" % ("开启" if enable else "关闭"), APP_NAME)
        except Exception as exc:
            print("【系统】设置开机自启失败:", exc)
            tray_notify("设置失败：%s" % exc, APP_NAME)

    def _open_dir(icon=None, item=None):
        try:
            os.startfile(APP_DIR)
        except Exception as exc:
            print("【系统】打开程序目录失败:", exc)

    def _about(icon=None, item=None):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "%s 自动化任务节点编辑器\n版本 %s\n\n本地引擎：http://127.0.0.1:%s\n"
                "关闭主窗口不会退出软件，退出请用托盘菜单「退出 %s」。"
                % (APP_NAME, APP_VERSION, APP_PORT, APP_NAME),
                "关于 %s" % APP_NAME, 0x00000040)
        except Exception as exc:
            print("【系统】关于对话框打开失败:", exc)

    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口（双击）", _show_window, default=True),
        # pystray 运行期支持 enabled 传回调，但类型存根标注为 bool
        pystray.MenuItem("停止当前任务", _stop_task, enabled=lambda item: running),  # pyright: ignore[reportArgumentType]
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", _toggle_autostart, checked=lambda item: is_autostart_enabled()),
        pystray.MenuItem("打开程序目录", _open_dir),
        pystray.MenuItem("关于 %s" % APP_NAME, _about),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 %s" % APP_NAME, lambda icon, item: quit_app()),
    )

    icon = pystray.Icon(APP_NAME, image, "%s 自动化任务节点编辑器" % APP_NAME, menu)
    _tray_icon = icon
    _tray_thread = threading.Thread(target=icon.run, daemon=True)
    _tray_thread.start()
    print("【系统】已驻留系统托盘（右键查看菜单）。")
    return icon


def _force_remove_tray_icon(icon):
    """【兜底】直接发 NIM_DELETE 删除托盘图标，绕开 pystray 的消息循环。

    为什么要这一步：pystray 的 NIM_DELETE 写在托盘线程 _mainloop 的 finally 里，
    而从托盘菜单点“退出”时，代码本身就运行在托盘线程内，随后 os._exit(0)
    会立刻 TerminateProcess 整个进程，托盘线程根本没机会跑到那个 finally。
    因此必须在 os._exit 之前，主动、同步地把图标删掉。

    icon._hide() 内部即用注册时的同一个 (hWnd, id(self)) 调 Shell_NotifyIcon(NIM_DELETE)，
    参数与注册时一致，能准确删掉本图标；重复删除是幂等安全的。
    """
    if icon is None:
        return
    try:
        icon._hide()
    except Exception as exc:
        print("【系统】注销托盘图标失败:", exc)


def quit_app(skip_unsaved_check=False):
    """统一退出：未保存确认 → 停任务 → 注销托盘图标(同步) → 关窗口 → 释放单实例锁 → 结束进程。"""
    global _tray_icon, _tray_thread, _SINGLE_INSTANCE_HANDLE, _quitting

    if not skip_unsaved_check and not _confirm_before_exit_like_action("exit"):
        print("【系统】已取消退出。")
        return False

    _quitting = True
    print("【系统】正在退出 %s..." % APP_NAME)
    try:
        stop_current_task()
    except Exception:
        pass

    icon = _tray_icon
    tray_thread = _tray_thread

    # 1) 通知托盘线程结束消息循环（stop() 只是异步投递 WM_STOP）
    try:
        if icon is not None:
            icon.stop()
    except Exception as exc:
        print("【系统】停止托盘图标失败:", exc)

    # 2) 若当前不是托盘线程，等它把消息循环跑完、自己清理一次
    #    （从托盘菜单退出时，代码就在托盘线程内，不能 join 自己，否则死锁）
    try:
        if (tray_thread is not None and tray_thread.is_alive()
                and tray_thread.ident != threading.current_thread().ident):
            tray_thread.join(timeout=3.0)
    except Exception as exc:
        print("【系统】等待托盘线程清理失败:", exc)

    # 3) 【关键兜底】无论上面是否成功，都主动同步发一次 NIM_DELETE。
    #    托盘菜单退出这条路径靠的就是这一步——因为 os._exit 会让线程的 finally 失效。
    _force_remove_tray_icon(icon)

    # 4) 给 explorer 一点时间处理 NIM_DELETE，再结束进程
    time.sleep(0.3)
    _tray_icon = None
    _tray_thread = None

    try:
        if _main_window is not None:
            _main_window.destroy()
    except Exception:
        pass

    # 5) 主动释放单实例互斥体句柄，避免任何时序下"进程还在"的误判。
    #    （os._exit 后系统也会回收，但这里显式释放更干净、更可控。）
    try:
        if _SINGLE_INSTANCE_HANDLE is not None and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_HANDLE)
        _SINGLE_INSTANCE_HANDLE = None
    except Exception:
        pass

    os._exit(0)


if __name__ == '__main__':
    import webview

    _set_app_user_model_id()

    PORT = find_free_port(8000)
    APP_PORT = PORT
    if PORT != 8000:
        print(f"【系统】[提示] 8000 端口已被占用，自动切换到 {PORT} 端口。")

    def start_server():
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")

    print("【系统】正在启动本地核心引擎...")
    threading.Thread(target=start_server, daemon=True).start()
    time.sleep(1)

    print("【系统】正在启动原生桌面 UI 窗口...")
    WINDOW_TITLE = 'OpenTask 自动化任务节点编辑器'

    class _WindowApi:
        """暴露给前端 JS 的小接口（pywebview js_api）。"""

        def set_title(self, title):
            """让前端可以把「工程名 + *」写进系统窗口标题栏。"""
            try:
                if _main_window is not None:
                    _main_window.title = title
                    return True
            except Exception as exc:
                print("设置窗口标题失败:", exc)
            return False

    _window_api = _WindowApi()
    _main_window: Any = webview.create_window(
        title=WINDOW_TITLE,
        url=f'http://127.0.0.1:{PORT}',
        width=1400,
        height=900,
        min_size=(1080, 700),
        resizable=True,
        js_api=_window_api
    )

    def _hide_to_tray():
        try:
            _main_window.hide()
        except Exception as exc:
            print("【系统】隐藏窗口失败:", exc)

    def _close_window_after_confirm():
        # 关闭窗口不等于退出：确认保存后仍只收起到托盘
        if not _confirm_before_exit_like_action("close"):
            print("【系统】已取消关闭窗口。")
            return
        _hide_to_tray()
        tray_notify("OpenTask 仍在后台运行", "已收起到系统托盘，右键托盘图标可退出")
        print("【系统】窗口已收起，软件继续在后台运行（托盘右键可退出）。")

    def on_closing():
        if _quitting:
            return True
        # 不能在此阻塞 GUI 线程，否则前端弹窗无法渲染；交给后台线程等待决策
        threading.Thread(target=_close_window_after_confirm, daemon=True).start()
        return False

    _main_window.events.closing += on_closing
    create_tray(_main_window)

    def _start_ui():
        # 软件窗口打开后立即隐藏控制台黑窗
        _hide_console_window()
        _apply_window_icon(WINDOW_TITLE)

    webview.start(_start_ui)
    quit_app()
