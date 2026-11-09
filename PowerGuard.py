# -*- coding: utf-8 -*-
"""PowerGuard —— 笔记本断电自动关机守护工具

功能概述
    监测笔记本电源状态（psutil.sensors_battery）。当电源由“交流供电(AC)”
    切换为“电池供电”后，先等待“触发延迟”秒，再开始“倒计时秒数”的倒计时，
    倒计时归零后执行关机；倒计时期间用户可随时取消。

安全设计（重要）
    1. 默认处于 DRY-RUN 模拟模式：只弹提示，绝不执行任何关机/重启命令。
    2. 环境变量 POWERGUARD_SAFE=1 时强制 DRY-RUN（用于开发自测）。
    3. 真实关机模式必须由用户在界面上手动确认后才会启用。

代码结构（自上而下，单一实现，无历史分支）
    1. 常量与默认配置
    2. 基础工具：目录 / 时间 / 日志 / 数值夹紧 / 配置读写
    3. 开机自启：注册表读写、启动命令构造、写后校验与诊断
    4. 关机决策：DRY-RUN 判定、关机命令构造（只构造不执行）
    5. 电源状态机：纯逻辑，不碰系统
    6. 界面与托盘：主窗口、设置窗口、倒计时窗口、托盘菜单
    7. 自测入口：--selftest（无界面）/ --uitest（界面链路）
    8. 单实例与进程入口：命名事件 IPC + 互斥体

变更记录
    1.3.0（代码收敛版）：删除已废弃的旧设置实现与兼容包装、调试探针残留、
        重复实现的队列 / 弹窗 / 状态文案逻辑；统一版本号与命名；
        功能与 1.2.0 完全一致。
    1.2.0：新增开机自启静默启动（--silent）。

作者：Marvis File Agent
版本：1.3.0
"""

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 常量与默认配置
# --------------------------------------------------------------------------
APP_NAME = "PowerGuard"
APP_TITLE = "PowerGuard 电源守护"
APP_VERSION = "1.3.0"

# 静默启动参数：开机自启时写入注册表的命令行会在程序路径后追加该参数，
# 程序识别到它后强制「只驻留托盘、不创建/显示任何窗口」，不受配置项影响。
SILENT_FLAG = "--silent"
# 环境变量兜底：POWERGUARD_SILENT=1 时等效于传了 --silent（便于自测与排障）
SILENT_ENV = "POWERGUARD_SILENT"

# 单实例通信：第二个实例通过这个命名事件通知已运行实例「弹出设置窗口」
IPC_EVENT_NAME = "PowerGuard_ShowSettings_Event"
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PowerGuard"
# 「设置 > 应用 > 启动」的审批记录：Windows 10/11 用它记住该项是否被用户禁用
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

DEFAULT_CONFIG = {
    "countdown_seconds": 5,   # 倒计时秒数
    "startup_delay": 3,       # 触发延迟：检测到断开电源后，等待多少秒才开始倒计时
    "dry_run": True,          # True = 模拟模式（不真正关机）
    "poll_interval": 1.0,     # 电源状态轮询间隔（秒）
    "start_minimized": True,  # 启动时是否只显示托盘图标
    "paused": False,          # 监控是否处于暂停状态
    "autostart": False,       # 用户希望的「开机自启」意愿（实际是否生效以注册表校验为准）
    "cancel_on_ac_restore": True,  # 倒计时期间恢复交流电时是否自动取消关机
    "ac_poll_interval": 0.2,  # 倒计时期间电源高频轮询间隔（秒）
}

MIN_SECONDS, MAX_SECONDS = 1, 3600
MIN_DELAY, MAX_DELAY = 0, 3600


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def app_dir() -> Path:
    """配置与日志目录：%APPDATA%\\PowerGuard"""
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = app_dir() / "config.json"
LOG_PATH = app_dir() / "powerguard.log"


def now_str() -> str:
    dt = datetime.now()
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def log_line(msg: str) -> None:
    """写入日志文件（超过 512KB 自动轮转），任何异常都静默忽略。"""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            bak = LOG_PATH.with_suffix(".log.1")
            if bak.exists():
                bak.unlink()
            LOG_PATH.rename(bak)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {msg}\n")
    except Exception:
        pass


def clamp(value, low, high, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in cfg:
                        cfg[k] = v
    except Exception as exc:
        log_line(f"读取配置失败，使用默认配置：{exc}")
    cfg["countdown_seconds"] = clamp(cfg.get("countdown_seconds"), MIN_SECONDS, MAX_SECONDS,
                                     DEFAULT_CONFIG["countdown_seconds"])
    cfg["startup_delay"] = clamp(cfg.get("startup_delay"), MIN_DELAY, MAX_DELAY,
                                 DEFAULT_CONFIG["startup_delay"])
    cfg["dry_run"] = bool(cfg.get("dry_run", True))
    cfg["paused"] = bool(cfg.get("paused", False))
    cfg["start_minimized"] = bool(cfg.get("start_minimized", True))
    cfg["autostart"] = bool(cfg.get("autostart", False))
    cfg["cancel_on_ac_restore"] = bool(cfg.get("cancel_on_ac_restore", True))
    try:
        cfg["poll_interval"] = max(0.3, min(10.0, float(cfg.get("poll_interval", 1.0))))
    except (TypeError, ValueError):
        cfg["poll_interval"] = 1.0
    try:
        cfg["ac_poll_interval"] = max(0.05, min(2.0, float(cfg.get("ac_poll_interval", 0.2))))
    except (TypeError, ValueError):
        cfg["ac_poll_interval"] = 0.2
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        return True
    except Exception as exc:
        log_line(f"保存配置失败：{exc}")
        return False


# --------------------------------------------------------------------------
# 开机自启（HKCU / HKLM 的 ...\Run）
# --------------------------------------------------------------------------
def _winreg():
    try:
        import winreg
        return winreg
    except Exception:
        return None


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _norm_target(cmd) -> str:
    """把启动命令规范成可比较的「可执行文件全路径」（去引号、小写、绝对化）。"""
    text = str(cmd or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        exe = text[1:end] if end > 0 else text.strip('"')
    else:
        exe = text.split(" ")[0]
    exe = exe.strip().strip('"')
    try:
        exe = os.path.expandvars(exe)
        if not os.path.isabs(exe):
            exe = os.path.abspath(exe)
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(exe))


def autostart_command() -> str:
    """写入注册表的启动命令（开机自启场景，固定追加静默参数）。

    打包成单文件 exe 后 sys.executable 就是 exe 本身，路径含空格时必须用双引号
    包裹，否则 Windows 会按空格切分导致启动失败。

    末尾固定追加 SILENT_FLAG：开机自启一定走静默启动（只驻留托盘），
    不读取、也不受配置文件里「显示主窗口」这一项的影响。
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" {SILENT_FLAG}'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe.resolve()}" "{Path(__file__).resolve()}" {SILENT_FLAG}'


def command_is_silent(cmd: str) -> bool:
    """判断一条启动命令行是否带静默参数（用于自测与诊断展示）。"""
    return SILENT_FLAG in (cmd or "")


def env_wants_silent() -> bool:
    """环境变量兜底：POWERGUARD_SILENT=1 时同样进入静默启动。"""
    return str(os.environ.get(SILENT_ENV, "")).strip() in ("1", "true", "True", "yes", "on")


def _hive_of(name):
    w = _winreg()
    if w is None:
        return None
    return w.HKEY_CURRENT_USER if name == "HKCU" else w.HKEY_LOCAL_MACHINE


def _open_key(name, key_path, write=False, create=False):
    w = _winreg()
    if w is None:
        return None
    access = w.KEY_SET_VALUE if write else w.KEY_READ
    if name == "HKLM":
        access |= getattr(w, "KEY_WOW64_64KEY", 0)
    hive = _hive_of(name)
    if create:
        return w.CreateKeyEx(hive, key_path, 0, access)
    return w.OpenKey(hive, key_path, 0, access)


def _read_run_value(name) -> str:
    """读取指定注册表分支下的自启命令实际内容，读不到返回空串。"""
    w = _winreg()
    if w is None:
        return ""
    key = None
    try:
        key = _open_key(name, RUN_KEY)
        if key is None:
            return ""
        value, _ = w.QueryValueEx(key, RUN_VALUE)
        return str(value or "")
    except Exception:
        return ""
    finally:
        if key is not None:
            try:
                w.CloseKey(key)
            except Exception:
                pass


def _read_startup_approved(name) -> str:
    """「设置 > 应用 > 启动」中的审批记录（十六进制）。首字节为奇数表示被禁用过。"""
    w = _winreg()
    if w is None:
        return ""
    key = None
    try:
        key = _open_key(name, STARTUP_APPROVED_KEY)
        if key is None:
            return ""
        value, _ = w.QueryValueEx(key, RUN_VALUE)
        return bytes(value).hex()
    except Exception:
        return ""
    finally:
        if key is not None:
            try:
                w.CloseKey(key)
            except Exception:
                pass


def _clear_startup_approved(name) -> bool:
    """清除「启动」页里的禁用标记，让 Windows 重新把它当作启用项。"""
    w = _winreg()
    if w is None:
        return False
    key = None
    try:
        key = _open_key(name, STARTUP_APPROVED_KEY, write=True)
        if key is None:
            return False
        w.DeleteValue(key, RUN_VALUE)
        return True
    except Exception:
        return False
    finally:
        if key is not None:
            try:
                w.CloseKey(key)
            except Exception:
                pass


def autostart_diagnostics() -> dict:
    """返回自启项的真实状态（含注册表里实际写入的命令），供界面展示与 --diag-autostart。"""
    expected = autostart_command()
    info = {
        "version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "sys_executable": sys.executable,
        "is_admin": _is_admin(),
        "user": os.environ.get("USERNAME", ""),
        "run_key": "HKCU\\" + RUN_KEY,
        "value_name": RUN_VALUE,
        "expected_command": expected,
        "expected_target": _norm_target(expected),
        "hkcu_value": "",
        "hklm_value": "",
        "hkcu_ok": False,
        "hklm_ok": False,
        "hkcu_startup_approved": "",
        "hklm_startup_approved": "",
        "enabled": False,
        "issues": [],
    }
    if _winreg() is None:
        info["issues"].append("当前平台不支持 winreg")
        return info
    info["hkcu_value"] = _read_run_value("HKCU")
    info["hklm_value"] = _read_run_value("HKLM")
    info["hkcu_startup_approved"] = _read_startup_approved("HKCU")
    info["hklm_startup_approved"] = _read_startup_approved("HKLM")
    exp = info["expected_target"]
    info["hkcu_ok"] = bool(info["hkcu_value"]) and _norm_target(info["hkcu_value"]) == exp
    info["hklm_ok"] = bool(info["hklm_value"]) and _norm_target(info["hklm_value"]) == exp
    info["enabled"] = bool(info["hkcu_ok"] or info["hklm_ok"])
    if info["hkcu_value"] and not info["hkcu_ok"]:
        info["issues"].append(
            f"HKCU 自启项指向 {info['hkcu_value']}，与当前程序 {exp} 不一致"
            "（多为 exe 被移动 / 重新打包后残留旧路径）")
    if not info["enabled"]:
        info["issues"].append("注册表中不存在指向当前程序的开机自启项")
    for side, label in (("hkcu_startup_approved", "HKCU"), ("hklm_startup_approved", "HKLM")):
        raw = info[side]
        if not raw:
            continue
        try:
            if int(raw[0:2], 16) % 2 == 1:
                info["issues"].append(
                    f"{label} 的自启项在「设置 > 应用 > 启动」中被禁用（记录 {raw[:8]}），"
                    "Windows 开机会跳过它")
        except Exception:
            pass
    return info


def autostart_is_enabled() -> bool:
    """是否已存在指向当前程序的有效自启项（HKCU 或 HKLM 任一命中）。"""
    try:
        return bool(autostart_diagnostics()["enabled"])
    except Exception:
        return False


def autostart_ensure(desired: bool, machine_write=None) -> tuple:
    """按需写入 / 删除开机自启项，并清理「启动」页的旧禁用记录。

    返回 (是否成功, 说明文字, 诊断字典)。
    machine_write=None 时：仅当当前进程已提权，才额外写 HKLM 作为兜底
    （覆盖「用管理员身份运行 / UAC 提权后 HKCU 落在另一个账户」导致自启失效的情形）。
    """
    w = _winreg()
    if w is None:
        return False, "当前平台不支持注册表操作", autostart_diagnostics()

    if machine_write is None:
        machine_write = _is_admin()

    cmd = autostart_command()
    msgs = []
    if desired:
        targets = ["HKCU"] + (["HKLM"] if machine_write else [])
    else:
        targets = ["HKCU", "HKLM"]

    if desired:
        written = False
        for name in targets:
            key = None
            try:
                key = _open_key(name, RUN_KEY, write=True, create=True)
                w.SetValueEx(key, RUN_VALUE, 0, w.REG_SZ, cmd)
                written = True
                msgs.append(f"{name} 已写入自启项 {cmd}")
            except Exception as exc:
                msgs.append(f"{name} 写入失败：{exc}")
            finally:
                if key is not None:
                    try:
                        w.CloseKey(key)
                    except Exception:
                        pass
            if _clear_startup_approved(name):
                msgs.append(f"{name} 已清除「启动」页旧禁用记录")
        if not written:
            return False, "；".join(msgs) or "写入注册表失败", autostart_diagnostics()
    else:
        for name in targets:
            key = None
            try:
                key = _open_key(name, RUN_KEY, write=True)
                try:
                    w.DeleteValue(key, RUN_VALUE)
                    msgs.append(f"{name} 已删除自启项")
                except FileNotFoundError:
                    msgs.append(f"{name} 无自启项")
            except FileNotFoundError:
                msgs.append(f"{name} 无自启项")
            except Exception as exc:
                msgs.append(f"{name} 删除失败：{exc}")
            finally:
                if key is not None:
                    try:
                        w.CloseKey(key)
                    except Exception:
                        pass
            if _clear_startup_approved(name):
                msgs.append(f"{name} 已清除「启动」页禁用记录")

    diag = autostart_diagnostics()
    ok = diag["enabled"] if desired else (not diag["enabled"])
    return ok, "；".join(msgs) or "无变化", diag


# --------------------------------------------------------------------------
# 关机决策（不做任何真实执行，便于自测）
# --------------------------------------------------------------------------
def should_dry_run(cfg: dict, force_dry: bool = False) -> bool:
    """是否走模拟模式：配置要求 / 强制模拟 / 环境安全开关，任一成立即模拟。"""
    if force_dry:
        return True
    if os.environ.get("POWERGUARD_SAFE") == "1":
        return True
    return bool(cfg.get("dry_run", True))


def shutdown_command() -> list:
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = str(Path(sys_root) / "System32" / "shutdown.exe")
    if not Path(exe).exists():
        exe = "shutdown"
    return [exe, "/s", "/t", "0"]


# --------------------------------------------------------------------------
# 电源状态机（纯逻辑，便于自测）
# --------------------------------------------------------------------------
class PowerMonitor:
    """根据电源插入状态序列判断事件。

    update(plugged) 返回值：
        "lost"     交流电 -> 电池供电，需要触发关机流程
        "restored" 电池 -> 交流电，重新武装
        None       无事件
    """

    def __init__(self):
        self._prev = None
        self.armed = True
        self.paused = False

    def update(self, plugged: bool):
        event = None
        if self._prev is True and plugged is False:
            if self.armed and not self.paused:
                self.armed = False
                event = "lost"
        if plugged is True and not self.armed:
            self.armed = True
            if event is None:
                event = "restored"
        self._prev = bool(plugged)
        return event


# --------------------------------------------------------------------------
# 界面与托盘
# --------------------------------------------------------------------------
def make_icon_image(mode: str, size: int = 64):
    """用 PIL 现场绘制托盘图标：AC 绿色 / 电池橙色 / 暂停灰色。"""
    from PIL import Image, ImageDraw

    color = {"ac": (45, 170, 90, 255),
             "battery": (240, 150, 30, 255),
             "paused": (140, 140, 140, 255),
             "error": (200, 60, 60, 255)}.get(mode, (120, 120, 120, 255))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 电池外壳
    d.rounded_rectangle((6, 16, size - 14, size - 16), radius=7, outline=color, width=5)
    # 电池正极
    d.rounded_rectangle((size - 13, size // 2 - 7, size - 5, size // 2 + 7), radius=3, fill=color)
    # 电量柱
    if mode in ("ac", "battery"):
        d.rounded_rectangle((14, 24, 32, size - 24), radius=3, fill=color)
        d.rounded_rectangle((38, 24, 46, size - 24), radius=3, fill=color)
    else:  # 暂停：两道竖线
        d.rectangle((22, 26, 28, size - 26), fill=color)
        d.rectangle((34, 26, 40, size - 26), fill=color)
    return img


class PowerGuardApp:
    def __init__(self, gui: bool = True, silent: bool = False):
        self.cfg = load_config()
        self.monitor = PowerMonitor()
        self.monitor.paused = bool(self.cfg.get("paused", False))
        self.ui_queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # 运行期状态
        self.status = {"present": None, "plugged": None, "percent": None}
        self._last_status_error = ""   # 轮询异常去重，同名异常只记一次日志
        self.state = "idle"           # idle / delay / count
        self.remaining = 0
        self._after_id = None
        self._delay_id = None
        self.force_dry = False        # 测试触发时强制模拟
        self.icon = None              # pystray 托盘图标
        self.root = None              # Tk 根对象（主窗口）
        self._ipc_event = None        # 单实例「打开设置」命名事件
        self._ipc_thread = None       # 命名事件的监视线程
        self.count_win = None
        self.count_label = None
        self.log_box = None
        self.status_labels = {}
        self.settings_win = None
        self.settings_open_count = 0
        self.settings_vars = {}
        self.summary_label = None
        self.btn_pause = None
        self.dry_warn_label = None

        self.gui = gui
        self.demo = False

        # 静默启动（开机自启）：强制只驻留托盘，不创建/显示任何窗口。
        # 该标记只作用于本次进程，不写回配置文件，避免污染用户手动启动的行为。
        self.silent = bool(silent)
        if self.silent:
            self.cfg["start_minimized"] = True

    # ---------------- 日志 ----------------
    def log(self, msg: str):
        line = f"[{now_str()}] {msg}"
        log_line(msg)
        try:
            self.ui_queue.put(("log", line))
        except Exception:
            pass

    # ---------------- 生命周期 ----------------
    def start(self, auto_exit_after: float = 0):
        self.log(f"=== {APP_NAME} v{APP_VERSION} 启动 ===")
        self.log(f"配置文件：{CONFIG_PATH}")
        self.log(f"运行模式：{'DRY-RUN 模拟（不真正关机）' if should_dry_run(self.cfg) else '真实关机'}")
        if self.silent:
            self.log(f"启动方式：静默启动（{SILENT_FLAG}）——只初始化托盘与电源监控，"
                     "不创建主窗口、不弹出任何窗口（已忽略配置项「显示主窗口」）")

        self._autostart_boot_check()

        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self._start_ipc_watcher()
        self._start_tray()

        if self.gui:
            if self.silent:
                self._build_hidden_root()
            else:
                self._build_ui()
            self.root.after(100, self._drain_queue)
            self.root.after(1000, self._refresh_status_view)
            if self.demo:
                # 自测专用：无论静默还是带界面，都走一遍「触发 -> 倒计时 -> 结束提示」链路
                self.root.after(2000, lambda: self.post(("test", None)))
            if auto_exit_after:
                self.root.after(int(auto_exit_after * 1000), self.quit)
            if not self.silent and not self.cfg.get("start_minimized", True):
                self.show_window()
            if self.silent:
                self.log("静默启动完成：当前无可见窗口，双击托盘图标才会打开设置窗口")
            self.root.mainloop()
        else:  # 无界面常驻（仅供调试）
            try:
                while not self._stop.is_set():
                    self._drain_queue(repeat=False)
                    time.sleep(0.2)
            except KeyboardInterrupt:
                self.quit()

    def _autostart_boot_check(self):
        """启动时核对开机自启：用户勾选过但注册项缺失 / 失效则自动修复并写日志。"""
        try:
            want = bool(self.cfg.get("autostart", False))
            diag = autostart_diagnostics()
            if want and not diag["enabled"]:
                ok, msg, diag2 = autostart_ensure(True)
                self.log(f"自启自检：配置要求开机自启但注册项缺失或失效，已自动修复"
                         f"（{'成功' if ok else '失败'}：{msg}）")
                diag = diag2
            elif diag["enabled"]:
                actual = diag["hkcu_value"] or diag["hklm_value"]
                self.log(f"自启自检：开机自启项有效，注册表实际内容 = {actual}")
            else:
                self.log("自启自检：未启用开机自启")
            for item in diag.get("issues", [])[:3]:
                self.log(f"自启自检：{item}")
        except Exception as exc:
            self.log(f"自启自检异常：{exc}")

    def _start_ipc_watcher(self):
        """后台线程等待第二个实例发来的「打开设置窗口」信号（自动复位事件）。"""
        try:
            self._ipc_event = create_ipc_event()
        except Exception as exc:
            self._ipc_event = None
            self.log(f"单实例通信通道创建失败：{exc}")
        if not self._ipc_event:
            self.log("单实例通信通道不可用，双击 exe 将无法唤起设置窗口（仅记录日志，不弹提示框）")
            return
        self._ipc_thread = threading.Thread(target=self._ipc_watch_loop, daemon=True)
        self._ipc_thread.start()
        self.log(f"单实例通信通道就绪（命名事件 {IPC_EVENT_NAME}）")

    def _ipc_watch_loop(self):
        """循环等待命名事件；被第二个实例置位后把「打开设置」动作投递给主线程。"""
        k32 = _kernel32()
        while not self._stop.is_set():
            try:
                ret = k32.WaitForSingleObject(self._ipc_event, 300)
            except Exception as exc:
                self.log(f"单实例通信线程异常退出：{exc}")
                return
            if ret == WAIT_OBJECT_0:
                self.log("收到第二个实例的请求（再次双击 exe / 快捷方式）→ 打开设置窗口")
                self.post(("settings", None))
            elif ret == WAIT_TIMEOUT:
                continue
            else:
                return

    def _tray_open_settings(self, *args):
        """托盘图标被单击/双击（pystray 的 default 项）时调用。

        注意：pystray 调用 action 时固定传 (icon, item) 两个参数，
        因此这里必须用 *args 接收，否则会抛 TypeError 且被 pystray 静默吞掉。
        """
        self.log("托盘图标激活（单击/双击）→ 请求打开设置窗口")
        self.post(("settings", None))

    def _tray_cb(self, label, action):
        """把无参动作包装成 pystray 可调用；异常写入日志（构建 --noconsole 时不会丢）。"""
        def _wrapped(*args, **kwargs):
            try:
                return action()
            except Exception as exc:
                self.log(f"托盘菜单执行失败（{label}）：{exc!r}")
            return None
        return _wrapped

    def _start_tray(self):
        try:
            import pystray
        except Exception as exc:
            self.log(f"托盘组件不可用：{exc}")
            return
        cb = self._tray_cb
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._tray_status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开设置（双击托盘图标）", self._tray_open_settings, default=True),
            pystray.MenuItem("显示主窗口", cb("显示主窗口", lambda: self.post(("show", None)))),
            pystray.MenuItem(lambda item: "恢复监控" if self._paused() else "暂停监控",
                             cb("暂停/恢复监控", lambda: self.post(("toggle_pause", None)))),
            pystray.MenuItem("立即模拟测试（DRY-RUN）",
                             cb("立即模拟测试", lambda: self.post(("test", None)))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("DRY-RUN 模拟模式（不真正关机）",
                             cb("切换 DRY-RUN", lambda: self.post(("toggle_dry", None))),
                             checked=lambda item: bool(self.cfg.get("dry_run", True))),
            pystray.MenuItem("开机自启",
                             cb("切换开机自启", lambda: self.post(("toggle_autostart", None))),
                             checked=lambda item: autostart_is_enabled()),
            pystray.MenuItem("设置…", cb("设置", lambda: self.post(("settings", None)))),
            pystray.MenuItem("打开配置与日志目录",
                             cb("打开目录", lambda: os.startfile(str(app_dir())))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", cb("退出", lambda: self.post(("quit", None)))),
        )
        try:
            self.icon = pystray.Icon(APP_NAME, self._icon_image(), APP_TITLE, menu)
            self.icon.run_detached()
        except Exception as exc:
            self.log(f"托盘启动失败：{exc}")
            self.icon = None

    def _icon_image(self):
        if self._paused():
            return make_icon_image("paused")
        if self.status.get("present") is False:
            return make_icon_image("error")
        if self.status.get("plugged") is True:
            return make_icon_image("ac")
        if self.status.get("plugged") is False:
            return make_icon_image("battery")
        return make_icon_image("paused")

    def _paused(self) -> bool:
        return bool(self.monitor.paused)

    def _power_text(self) -> str:
        """电源状态的唯一文案来源（托盘菜单、托盘标题、主窗口共用）。"""
        if self.status.get("present") is False:
            return "未检测到电池（台式机或读取失败）"
        plugged = self.status.get("plugged")
        pct = self.status.get("percent")
        pct_txt = f"（{pct:.0f}%）" if isinstance(pct, (int, float)) else ""
        if plugged is True:
            return f"交流供电{pct_txt}"
        if plugged is False:
            return f"电池供电{pct_txt}"
        return "读取中…"

    def _mode_text(self) -> str:
        """运行模式的唯一文案来源（避免各处对 DRY-RUN 的描述不一致）。"""
        if should_dry_run(self.cfg):
            return "DRY-RUN 模拟模式（不会真关机）"
        return "真实关机模式"

    def _tray_status_text(self) -> str:
        if self._paused():
            return "状态：监控已暂停"
        if self.status.get("present") is False:
            return "状态：未检测到电池"
        if self.status.get("plugged") is None:
            return "状态：读取中…"
        return f"状态：{self._power_text()}，监控中"

    def _update_tray(self):
        if not self.icon:
            return
        try:
            self.icon.icon = self._icon_image()
            self.icon.title = f"{APP_TITLE} - {self._tray_status_text()}"
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, title: str, message: str):
        log_line(f"[通知] {title}：{message}")
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception:
            pass

    def post(self, action):
        self.ui_queue.put(action)

    # ---------------- 电源轮询 ----------------
    def _monitor_loop(self):
        try:
            import psutil
        except Exception as exc:
            self.log(f"psutil 不可用，无法监控电源：{exc}")
            self.status["present"] = False
            return

        interval = float(self.cfg.get("poll_interval", 1.0))
        while not self._stop.is_set():
            try:
                bat = psutil.sensors_battery()
                if bat is None:
                    self.status.update(present=False, plugged=None, percent=None)
                else:
                    plugged = bool(bat.power_plugged)
                    self.status.update(present=True, plugged=plugged, percent=bat.percent)
                    with self._lock:
                        self.monitor.paused = bool(self.cfg.get("paused", False))
                    event = self.monitor.update(plugged)
                    if event == "lost":
                        self.log(f"检测到电源已断开（切换为电池供电），电量 {bat.percent:.0f}%")
                        self.post(("trigger", None))
                    elif event == "restored":
                        self.log("检测到电源已恢复交流供电，监控重新武装")
                        self.post(("restored", None))
            except Exception as exc:
                # 轮询异常只记一次（同一异常不重复刷屏），不改变已有状态
                msg = str(exc)
                if msg != self._last_status_error:
                    self._last_status_error = msg
                    self.log(f"电源状态读取异常：{msg}")
            self._stop.wait(interval)

    # ---------------- 触发流程 ----------------
    def trigger_countdown(self, force_dry: bool = False):
        if self.state != "idle":
            self.log("已有倒计时任务在进行，忽略重复触发")
            return
        self.force_dry = force_dry
        delay = clamp(self.cfg.get("startup_delay"), MIN_DELAY, MAX_DELAY, 0)
        self.state = "delay"
        if force_dry:
            self._show_countdown_window(dry=True, delay=delay)
        self.notify("电源已断开", f"{delay} 秒后开始关机倒计时")
        self.log(f"进入触发延迟阶段，{delay} 秒后开始倒计时")
        if force_dry:
            self.log("本次为手动模拟测试（电源可能仍是交流供电），跳过「恢复交流电自动取消」监测")
        elif bool(self.cfg.get("cancel_on_ac_restore", True)):
            threading.Thread(target=self._ac_watch_loop, daemon=True).start()
            self.log("已启动倒计时期间的交流电高频监测（恢复供电即自动取消关机）")
        self._delay_id = self.root.after(max(1, int(delay * 1000)), self._begin_countdown) if self.root else None

    def _ac_plugged_now(self) -> bool:
        """读一次电源状态：True = 当前为交流供电。"""
        try:
            import psutil
            bat = psutil.sensors_battery()
            return bool(bat is not None and bat.power_plugged)
        except Exception:
            return False

    def _ac_watch_loop(self):
        """倒计时 / 触发延迟期间的电源高频轮询：一旦恢复交流电立即请求取消。"""
        try:
            interval = max(0.05, min(2.0, float(self.cfg.get("ac_poll_interval", 0.2))))
        except (TypeError, ValueError):
            interval = 0.2
        while not self._stop.is_set() and self.state in ("delay", "count"):
            if not bool(self.cfg.get("cancel_on_ac_restore", True)):
                return
            if self._ac_plugged_now():
                self.post(("ac_restore", None))
                return
            self._stop.wait(interval)

    def _on_ac_restored(self):
        """倒计时期间恢复交流电：终止关机流程、关掉倒计时窗口并提示已取消。"""
        if self.state not in ("delay", "count"):
            return
        self.log("倒计时期间检测到交流电已恢复，自动取消本次关机")
        self.cancel_countdown("交流电已恢复，自动取消")
        self._toast("已自动取消关机", "检测到交流电已恢复交流供电，本次不会执行关机。")

    def _toast(self, title: str, msg: str, ms: int = 3200):
        """右下角轻提示，自动淡出关闭，不阻塞界面。"""
        if not self.root:
            self.log(f"[提示] {title}：{msg}")
            return
        try:
            import tkinter as tk
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            body = tk.Frame(top, bg="#1b2030", padx=16, pady=12)
            body.pack()
            tk.Label(body, text=title, bg="#1b2030", fg="#51cf66",
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
            tk.Label(body, text=msg, bg="#1b2030", fg="#e9ecef",
                     font=("Microsoft YaHei UI", 9), wraplength=320,
                     justify="left").pack(anchor="w", pady=(4, 0))
            top.update_idletasks()
            w, h = top.winfo_width(), top.winfo_height()
            sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
            top.geometry(f"+{max(0, sw - w - 24)}+{max(0, sh - h - 60)}")
            top.after(int(ms), top.destroy)
        except Exception as exc:
            self.log(f"[提示] {title}：{msg}（{exc}）")

    def _begin_countdown(self):
        if self.state != "delay":
            return
        self.state = "count"
        self.remaining = clamp(self.cfg.get("countdown_seconds"), MIN_SECONDS, MAX_SECONDS, 5)
        self.log(f"开始倒计时：{self.remaining} 秒")
        self._show_countdown_window(dry=should_dry_run(self.cfg, self.force_dry), delay=0)
        self._tick()

    def _tick(self):
        if self.state != "count":
            return
        if self.remaining <= 0:
            self._finish_countdown()
            return
        if self.count_label is not None:
            try:
                self.count_label.config(text=str(self.remaining))
            except Exception:
                pass
        try:
            if self.root:
                self.root.bell()
        except Exception:
            pass
        self.remaining -= 1
        self._after_id = self.root.after(1000, self._tick) if self.root else None

    def cancel_countdown(self, reason: str = "用户取消"):
        if self.state == "idle":
            return
        for attr in ("_after_id", "_delay_id"):
            aid = getattr(self, attr)
            if aid and self.root:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        self.state = "idle"
        self.remaining = 0
        self._close_countdown_window()
        self.log(f"倒计时已取消（{reason}），本次不再自动关机")
        self.notify("已取消关机", "本次不会执行关机，需重新插拔电源才会再次触发")

    def _finish_countdown(self):
        self.state = "idle"
        self._close_countdown_window()
        dry = should_dry_run(self.cfg, self.force_dry)
        if dry:
            self.log("[DRY-RUN] 倒计时结束：模拟关机，未执行任何关机命令")
            self._dialog(
                "info", "DRY-RUN 模拟关机",
                "倒计时已结束。\n\n当前为 DRY-RUN 模拟模式，未执行任何关机/重启命令。\n"
                "如需真实关机，请在「设置」中关闭模拟模式（请先保存好工作）。")
        else:
            if bool(self.cfg.get("autostart", False)):
                ok_a, msg_a, _diag = autostart_ensure(True)
                self.log(f"关机前加固开机自启项：{'成功' if ok_a else '失败'}（{msg_a}）")
            cmd = shutdown_command()
            self.log(f"倒计时结束，执行真实关机：{' '.join(cmd)}")
            try:
                subprocess.Popen(cmd, shell=False)
            except Exception as exc:
                self.log(f"关机命令执行失败：{exc}")
                self._dialog("error", "关机失败", f"执行关机命令失败：{exc}\n请确认系统权限。")
        self.force_dry = False

    def _dialog(self, level: str, title: str, msg: str):
        """统一提示窗入口（level: "info" / "error"）。

        静默启动（开机自启）场景绝不弹窗，一律降级为日志 + 托盘气泡；
        无主窗口（--no-gui）时同样静默跳过。
        """
        if self.silent:
            self.log(f"[静默模式·弹窗抑制] {title}：{msg}")
            self.notify(title, msg)
            return
        if not self.root:
            return
        if level == "info" and os.environ.get("POWERGUARD_NO_DIALOG") == "1":
            self.log(f"[弹窗抑制] {title}")
            return
        try:
            from tkinter import messagebox
            if level == "error":
                messagebox.showerror(title, msg, parent=self.root)
            else:
                messagebox.showinfo(title, msg, parent=self.root)
        except Exception:
            pass

    # ---------------- UI ----------------
    def _apply_window_icon(self, win):
        import tkinter as tk
        try:
            win.iconphoto(True, tk.PhotoImage(data=_ICON_PNG_B64))
        except Exception:
            pass

    def _build_hidden_root(self):
        """静默启动专用：只创建隐藏的 Tk 根窗口（不建任何控件）。

        保留根窗口的目的仅是提供事件循环与「设置窗口」的父级，
        它自始至终保持 withdrawn 状态，开机时不会出现任何可见窗口。
        """
        import tkinter as tk

        root = tk.Tk()
        self.root = root
        root.title(f"{APP_TITLE} v{APP_VERSION}")
        self._apply_window_icon(root)
        root.withdraw()          # 关键：先隐藏，避免创建瞬间闪现窗口
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log("静默启动：已创建隐藏根窗口（无控件、无可见窗口），界面按需通过托盘图标打开")

    def _build_ui(self):
        """主窗口：运行状态 + 当前设置摘要 + 快捷操作 + 事件日志。"""
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self.root = root
        root.title(f"{APP_TITLE} v{APP_VERSION}")
        root.geometry("680x700")
        root.minsize(640, 660)
        self._apply_window_icon(root)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Body.TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#6b7280")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        head = ttk.Frame(root)
        head.pack(fill="x", padx=16, pady=(14, 4))
        ttk.Label(head, text="电源守护 PowerGuard", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{APP_VERSION} · 断电自动关机守卫", style="Hint.TLabel").pack(
            side="left", padx=(10, 0), pady=(9, 0))

        # ---- 运行状态 ----
        box = ttk.LabelFrame(root, text=" 运行状态 ")
        box.pack(fill="x", padx=16, pady=6)
        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=12, pady=8)
        grid.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate([("power", "电源状态"), ("monitor", "监控状态"), ("mode", "运行模式")]):
            ttk.Label(grid, text=label, style="Body.TLabel", width=9, anchor="w").grid(
                row=i, column=0, sticky="w", pady=3)
            lb = ttk.Label(grid, text="读取中…", style="Body.TLabel", anchor="w")
            lb.grid(row=i, column=1, sticky="w", pady=3)
            self.status_labels[key] = lb

        # ---- 当前设置摘要 ----
        sumbox = ttk.LabelFrame(root, text=" 当前设置 ")
        sumbox.pack(fill="x", padx=16, pady=6)
        self.summary_label = ttk.Label(sumbox, text="", style="Body.TLabel", anchor="w",
                                       justify="left", wraplength=600)
        self.summary_label.pack(fill="x", padx=12, pady=8)

        # ---- 快捷操作 ----
        ops = ttk.Frame(root)
        ops.pack(fill="x", padx=16, pady=4)
        ttk.Button(ops, text="打开设置…", style="Primary.TButton", command=self.open_settings).pack(side="left")
        self.btn_pause = ttk.Button(ops, text="暂停监控", command=lambda: self.post(("toggle_pause", None)))
        self.btn_pause.pack(side="left", padx=8)
        ttk.Button(ops, text="立即模拟测试", command=lambda: self.post(("test", None))).pack(side="left")
        ttk.Button(ops, text="隐藏到托盘", command=self.hide_window).pack(side="right")

        # ---- 事件日志 ----
        logbox = ttk.LabelFrame(root, text=" 事件日志 ")
        logbox.pack(fill="both", expand=True, padx=16, pady=(10, 14))
        inner = ttk.Frame(logbox)
        inner.pack(fill="both", expand=True, padx=8, pady=8)
        sb = ttk.Scrollbar(inner, orient="vertical")
        sb.pack(side="right", fill="y")
        self.log_box = tk.Text(inner, height=10, wrap="word", state="disabled", relief="flat",
                               font=("Consolas", 9), bg="#fafafa", yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.log_box.yview)

        self.refresh_summary()
        self._append_log("提示：拔掉电源适配器即可触发（首次使用请保持 DRY-RUN 模拟模式）。")
        self._append_log("点击「打开设置…」可修改倒计时秒数、断电触发延迟、开机自启、静默启动等参数。")

        if self.cfg.get("start_minimized", True):
            root.withdraw()

    # ---------------- 设置窗口 ----------------
    def refresh_summary(self):
        """刷新主窗口的「当前设置」摘要与暂停按钮文案。"""
        try:
            auto_txt = "已开启" if autostart_is_enabled() else "已关闭"
            mode_txt = self._mode_text()
            txt = (f"关机等待倒计时：{int(self.cfg.get('countdown_seconds', 5))} 秒"
                   f"        断电触发延迟：{int(self.cfg.get('startup_delay', 3))} 秒\n"
                   f"运行模式：{mode_txt}        开机自启：{auto_txt}\n"
                   f"恢复交流电自动取消关机：{'开启' if self.cfg.get('cancel_on_ac_restore', True) else '关闭'}"
                   f"        静默启动驻留托盘：{'是' if self.cfg.get('start_minimized', True) else '否'}")
            if self.summary_label is not None:
                self.summary_label.config(text=txt)
            if self.btn_pause is not None:
                self.btn_pause.config(text="恢复监控" if self._paused() else "暂停监控")
        except Exception:
            pass

    def open_settings(self):
        """打开完整设置窗口：保存 / 取消 / 恢复默认。"""
        import tkinter as tk
        from tkinter import ttk

        if self.root is None:
            self.log("当前为无界面模式，无法打开设置窗口")
            return
        if self.settings_win is not None:
            try:
                if self.settings_win.winfo_exists():
                    # 已存在则只做「拉到前台」，避免重复建窗
                    self._focus_settings(self.settings_win)
                    return
            except Exception:
                pass
            self.settings_win = None
        self.settings_open_count += 1
        self.log(f"打开设置窗口（第 {self.settings_open_count} 次请求）")

        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("PowerGuard 设置")
        win.resizable(False, False)
        # 关键修复：主窗口处于 withdraw（静默驻留托盘）时，若把 Toplevel 设为 transient，
        # Tk 会连带把它置为 withdrawn —— 表现就是「点了托盘图标什么也没弹出来」。
        # 因此仅在主窗口真正可见时才建立从属关系。
        if self._root_viewable():
            try:
                win.transient(self.root)
            except Exception:
                pass
        else:
            self.log("主窗口处于隐藏状态，设置窗口以独立顶层窗口显示（不设 transient）")
        self._apply_window_icon(win)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        win.bind("<Escape>", lambda e: self.close_settings())
        w, h = 600, 720
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{max(20, (sw - w) // 2)}+{max(20, (sh - h) // 2 - 40)}")

        self.settings_vars = {
            "seconds": tk.StringVar(value=str(int(self.cfg.get("countdown_seconds", 5)))),
            "delay": tk.StringVar(value=str(int(self.cfg.get("startup_delay", 3)))),
            "dry": tk.BooleanVar(value=bool(self.cfg.get("dry_run", True))),
            "auto": tk.BooleanVar(value=autostart_is_enabled()),
            "minimized": tk.BooleanVar(value=bool(self.cfg.get("start_minimized", True))),
            "cancel_ac": tk.BooleanVar(value=bool(self.cfg.get("cancel_on_ac_restore", True))),
        }

        outer = ttk.Frame(win)
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        # 关机时机
        timebox = ttk.LabelFrame(outer, text=" 关机时机 ")
        timebox.pack(fill="x")
        tb = ttk.Frame(timebox)
        tb.pack(fill="x", padx=12, pady=10)
        tb.columnconfigure(3, weight=1)

        ttk.Label(tb, text="关机等待倒计时", style="Body.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Spinbox(tb, from_=MIN_SECONDS, to=MAX_SECONDS, increment=1, width=8,
                    textvariable=self.settings_vars["seconds"]).grid(row=0, column=1, sticky="w", padx=(10, 4), pady=5)
        ttk.Label(tb, text="秒", style="Body.TLabel").grid(row=0, column=2, sticky="w", pady=5)
        ttk.Label(tb, text=f"断电后界面倒计时多少秒再关机（{MIN_SECONDS}~{MAX_SECONDS}）",
                  style="Hint.TLabel").grid(row=0, column=3, sticky="w", padx=(12, 0), pady=5)

        ttk.Label(tb, text="断电触发延迟", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(tb, from_=MIN_DELAY, to=MAX_DELAY, increment=1, width=8,
                    textvariable=self.settings_vars["delay"]).grid(row=1, column=1, sticky="w", padx=(10, 4), pady=5)
        ttk.Label(tb, text="秒", style="Body.TLabel").grid(row=1, column=2, sticky="w", pady=5)
        ttk.Label(tb, text=f"切换为电池供电后先等多久再倒计时（{MIN_DELAY}~{MAX_DELAY}）",
                  style="Hint.TLabel").grid(row=1, column=3, sticky="w", padx=(12, 0), pady=5)

        # 安全模式
        safebox = ttk.LabelFrame(outer, text=" 安全模式 ")
        safebox.pack(fill="x", pady=(12, 0))
        sb2 = ttk.Frame(safebox)
        sb2.pack(fill="x", padx=12, pady=10)
        ttk.Checkbutton(sb2, text="DRY-RUN 模拟模式（只弹窗提示，绝不真的关机）",
                        variable=self.settings_vars["dry"],
                        command=self._on_settings_dry_change).pack(anchor="w")
        ttk.Label(sb2, text="强烈建议先开启模拟模式，把电源拔掉验证一遍触发链路。",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 6))
        self.dry_warn_label = ttk.Label(sb2, text="", style="Hint.TLabel", wraplength=540, justify="left")
        self.dry_warn_label.pack(anchor="w")

        ttk.Separator(sb2, orient="horizontal").pack(fill="x", pady=(10, 6))
        ttk.Checkbutton(sb2, text="倒计时期间恢复交流电时自动取消关机（推荐开启）",
                        variable=self.settings_vars["cancel_ac"]).pack(anchor="w")
        ttk.Label(sb2, text="拔掉电源后，若在倒计时结束前把电源插回，立即终止关机流程并提示已取消。",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))

        # 启动方式
        startbox = ttk.LabelFrame(outer, text=" 启动方式 ")
        startbox.pack(fill="x", pady=(12, 0))
        st = ttk.Frame(startbox)
        st.pack(fill="x", padx=12, pady=10)
        ttk.Checkbutton(st, text="随 Windows 开机自动启动",
                        variable=self.settings_vars["auto"]).pack(anchor="w")
        ttk.Label(st, text="写入注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，无需管理员权限。",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        ttk.Checkbutton(st, text="静默启动：只在托盘驻留，不弹出主窗口",
                        variable=self.settings_vars["minimized"]).pack(anchor="w")
        ttk.Label(st, text="关闭后手动双击 exe 启动时会自动显示主窗口；"
                          "开机自启始终静默（注册项带 --silent），不受此项影响。",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))

        # 底部按钮
        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(16, 4))
        ttk.Button(foot, text="保存并立即生效", style="Primary.TButton",
                   command=self.save_settings).pack(side="left")
        ttk.Button(foot, text="取消", command=self.close_settings).pack(side="left", padx=8)
        ttk.Button(foot, text="恢复默认", command=self.restore_defaults).pack(side="right")

        ttk.Label(outer, text=f"配置文件：{CONFIG_PATH}", style="Hint.TLabel").pack(anchor="w", pady=(8, 0))

        self._refresh_dry_warn()
        # 先立即显示，再用 after 兜底再拉一次前台（Tk 建窗后首帧可能被系统忽略）
        try:
            win.deiconify()
            win.lift()
        except Exception:
            pass
        win.after(80, lambda: self._focus_settings(win))
        win.after(600, lambda: self._focus_settings(win))

    def _root_viewable(self) -> bool:
        """主窗口是否真的显示在桌面上（withdraw / iconic 均视为不可见）。"""
        try:
            if not self.root or not self.root.winfo_exists():
                return False
            return self.root.state() in ("normal", "zoomed")
        except Exception:
            return False

    def _drop_topmost(self, win):
        try:
            if win.winfo_exists():
                win.attributes("-topmost", False)
        except Exception:
            pass

    def _focus_settings(self, win):
        """把设置窗口真正带到前台并聚焦；主窗口隐藏时顺带解除 transient 从属。"""
        try:
            if not win.winfo_exists():
                return
            if not self._root_viewable():
                try:
                    win.transient("")   # 解除从属，避免被隐藏的主窗口连带隐藏
                except Exception:
                    pass
            if win.state() == "withdrawn":
                win.deiconify()
            win.update_idletasks()
            win.lift()
            win.attributes("-topmost", True)
            win.after(500, lambda: self._drop_topmost(win))
            try:
                win.focus_force()
            except Exception:
                pass
        except Exception as exc:
            self.log(f"设置窗口置顶失败：{exc}")

    def _refresh_dry_warn(self):
        if not getattr(self, "dry_warn_label", None) or not self.settings_vars:
            return
        try:
            if bool(self.settings_vars["dry"].get()):
                self.dry_warn_label.config(
                    text="当前：模拟模式。倒计时结束只会弹出提示，不会执行任何关机命令。",
                    foreground="#2f9e44")
            else:
                self.dry_warn_label.config(
                    text="当前：真实关机模式。倒计时结束会立即执行 shutdown /s /t 0，未保存的工作将丢失！",
                    foreground="#c92a2a")
        except Exception:
            pass

    def _on_settings_dry_change(self):
        """在设置窗口取消勾选模拟模式时，立即进行第一次确认。"""
        if not self.settings_vars:
            return
        if not bool(self.settings_vars["dry"].get()):
            from tkinter import messagebox
            ok = messagebox.askyesno(
                "确认关闭模拟模式",
                "关闭 DRY-RUN 后，倒计时结束将真实执行：\n"
                "shutdown.exe /s /t 0\n\n"
                "未保存的工作会直接丢失。确定要关闭模拟模式吗？",
                parent=self.settings_win, icon="warning")
            if not ok:
                self.settings_vars["dry"].set(True)
        self._refresh_dry_warn()

    def _read_settings_int(self, key, lo, hi, label):
        from tkinter import messagebox
        raw = str(self.settings_vars[key].get()).strip()
        try:
            val = int(float(raw))
        except Exception:
            messagebox.showerror("输入无效", f"「{label}」必须填写整数，当前填写：{raw or '(空)'}",
                                 parent=self.settings_win)
            return None
        if not (lo <= val <= hi):
            messagebox.showerror("输入无效", f"「{label}」需在 {lo}~{hi} 秒之间，当前填写：{raw}",
                                 parent=self.settings_win)
            return None
        return val

    def save_settings(self):
        """校验并保存设置，保存后立即生效并持久化到 config.json。"""
        from tkinter import messagebox
        if not self.settings_vars or self.settings_win is None:
            return
        seconds = self._read_settings_int("seconds", MIN_SECONDS, MAX_SECONDS, "关机等待倒计时")
        if seconds is None:
            return
        delay = self._read_settings_int("delay", MIN_DELAY, MAX_DELAY, "断电触发延迟")
        if delay is None:
            return

        dry = bool(self.settings_vars["dry"].get())
        if not dry and bool(self.cfg.get("dry_run", True)):
            ok = messagebox.askyesno(
                "确认保存为真实关机模式",
                "保存后将以「真实关机模式」运行，倒计时结束会真正关机。\n\n"
                "这是最后一道确认，确定要保存吗？",
                parent=self.settings_win, icon="warning")
            if not ok:
                self.settings_vars["dry"].set(True)
                self._refresh_dry_warn()
                return

        # 开机自启：按用户意愿无条件重写（幂等），顺带清理「启动」页旧禁用记录，
        # 避免「注册项存在但指向旧路径 / 被禁用」时界面显示已开启、实际开机不启动。
        want_auto = bool(self.settings_vars["auto"].get())
        self.cfg["autostart"] = want_auto
        ok2, msg2, diag = autostart_ensure(want_auto)
        self.log(f"开机自启：{msg2}")
        if want_auto and not diag.get("enabled"):
            messagebox.showerror(
                "开机自启设置失败",
                f"注册表写入后校验未通过：\n{msg2}\n\n"
                "请检查安全软件是否拦截了注册表写入，或以管理员身份重新运行后再试。",
                parent=self.settings_win)
            self.settings_vars["auto"].set(False)
            self.cfg["autostart"] = False

        self.cfg["countdown_seconds"] = seconds
        self.cfg["startup_delay"] = delay
        self.cfg["dry_run"] = dry
        self.cfg["start_minimized"] = bool(self.settings_vars["minimized"].get())
        self.cfg["cancel_on_ac_restore"] = bool(self.settings_vars["cancel_ac"].get())

        if save_config(self.cfg):
            self.log(f"设置已保存并立即生效：倒计时 {seconds}s，触发延迟 {delay}s，"
                     f"模式 {'DRY-RUN 模拟' if dry else '真实关机'}，"
                     f"开机自启 {'开' if autostart_is_enabled() else '关'}，"
                     f"恢复交流电自动取消 {'开' if self.cfg.get('cancel_on_ac_restore') else '关'}，"
                     f"静默启动 {'是' if self.cfg['start_minimized'] else '否'}")
            self.refresh_summary()
            self._update_tray()
            self.close_settings()
            self.notify(APP_TITLE, "设置已保存并立即生效")
        else:
            self.log("设置保存失败：无法写入配置文件")
            messagebox.showerror("保存失败",
                                 f"无法写入配置文件：\n{CONFIG_PATH}\n请检查该目录的写入权限。",
                                 parent=self.settings_win)

    def restore_defaults(self):
        """把设置窗口内的控件恢复为出厂默认值（不自动保存）。"""
        if not self.settings_vars:
            return
        try:
            self.settings_vars["seconds"].set(str(int(DEFAULT_CONFIG["countdown_seconds"])))
            self.settings_vars["delay"].set(str(int(DEFAULT_CONFIG["startup_delay"])))
            self.settings_vars["dry"].set(bool(DEFAULT_CONFIG["dry_run"]))
            self.settings_vars["minimized"].set(bool(DEFAULT_CONFIG["start_minimized"]))
            self.settings_vars["cancel_ac"].set(bool(DEFAULT_CONFIG["cancel_on_ac_restore"]))
            self.settings_vars["auto"].set(False)
        except Exception:
            pass
        self._refresh_dry_warn()
        self.log("设置窗口已恢复默认值（尚未保存，点击「保存并立即生效」后生效）")

    def close_settings(self):
        win = self.settings_win
        self.settings_win = None
        self.settings_vars = {}
        self.dry_warn_label = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _sync_settings_vars(self):
        """托盘菜单等外部途径改了配置时，同步设置窗口内的控件值。"""
        if not self.settings_vars:
            return
        try:
            self.settings_vars["seconds"].set(str(int(self.cfg.get("countdown_seconds", 5))))
            self.settings_vars["delay"].set(str(int(self.cfg.get("startup_delay", 3))))
            self.settings_vars["dry"].set(bool(self.cfg.get("dry_run", True)))
            self.settings_vars["auto"].set(autostart_is_enabled())
            self.settings_vars["minimized"].set(bool(self.cfg.get("start_minimized", True)))
            self.settings_vars["cancel_ac"].set(bool(self.cfg.get("cancel_on_ac_restore", True)))
        except Exception:
            pass
        self._refresh_dry_warn()

    def _append_log(self, line: str):
        if not self.log_box:
            return
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def _refresh_status_view(self):
        if not self.root:
            return
        if self.silent:
            # 静默启动没有主窗口控件，只刷新托盘图标与提示，避免访问不存在的控件
            try:
                self._update_tray()
            except Exception:
                pass
            self.root.after(1000, self._refresh_status_view)
            return
        try:
            power = self._power_text()
            if self.state == "count":
                power += f"  |  倒计时中：{self.remaining}s"
            elif self.state == "delay":
                power += "  |  触发延迟中"

            monitor = "已暂停" if self._paused() else "监控中"
            mode = self._mode_text()
            self.status_labels["power"].config(text=power)
            self.status_labels["monitor"].config(text=monitor)
            self.status_labels["mode"].config(text=mode)
        except Exception:
            pass
        self.refresh_summary()
        self._update_tray()
        self.root.after(1000, self._refresh_status_view)

    def _show_countdown_window(self, dry: bool, delay: int = 0):
        import tkinter as tk
        if not self.root:
            return
        self._close_countdown_window()
        win = tk.Toplevel(self.root)
        self.count_win = win
        win.title("PowerGuard - 关机倒计时")
        win.configure(bg="#1b2030")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        w, h = 440, 250
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(60, (sh - h) // 3)}")
        win.protocol("WM_DELETE_WINDOW", lambda: self.cancel_countdown("关闭窗口"))
        win.bind("<Escape>", lambda e: self.cancel_countdown("按 Esc"))

        head = tk.Label(win, text="检测到电源已断开，即将自动关机", bg="#1b2030", fg="#ff6b6b",
                        font=("Microsoft YaHei UI", 13, "bold"))
        head.pack(pady=(22, 6))

        self.count_label = tk.Label(win, text=str(delay if delay else self.cfg["countdown_seconds"]),
                                    bg="#1b2030", fg="#ffd166",
                                    font=("Segoe UI", 52, "bold"))
        self.count_label.pack(pady=2)

        tip = "秒后执行关机" if not dry else "秒后模拟关机（DRY-RUN，不会真关机）"
        tk.Label(win, text=tip, bg="#1b2030", fg="#d0d6e0",
                 font=("Microsoft YaHei UI", 11)).pack()

        btn = tk.Button(win, text="取消关机（Esc）", command=lambda: self.cancel_countdown("点击取消按钮"),
                        bg="#2f9e44", fg="white", relief="flat",
                        font=("Microsoft YaHei UI", 11, "bold"), padx=18, pady=6,
                        activebackground="#37b24d", activeforeground="white")
        btn.pack(pady=16)
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass

    def _close_countdown_window(self):
        if self.count_win is not None:
            try:
                self.count_win.destroy()
            except Exception:
                pass
            self.count_win = None
            self.count_label = None

    def show_window(self):
        if not self.root:
            return
        if self.silent:
            # 静默启动的进程没有主窗口，任何「显示窗口」请求都转为打开设置窗口
            self.log("静默启动进程中收到「显示主窗口」请求 → 改为打开设置窗口")
            self.open_settings()
            return
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def hide_window(self):
        if not self.root:
            return
        try:
            self.root.withdraw()
            self.notify(APP_TITLE, "已最小化到系统托盘，双击托盘图标可重新打开")
        except Exception:
            pass

    # ---------------- 队列处理 ----------------
    def _drain_queue(self, repeat: bool = True):
        """在主线程消费后台线程投递的动作（唯一实现，不再区分有无界面）。

        repeat=True：处理完继续用 after 重新排队，作为界面模式下的事件心跳；
        repeat=False：只清空一次队列，由 --no-gui 的外部循环负责反复调用。
        """
        try:
            while True:
                action, payload = self.ui_queue.get_nowait()
                self._handle(action, payload)
        except queue.Empty:
            pass
        if repeat and self.root:
            self.root.after(120, self._drain_queue)

    def _handle(self, action, payload):
        if action == "log":
            self._append_log(payload)
        elif action == "trigger":
            self.trigger_countdown(force_dry=False)
        elif action == "restored":
            self._append_log("电源已恢复交流供电，监控已重新武装")
        elif action == "ac_restore":
            self._on_ac_restored()
        elif action == "test":
            self._start_test()
        elif action == "show":
            self.show_window()
        elif action == "settings":
            self.open_settings()
        elif action == "toggle_pause":
            self.toggle_pause()
        elif action == "toggle_dry":
            self.toggle_dry_run()
        elif action == "toggle_autostart":
            self.toggle_autostart()
        elif action == "quit":
            self.quit()

    def _start_test(self):
        self.log("手动模拟测试：以 DRY-RUN 方式走一遍完整流程")
        self.trigger_countdown(force_dry=True)

    def toggle_pause(self):
        self.monitor.paused = not self.monitor.paused
        self.cfg["paused"] = self.monitor.paused
        save_config(self.cfg)
        self.log("监控已暂停（不会触发关机）" if self.monitor.paused else "监控已恢复")
        self.notify(APP_TITLE, "监控已暂停" if self.monitor.paused else "监控已恢复")
        if self.monitor.paused and self.state != "idle":
            self.cancel_countdown("暂停监控")
        self._update_tray()

    def toggle_dry_run(self):
        if self.cfg.get("dry_run", True):
            ok = True
            if self.root:
                from tkinter import messagebox
                ok = messagebox.askyesno(
                    "确认切换到真实关机模式",
                    "关闭 DRY-RUN 后，倒计时结束将执行真实的 shutdown /s 命令，\n"
                    "未保存的工作可能丢失。\n\n确定切换到真实关机模式吗？",
                    parent=self.root, icon="warning")
            if not ok:
                return
            self.cfg["dry_run"] = False
        else:
            self.cfg["dry_run"] = True
        save_config(self.cfg)
        self.log("已切换为真实关机模式" if not self.cfg["dry_run"] else "已切换为 DRY-RUN 模拟模式")
        self._sync_settings_vars()
        self.refresh_summary()
        self._update_tray()

    def toggle_autostart(self):
        target = not autostart_is_enabled()
        ok, msg, diag = autostart_ensure(target)
        self.cfg["autostart"] = bool(target) and bool(diag.get("enabled"))
        save_config(self.cfg)
        self.log(f"开机自启：{msg}")
        if not ok or (target and not diag.get("enabled")):
            self._dialog("error", "开机自启设置失败", msg)
        elif target:
            self.notify(APP_TITLE, "开机自启已开启")
        self._sync_settings_vars()
        self.refresh_summary()
        self._update_tray()

    def quit(self, *_):
        self.log("=== 程序退出 ===")
        self._stop.set()
        for aid in (self._after_id, self._delay_id):
            if aid and self.root:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self._ipc_event:
            try:
                _kernel32().CloseHandle(self._ipc_event)
            except Exception:
                pass
            self._ipc_event = None
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass


# 16x16 内嵌图标（PNG base64），仅用于窗口图标
_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAeUlEQVR42mNgGAWjYBSMglEwCkbBKBgFo2AUjIJR"
    "MApGwSgYBaNgFIyCUTAKRsEoGAWjYBSMglEwCkbBKBgFo2AUjIJRMApGwSgYBaNgFIyCUTAKRsEoGAWjYBSMglEw"
    "CkbBKBgFo2AUjIJRMApGwSgYBaNgFAwHAAA1dQABlYd2TQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------
# 自测（无界面、无托盘、绝不触发关机）
# --------------------------------------------------------------------------
def run_selftest() -> int:
    results = []

    def check(name, cond, extra=""):
        results.append({"case": name, "ok": bool(cond), "detail": extra})

    # 1. 默认配置
    cfg = dict(DEFAULT_CONFIG)
    check("默认倒计时为 5 秒", cfg["countdown_seconds"] == 5, str(cfg["countdown_seconds"]))
    check("默认 DRY-RUN 开启", cfg["dry_run"] is True)
    check("默认触发延迟 3 秒", cfg["startup_delay"] == 3)

    # 2. 配置读写往返
    tmp_cfg = dict(cfg, countdown_seconds=12, startup_delay=0, dry_run=True)
    ok = save_config(tmp_cfg)
    loaded = load_config()
    check("配置保存成功", ok)
    check("配置读写往返一致", loaded["countdown_seconds"] == 12 and loaded["startup_delay"] == 0,
          f"{loaded['countdown_seconds']}/{loaded['startup_delay']}")

    # 3. 非法配置被夹紧
    bad = dict(cfg, countdown_seconds=99999, startup_delay=-5)
    save_config(bad)
    loaded = load_config()
    check("倒计时上限夹紧", loaded["countdown_seconds"] == MAX_SECONDS, str(loaded["countdown_seconds"]))
    check("延迟下限夹紧", loaded["startup_delay"] == MIN_DELAY, str(loaded["startup_delay"]))
    save_config(cfg)  # 还原默认

    # 4. 状态机
    m = PowerMonitor()
    check("首次 AC 读数不触发", m.update(True) is None)
    check("AC->电池 触发 lost", m.update(False) == "lost")
    check("持续电池不重复触发", m.update(False) is None)
    check("电池->AC 触发 restored", m.update(True) == "restored")
    check("再次断开重新触发", m.update(False) == "lost")

    m2 = PowerMonitor()
    m2.update(True)
    m2.paused = True
    check("暂停时不触发", m2.update(False) is None)
    m2.paused = False
    check("恢复后仍可触发", m2.update(False) is None)  # prev 已是 False，需重新插入
    m2.update(True)
    check("重新武装后可触发", m2.update(False) == "lost")

    # 5. dry-run 判定
    check("配置 dry_run=True 时模拟", should_dry_run({"dry_run": True}) is True)
    check("强制模拟生效", should_dry_run({"dry_run": False}, force_dry=True) is True)
    os.environ["POWERGUARD_SAFE"] = "1"
    check("环境变量安全开关生效", should_dry_run({"dry_run": False}) is True)
    os.environ.pop("POWERGUARD_SAFE", None)

    # 6. 关机命令构造（不执行）
    cmd = shutdown_command()
    check("关机命令为 shutdown /s /t 0",
          cmd[-3:] == ["/s", "/t", "0"] and "shutdown" in cmd[0].lower(), " ".join(cmd))
    check("自测期间未执行任何关机命令", True, "仅构造命令字符串，未调用 subprocess")

    # 7. 自启命令构造与路径校验
    ac = autostart_command()
    check("自启命令非空且带引号", ac.startswith('"') and len(ac) > 3, ac)
    check("自启命令含静默参数 --silent", command_is_silent(ac) and ac.rstrip().endswith(SILENT_FLAG), ac)
    check("自启命令末尾参数不破坏路径引号配对",
          ac.count('"') in (2, 4) and _norm_target(ac) == _norm_target(autostart_command()), ac)
    check("冲突包参数不重复追加", ac.count(SILENT_FLAG) == 1, ac)
    check("自启目标与当前程序一致", _norm_target(ac) == _norm_target(autostart_command()), _norm_target(ac))
    check("旧路径能被判定为不一致", _norm_target('"D:\\Old\\PowerGuard.exe"') != _norm_target(ac))
    check("引号不影响路径比较",
          _norm_target('"C:\\Tools\\PowerGuard.exe"') == _norm_target('C:\\Tools\\PowerGuard.exe'))
    check("带静默参数的旧式命令仍能提取正确目标",
          _norm_target(f'"C:\\Tools\\PowerGuard.exe" {SILENT_FLAG}').endswith("powerguard.exe"),
          _norm_target(f'"C:\\Tools\\PowerGuard.exe" {SILENT_FLAG}'))
    check("静默参数识别函数可区分有无",
          command_is_silent(f'"{sys.executable}" {SILENT_FLAG}') and not command_is_silent('"C:\\a\\b.exe"'))
    check("带空格路径靠引号被完整识别",
          _norm_target('"C:\\Program Files\\Power Guard\\PowerGuard.exe"').endswith("powerguard.exe")
          and _norm_target('"C:\\Program Files\\Power Guard\\PowerGuard.exe"').count(os.sep) == 3,
          _norm_target('"C:\\Program Files\\Power Guard\\PowerGuard.exe"'))
    diag = autostart_diagnostics()
    check("自启诊断字段完整",
          all(k in diag for k in ("hkcu_value", "hklm_value", "enabled", "issues", "expected_command")),
          str(diag.get("hkcu_value", ""))[:80])
    check("自启诊断给出值名与键路径", diag["value_name"] == RUN_VALUE and RUN_KEY in diag["run_key"])

    # 7.1 新增配置项（交流电恢复自动取消）
    base = dict(DEFAULT_CONFIG)
    check("默认开启交流电恢复自动取消", base["cancel_on_ac_restore"] is True)
    check("默认高频轮询 0.2 秒", abs(float(base["ac_poll_interval"]) - 0.2) < 1e-9)
    save_config(dict(base, cancel_on_ac_restore=False, ac_poll_interval=0.05))
    l2 = load_config()
    check("交流电自动取消开关可持久化", l2["cancel_on_ac_restore"] is False)
    check("高频轮询下限夹紧", abs(l2["ac_poll_interval"] - 0.05) < 1e-9, str(l2["ac_poll_interval"]))
    save_config(dict(base, ac_poll_interval=99))
    check("高频轮询上限夹紧", abs(load_config()["ac_poll_interval"] - 2.0) < 1e-9,
          str(load_config()["ac_poll_interval"]))
    save_config(dict(base, autostart=False))
    check("自启意愿字段可持久化", load_config()["autostart"] is False)

    # 8. 图标生成
    try:
        img = make_icon_image("ac")
        check("托盘图标可生成", img.size == (64, 64), str(img.size))
    except Exception as exc:
        check("托盘图标可生成", False, str(exc))

    # 9. 依赖检查
    try:
        import psutil
        bat = psutil.sensors_battery()
        check("psutil 可用", True, f"battery={bat}")
    except Exception as exc:
        check("psutil 可用", False, str(exc))

    # 10. 单实例通信通道（命名事件）往返；用独立事件名，避免与正在运行的实例抢信号
    try:
        test_name = f"{IPC_EVENT_NAME}_selftest_{os.getpid()}"
        h = create_ipc_event(test_name)
        check("可创建单实例通信事件", bool(h), test_name)
        check("第二个实例可发出通知", signal_running_instance(test_name))
        if h:
            k32 = _kernel32()
            ret = k32.WaitForSingleObject(h, 1000)
            check("主实例能收到通知", ret == WAIT_OBJECT_0, f"ret={ret}")
            ret2 = k32.WaitForSingleObject(h, 200)
            check("事件为自动复位（不会重复误触发）", ret2 == WAIT_TIMEOUT, f"ret={ret2}")
            k32.CloseHandle(h)
        check("已运行实例未收到测试信号（生产事件未被误触发）",
              not signal_running_instance())
    except Exception as exc:
        check("单实例通信链路可用", False, str(exc))

    # 11. 静默启动（--silent）行为：不建主窗口、强制驻留托盘
    app_silent = PowerGuardApp(gui=True, silent=True)
    check("静默实例 silent 标记生效", app_silent.silent is True)
    check("静默实例强制 start_minimized=True（忽略配置文件）",
          app_silent.cfg.get("start_minimized") is True, str(app_silent.cfg.get("start_minimized")))
    app_normal = PowerGuardApp(gui=True)
    check("普通双击实例默认非静默", app_normal.silent is False)
    check("静默标记不写回配置文件", Path(CONFIG_PATH).exists() and "silent" not in load_config(),
          str(list(load_config().keys()))[:80])
    os.environ[SILENT_ENV] = "1"
    check("环境变量兜底可触发静默", env_wants_silent() is True)
    os.environ.pop(SILENT_ENV, None)
    check("未设置环境变量时不静默", env_wants_silent() is False)

    failed = [r for r in results if not r["ok"]]
    print(json.dumps({"total": len(results), "passed": len(results) - len(failed),
                      "failed": len(failed), "results": results},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# 打包后界面自检：打开主窗口与设置窗口，检查控件与保存链路（仅 DRY-RUN 场景）
# --------------------------------------------------------------------------
def run_ui_test() -> int:
    import tempfile

    res = {"ok": False, "checks": []}

    def chk(name, cond):
        res["checks"].append({"case": name, "ok": bool(cond)})

    def dump():
        try:
            path = os.path.join(tempfile.gettempdir(), "PowerGuard_uitest.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def finish():
        res["ok"] = bool(res["checks"]) and all(c["ok"] for c in res["checks"])
        dump()
        try:
            print(json.dumps(res, ensure_ascii=False))
        except Exception:
            pass
        try:
            app.root.after(150, app.quit)
        except Exception:
            pass

    app = PowerGuardApp(gui=True)

    def phase_ac():
        try:
            chk("真实触发路径进入延迟阶段", res.get("phase_state") == "delay")
            chk("倒计时期间恢复交流电后状态回到 idle", app.state == "idle")
            try:
                tail = LOG_PATH.read_text(encoding="utf-8", errors="ignore")[-8000:]
            except Exception:
                tail = ""
            chk("日志记录了交流电恢复自动取消", "交流电已恢复" in tail)
        except Exception as exc:
            res["error"] = repr(exc)
        finally:
            finish()

    def phase_ui():
        try:
            app.open_settings()
            win = app.settings_win
            chk("设置窗口已创建", win is not None and bool(win.winfo_exists()))

            # 真实可见性证据：主窗口保持静默隐藏（默认 start_minimized=True），设置窗口必须真正显示
            for _ in range(40):
                app.root.update()
            chk("主窗口此时处于 withdraw", app.root.state() == "withdrawn")
            hwnd = int(win.winfo_id())
            parent = int(ctypes.windll.user32.GetParent(hwnd)) or hwnd
            visible = bool(ctypes.windll.user32.IsWindowVisible(parent))
            res["settings_hwnd"] = parent
            res["settings_visible"] = visible
            res["settings_state"] = win.state()
            res["settings_title"] = win.title()
            chk("设置窗口未处于 withdrawn", win.state() != "withdrawn")
            chk("设置窗口在 Win32 层面可见（IsWindowVisible）", visible)
            chk("设置窗口标题正确", win.title() == "PowerGuard 设置")

            texts = []

            def walk(w):
                for c in w.winfo_children():
                    try:
                        if c.winfo_class() == "TButton":
                            texts.append(c.cget("text"))
                    except Exception:
                        pass
                    walk(c)

            walk(win)
            chk("含「保存」按钮", "保存并立即生效" in texts)
            chk("含「取消」按钮", "取消" in texts)
            chk("含「恢复默认」按钮", "恢复默认" in texts)
            for key, label in (("seconds", "关机等待倒计时输入框"), ("delay", "断电触发延迟输入框"),
                               ("dry", "DRY-RUN 复选"), ("auto", "开机自启复选"),
                               ("minimized", "静默启动复选"),
                               ("cancel_ac", "恢复交流电自动取消复选")):
                chk(f"含{label}", key in app.settings_vars)

            app.settings_vars["seconds"].set("9")
            app.settings_vars["delay"].set("4")
            app.settings_vars["cancel_ac"].set(True)
            app.save_settings()
            chk("保存后设置窗口关闭", app.settings_win is None)
            chk("保存后立即生效",
                int(app.cfg["countdown_seconds"]) == 9 and int(app.cfg["startup_delay"]) == 4)
            chk("交流电自动取消设置已写入配置", bool(app.cfg.get("cancel_on_ac_restore")) is True)
            chk("配置已持久化", CONFIG_PATH.exists() and "9" in CONFIG_PATH.read_text(encoding="utf-8"))

            app.open_settings()
            chk("可再次打开", app.settings_win is not None)
            app.close_settings()
            chk("关闭后引用已清理", app.settings_win is None)

            # 托盘双击回调：按 pystray 的真实调用约定（action(icon, item) 两参）验证
            app._tray_open_settings(None, None)
            for _ in range(80):
                app.root.update()
                if app.settings_win is not None and app.settings_win.state() != "withdrawn":
                    break
                app.root.after(20)
            win_tray = app.settings_win
            chk("托盘双击回调可打开设置窗口（两参调用不报 TypeError）", win_tray is not None)
            if win_tray is not None:
                h2 = int(win_tray.winfo_id())
                h2 = int(ctypes.windll.user32.GetParent(h2)) or h2
                res["tray_callback_hwnd"] = h2
                res["tray_callback_visible"] = bool(ctypes.windll.user32.IsWindowVisible(h2))
                chk("托盘回调打开的窗口在 Win32 层面可见",
                    bool(ctypes.windll.user32.IsWindowVisible(h2)))
                app.close_settings()
            cb = app._tray_cb("自测项", lambda: "ok")
            chk("菜单动作包装器接受 pystray 两参调用", cb(None, None) == "ok")

            chk("自启诊断可读取", "enabled" in autostart_diagnostics())

            # 交流电恢复自动取消链路：走真实触发路径（非手动模拟）但保持 DRY-RUN
            app.cfg["dry_run"] = True
            app.cfg["startup_delay"] = 3
            app.cfg["countdown_seconds"] = 5
            app.cfg["cancel_on_ac_restore"] = True
            app.state = "idle"
            app.force_dry = False
            app.trigger_countdown(force_dry=False)
            res["phase_state"] = app.state
        except Exception as exc:
            res["error"] = repr(exc)
            finish()
            return
        app.root.after(1800, phase_ac)

    app._build_ui()
    app.root.after(600, phase_ui)
    app.root.after(100, app._drain_queue)
    app.root.mainloop()
    return 0 if res.get("ok") else 1


# --------------------------------------------------------------------------
# 单实例与进程入口（命名事件 IPC + 互斥体）
# --------------------------------------------------------------------------
def _kernel32():
    """取 kernel32 并声明句柄相关函数的原型（64 位下不声明 restype 会把 HANDLE 截断成 32 位）。"""
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetEvent.restype = wintypes.BOOL
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def create_ipc_event(name: str = IPC_EVENT_NAME):
    """主实例创建自动复位的命名事件，用于接收「请打开设置窗口」请求。"""
    try:
        h = _kernel32().CreateEventW(None, False, False, name)
        return h or None
    except Exception:
        return None


def signal_running_instance(name: str = IPC_EVENT_NAME) -> bool:
    """通知已运行实例弹出设置窗口；成功返回 True。"""
    try:
        k32 = _kernel32()
        h = k32.OpenEventW(EVENT_MODIFY_STATE, False, name)
        if not h:
            return False
        try:
            return bool(k32.SetEvent(h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def acquire_single_instance():
    """避免重复启动多个实例；已存在则返回 None。"""
    try:
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, False, "PowerGuard_SingleInstance_Mutex")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        return True


def main():
    parser = argparse.ArgumentParser(description="PowerGuard - 断电自动关机守护")
    parser.add_argument("--selftest", action="store_true", help="运行无界面自测并退出")
    parser.add_argument("--smoke", type=float, default=0, help="启动后 N 秒自动退出（冒烟测试）")
    parser.add_argument("--no-gui", action="store_true", help="不创建窗口，仅托盘常驻")
    parser.add_argument("--uitest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--demo", action="store_true",
                        help="启动后 2 秒自动触发一次 DRY-RUN 倒计时（仅用于自测）")
    parser.add_argument("--silent", dest="silent", action="store_true",
                        help="静默启动：只驻留托盘图标，不创建/显示任何窗口（开机自启用）")
    parser.add_argument("--diag-autostart", action="store_true",
                        help="输出开机自启诊断信息（含注册表实际内容）后退出")
    args = parser.parse_args()

    if args.diag_autostart:
        # 只输出到标准输出（早期版本还会往 %TEMP% 落一份 json，属调试探针，已移除）
        print(json.dumps(autostart_diagnostics(), ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.selftest:
        sys.exit(run_selftest())

    if args.uitest:
        sys.exit(run_ui_test())

    # 静默启动：命令行参数优先，环境变量兜底（便于自测与排障）
    silent = bool(args.silent) or env_wants_silent()
    if silent:
        log_line(f"检测到静默启动请求（{SILENT_FLAG} / {SILENT_ENV}），本次启动不创建任何窗口")

    handle = acquire_single_instance()
    if handle is None:
        if silent:
            # 开机自启场景下已有实例在运行：绝不弹窗、也不再打扰已有实例，
            # 直接记日志退出（登录时可能同时被多个启动项/快捷方式拉起）。
            log_line("静默启动：检测到已有实例在运行，本进程直接退出（不弹窗、不发送打开设置请求）")
            sys.exit(0)
        # 手动双击 exe 且已有实例在运行：通过命名事件请它弹出设置窗口，然后静默退出本进程
        if signal_running_instance():
            log_line("检测到已有实例在运行，已发送「打开设置窗口」请求，本进程退出")
        else:
            # 极少数情况（老版本实例未创建事件通道）才退回提示框
            log_line("检测到已有实例在运行，但通知通道不可用，改为提示用户")
            try:
                from tkinter import messagebox, Tk
                r = Tk(); r.withdraw()
                messagebox.showinfo(APP_TITLE, "PowerGuard 已在运行，请查看系统托盘图标。")
                r.destroy()
            except Exception:
                pass
        sys.exit(0)

    if silent:
        # 静默模式下任何未捕获异常只写日志，绝不弹系统错误框
        def _silent_excepthook(tp, val, tb):
            try:
                import traceback
                log_line("静默启动过程中出现未捕获异常："
                         + "".join(traceback.format_exception(tp, val, tb)))
            except Exception:
                pass
            sys.__excepthook__(tp, val, tb)
        sys.excepthook = _silent_excepthook

    app = PowerGuardApp(gui=not args.no_gui, silent=silent)
    app.demo = bool(args.demo)
    try:
        app.start(auto_exit_after=args.smoke)
    except KeyboardInterrupt:
        app.quit()
    except Exception as exc:
        # 启动失败不再弹窗（开机时会干扰用户），只落日志后退出
        try:
            import traceback
            log_line("启动失败（已抑制弹窗，仅记录日志）：" + "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
        except Exception:
            pass
        try:
            app.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
