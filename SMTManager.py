"""
SMT Manager 1.0.1

특정 게임이 실행되면 해당 프로세스의 CPU affinity를 조정해
SMT가 꺼진 것과 비슷한 효과를 내도록 돕는 Windows 전용 도구입니다.
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional

import psutil
import pystray
import tkinter as tk
from PIL import Image, ImageDraw
from tkinter import messagebox, scrolledtext, ttk

try:
    import pythoncom
    import win32com.client

    HAS_WMI = True
except ImportError:
    HAS_WMI = False


APP_NAME = "SMT Manager"
APP_VERSION = "1.0.1"
APP_AUTHOR = "IMSOBRA"
TASK_NAME = "SMTManager"
MUTEX_NAME = "SMTManager_Mutex"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "smt_config.json")
LOG_PATH = os.path.join(BASE_DIR, "smt_log.txt")

STATUS_WAITING = "게임 대기 중"
STATUS_DISABLED = "자동 SMT OFF 비활성화"

DEFAULT_CONFIG = {
    "enabled": True,
    "check_interval": 20,
    "game_processes": ["Client", "heroes", "heroes_x64"],
    "custom_mask": "",
}


logger = logging.getLogger("SMTManager")
logger.setLevel(logging.INFO)
logger.handlers.clear()
file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(file_handler)


PROCESSOR_RELATIONSHIP_CORE = 0
ERROR_INSUFFICIENT_BUFFER = 122


class SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("Relationship", ctypes.c_uint32),
        ("Size", ctypes.c_uint32),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> None:
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params: List[str] = []
    else:
        executable = sys.executable
        params = [os.path.abspath(__file__)]

    params.extend(sys.argv[1:])
    joined = subprocess.list2cmdline(params)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, joined, None, 1)
    sys.exit()


def normalize_process_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized


def get_logical_cpu_count() -> int:
    return psutil.cpu_count(logical=True) or 1


def mask_to_cpu_list(mask: int, cpu_count: Optional[int] = None) -> List[int]:
    total = cpu_count or get_logical_cpu_count()
    return [index for index in range(total) if mask & (1 << index)]


def sanitize_mask(mask: int, cpu_count: Optional[int] = None) -> int:
    total = cpu_count or get_logical_cpu_count()
    if total <= 0:
        return 0
    allowed_bits = (1 << total) - 1
    return mask & allowed_bits


def query_physical_core_mask() -> int:
    kernel32 = ctypes.windll.kernel32
    size = ctypes.c_ulong(0)

    result = kernel32.GetLogicalProcessorInformationEx(
        PROCESSOR_RELATIONSHIP_CORE,
        None,
        ctypes.byref(size),
    )
    if result == 0 and ctypes.GetLastError() != ERROR_INSUFFICIENT_BUFFER:
        raise ctypes.WinError()

    buffer = ctypes.create_string_buffer(size.value)
    result = kernel32.GetLogicalProcessorInformationEx(
        PROCESSOR_RELATIONSHIP_CORE,
        buffer,
        ctypes.byref(size),
    )
    if result == 0:
        raise ctypes.WinError()

    logical_count = get_logical_cpu_count()
    offset = 0
    mask = 0

    while offset < size.value:
        header = SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX.from_buffer_copy(
            buffer[offset : offset + ctypes.sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX)]
        )
        chunk = buffer[offset : offset + header.Size].raw

        if header.Relationship == PROCESSOR_RELATIONSHIP_CORE:
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                group_mask = int.from_bytes(chunk[8:16], byteorder="little", signed=False)
            else:
                group_mask = int.from_bytes(chunk[8:12], byteorder="little", signed=False)

            if group_mask:
                mask |= group_mask & -group_mask

        offset += header.Size

    return sanitize_mask(mask, logical_count)


def calculate_default_smt_mask() -> int:
    logical = get_logical_cpu_count()
    physical = psutil.cpu_count(logical=False) or logical

    try:
        core_mask = query_physical_core_mask()
        if core_mask:
            return core_mask
    except Exception as exc:
        logger.warning(f"CPU 토폴로지 조회 실패, 기본 계산으로 대체합니다: {exc}")

    if logical <= physical:
        return (1 << logical) - 1

    mask = 0
    for index in range(0, logical, 2):
        mask |= 1 << index
    return sanitize_mask(mask, logical)


def parse_mask_or_raise(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""

    total = get_logical_cpu_count()
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise ValueError("16진수 형식으로 입력해야 합니다. 예: AA 또는 0xAA") from exc

    parsed = sanitize_mask(parsed, total)
    if parsed == 0:
        raise ValueError("마스크가 0이면 어떤 코어도 사용할 수 없습니다.")

    if not mask_to_cpu_list(parsed, total):
        raise ValueError("현재 CPU 개수에 맞는 코어 비트가 없습니다.")

    return format(parsed, "X")


@dataclass
class ProcessAffinityState:
    pid: int
    name: str
    create_time: float
    original_affinity: List[int]


class ConfigManager:
    def __init__(self) -> None:
        self.config = DEFAULT_CONFIG.copy()
        self.load()

    def load(self) -> None:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                for key, value in loaded.items():
                    if key in self.config:
                        self.config[key] = value
        except Exception as exc:
            logger.warning(f"설정 파일을 읽지 못했습니다. 기본값을 사용합니다: {exc}")
        self._validate()

    def save(self) -> None:
        self._validate()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"설정 파일 저장 실패: {exc}")

    def __getitem__(self, key: str):
        return self.config[key]

    def __setitem__(self, key: str, value) -> None:
        self.config[key] = value

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    def _validate(self) -> None:
        self.config["enabled"] = bool(self.config.get("enabled", True))

        try:
            interval = int(self.config.get("check_interval", 20))
        except (TypeError, ValueError):
            interval = 20
        self.config["check_interval"] = max(5, min(300, interval))

        processes = self.config.get("game_processes", [])
        if not isinstance(processes, list):
            processes = []

        cleaned: List[str] = []
        seen = set()
        for item in processes:
            name = normalize_process_name(str(item))
            if name and name not in seen:
                seen.add(name)
                cleaned.append(name)

        if not cleaned:
            cleaned = [normalize_process_name(name) for name in DEFAULT_CONFIG["game_processes"]]
        self.config["game_processes"] = cleaned

        custom_mask = str(self.config.get("custom_mask", "") or "").strip()
        if custom_mask:
            try:
                custom_mask = parse_mask_or_raise(custom_mask)
            except ValueError:
                custom_mask = ""
        self.config["custom_mask"] = custom_mask


class GameMonitor:
    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._processed: Dict[int, ProcessAffinityState] = {}
        self._status = STATUS_WAITING
        self._active_games = 0
        self.smt_off_mask = calculate_default_smt_mask()
        self._update_mask_locked()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def active_games(self) -> int:
        with self._lock:
            return self._active_games

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self.running = True

        target = self._wmi_loop if HAS_WMI else self._poll_loop
        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

        if HAS_WMI:
            logger.info("감시 시작: WMI 이벤트 모드")
        else:
            logger.warning("WMI 모듈이 없어 폴링 모드로 동작합니다.")

    def stop(self) -> None:
        with self._lock:
            self.running = False

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.5)

    def wake_up(self) -> None:
        with self._lock:
            enabled = self.config["enabled"]
            self._update_mask_locked()

        if enabled:
            self._initial_scan()
        else:
            self.restore_all()
            with self._lock:
                self._status = STATUS_DISABLED

    def _is_running(self) -> bool:
        with self._lock:
            return self.running

    def _target_process_names(self) -> List[str]:
        with self._lock:
            return [normalize_process_name(name) for name in self.config["game_processes"]]

    def _update_mask_locked(self) -> None:
        custom_mask = self.config.get("custom_mask", "").strip()
        if custom_mask:
            try:
                parsed = int(custom_mask, 16)
                parsed = sanitize_mask(parsed)
                if not mask_to_cpu_list(parsed):
                    raise ValueError("empty affinity")
                self.smt_off_mask = parsed
                return
            except ValueError:
                logger.warning("잘못된 커스텀 마스크를 무시하고 자동 계산값을 사용합니다.")

        self.smt_off_mask = calculate_default_smt_mask()

    def _set_status_locked(self, text: str) -> None:
        self._status = text

    def _refresh_counts_locked(self) -> None:
        self._active_games = len(self._processed)
        if self._active_games:
            self._status = f"게임 {self._active_games}개 감지됨 (SMT OFF 적용 중)"
        elif self.config["enabled"]:
            self._status = STATUS_WAITING
        else:
            self._status = STATUS_DISABLED

    def restore_all(self) -> None:
        with self._lock:
            states = list(self._processed.values())

        for state in states:
            self._restore_process_affinity(state)

        with self._lock:
            self._processed.clear()
            self._refresh_counts_locked()

    def _restore_process_affinity(self, state: ProcessAffinityState) -> None:
        try:
            process = psutil.Process(state.pid)
            if not self._same_process(process, state):
                return
            process.cpu_affinity(state.original_affinity)
            logger.info(f"원래 affinity 복원: {state.name} (PID: {state.pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.warning(f"affinity 복원 실패: {state.name} (PID: {state.pid}) - {exc}")

    def _same_process(self, process: psutil.Process, state: ProcessAffinityState) -> bool:
        try:
            return abs(process.create_time() - state.create_time) < 0.01
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def _apply_smt_off(self, pid: int, process_name: str) -> None:
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
            target_affinity = mask_to_cpu_list(self.smt_off_mask)
            if not target_affinity:
                logger.warning("적용 가능한 CPU affinity가 없어 SMT OFF 적용을 건너뜁니다.")
                return

            with self._lock:
                existing = self._processed.get(pid)
                if existing and abs(existing.create_time - create_time) < 0.01:
                    return

            original_affinity = process.cpu_affinity()
            if not original_affinity:
                return

            new_affinity = [cpu for cpu in original_affinity if cpu in target_affinity]
            if not new_affinity:
                logger.warning(
                    f"현재 affinity와 SMT OFF 마스크가 겹치지 않아 적용을 건너뜁니다: {process_name} (PID: {pid})"
                )
                return

            process.cpu_affinity(new_affinity)
            state = ProcessAffinityState(
                pid=pid,
                name=process_name,
                create_time=create_time,
                original_affinity=original_affinity,
            )

            with self._lock:
                self._processed[pid] = state
                self._refresh_counts_locked()

            logger.info(
                f"SMT OFF 적용: {process_name} (PID: {pid}) | before={original_affinity} after={new_affinity}"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.warning(f"SMT OFF 적용 실패: {process_name} (PID: {pid}) - {exc}")

    def _remove_stale_processes(self) -> None:
        with self._lock:
            states = list(self._processed.values())

        stale_pids: List[int] = []
        for state in states:
            try:
                process = psutil.Process(state.pid)
                if not self._same_process(process, state):
                    stale_pids.append(state.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                stale_pids.append(state.pid)

        if not stale_pids:
            return

        with self._lock:
            for pid in stale_pids:
                removed = self._processed.pop(pid, None)
                if removed:
                    logger.info(f"종료 감지: {removed.name} (PID: {pid})")
            self._refresh_counts_locked()

    def _initial_scan(self) -> None:
        with self._lock:
            self._update_mask_locked()
            enabled = self.config["enabled"]

        if not enabled:
            with self._lock:
                self._set_status_locked(STATUS_DISABLED)
            return

        target_names = set(self._target_process_names())
        found = 0

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                process_name = proc.info["name"]
                if not process_name:
                    continue
                if normalize_process_name(process_name) in target_names:
                    self._apply_smt_off(proc.info["pid"], process_name)
                    found += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self._remove_stale_processes()

        with self._lock:
            if found == 0 and not self._processed:
                self._set_status_locked(STATUS_WAITING)

    def _handle_process_event(self, event_type: str, pid: int, process_name: str) -> None:
        normalized_name = normalize_process_name(process_name)
        if normalized_name not in set(self._target_process_names()):
            return

        if event_type == "Win32_ProcessStartTrace":
            logger.info(f"게임 시작 감지: {process_name} (PID: {pid})")
            self._apply_smt_off(pid, process_name)
            return

        if event_type == "Win32_ProcessStopTrace":
            with self._lock:
                removed = self._processed.pop(pid, None)
                if removed:
                    logger.info(f"게임 종료 감지: {removed.name} (PID: {pid})")
                self._refresh_counts_locked()

    def _wmi_loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            services = locator.ConnectServer(".", "root\\cimv2")
            events = services.ExecNotificationQuery("SELECT * FROM Win32_ProcessTrace")

            if self.config["enabled"]:
                self._initial_scan()
            else:
                with self._lock:
                    self._set_status_locked(STATUS_DISABLED)

            while self._is_running():
                try:
                    event = events.NextEvent(1000)
                except pythoncom.com_error:
                    continue
                except Exception as exc:
                    logger.error(f"WMI 이벤트 처리 오류: {exc}")
                    time.sleep(1)
                    continue

                if not self._is_running():
                    break

                with self._lock:
                    enabled = self.config["enabled"]

                if not enabled:
                    continue

                try:
                    self._handle_process_event(
                        event.Path_.Class,
                        int(event.ProcessID),
                        event.ProcessName,
                    )
                except Exception as exc:
                    logger.warning(f"이벤트 처리 실패: {exc}")
        finally:
            self.restore_all()
            pythoncom.CoUninitialize()

    def _poll_loop(self) -> None:
        if self.config["enabled"]:
            self._initial_scan()
        else:
            with self._lock:
                self._set_status_locked(STATUS_DISABLED)

        while self._is_running():
            try:
                with self._lock:
                    enabled = self.config["enabled"]
                if enabled:
                    self._initial_scan()
                else:
                    self.restore_all()
                    with self._lock:
                        self._set_status_locked(STATUS_DISABLED)
            except Exception as exc:
                logger.error(f"폴링 모니터 오류: {exc}")

            time.sleep(max(5, int(self.config.get("check_interval", 20))))


class StartupManager:
    @staticmethod
    def _build_tr_command() -> str:
        if getattr(sys, "frozen", False):
            return subprocess.list2cmdline([sys.executable, "--tray"])

        python_exe = sys.executable
        if python_exe.lower().endswith("python.exe"):
            candidate = python_exe[:-10] + "pythonw.exe"
            if os.path.exists(candidate):
                python_exe = candidate

        return subprocess.list2cmdline([python_exe, os.path.abspath(__file__), "--tray"])

    @staticmethod
    def register():
        command = [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            StartupManager._build_tr_command(),
            "/SC",
            "ONLOGON",
            "/RL",
            "HIGHEST",
            "/F",
            "/DELAY",
            "0000:30",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("시작 프로그램 등록 완료")
                return True, "시작 프로그램 등록이 완료되었습니다.\n로그인 후 트레이에서 자동 실행됩니다."
            return False, f"등록 실패: {result.stderr.strip() or result.stdout.strip()}"
        except Exception as exc:
            return False, f"오류: {exc}"

    @staticmethod
    def unregister():
        command = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("시작 프로그램 해제 완료")
                return True, "시작 프로그램에서 해제되었습니다."
            return False, f"해제 실패: {result.stderr.strip() or result.stdout.strip()}"
        except Exception as exc:
            return False, f"오류: {exc}"

    @staticmethod
    def is_registered() -> bool:
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception:
            return False


def create_icon(active: bool = True) -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if active:
        draw.rounded_rectangle(
            [4, 4, 60, 60],
            radius=8,
            fill=(30, 144, 255),
            outline=(255, 255, 255),
            width=2,
        )
        draw.rectangle([18, 18, 46, 46], fill=(20, 100, 200))
        draw.rectangle([22, 22, 42, 42], fill=(50, 170, 255))
        for y in [12, 28, 44]:
            draw.rectangle([8, y, 16, y + 4], fill=(255, 255, 255))
            draw.rectangle([48, y, 56, y + 4], fill=(255, 255, 255))
    else:
        draw.rounded_rectangle(
            [4, 4, 60, 60],
            radius=8,
            fill=(100, 100, 100),
            outline=(180, 180, 180),
            width=2,
        )
        draw.rectangle([18, 18, 46, 46], fill=(80, 80, 80))
        draw.rectangle([22, 22, 42, 42], fill=(120, 120, 120))
        draw.line([18, 18, 46, 46], fill=(255, 80, 80), width=3)
        draw.line([46, 18, 18, 46], fill=(255, 80, 80), width=3)
    return image


class SMTManagerApp:
    def __init__(self) -> None:
        self.config = ConfigManager()
        self.monitor = GameMonitor(self.config)
        self.tray_icon: Optional[pystray.Icon] = None
        self.tray_running = False

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} - {APP_AUTHOR}")
        self.root.geometry("560x700")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        try:
            buffer = io.BytesIO()
            create_icon(True).save(buffer, format="PNG")
            icon_data = base64.b64encode(buffer.getvalue())
            self._icon_photo = tk.PhotoImage(data=icon_data)
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        self._build_ui()
        self.monitor.start()
        self._start_tray()

        if "--tray" in sys.argv:
            logger.info("트레이 모드로 시작합니다.")
            self.root.withdraw()

    def run(self) -> None:
        logger.info(f"{APP_NAME} v{APP_VERSION} 시작")
        self.root.mainloop()

    def _build_ui(self) -> None:
        window = self.root
        style = ttk.Style(window)
        style.theme_use("clam")

        bg = "#1a1a2e"
        fg = "#e0e0e0"
        accent = "#1e90ff"
        card_bg = "#16213e"
        btn_bg = "#0f3460"

        style.configure("Card.TLabel", background=card_bg, foreground=fg, font=("Segoe UI", 10))

        header = tk.Frame(window, bg=bg)
        header.pack(fill="x", padx=20, pady=(15, 5))
        tk.Label(
            header,
            text=APP_NAME,
            bg=bg,
            fg="#ffffff",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text=f"{APP_AUTHOR} | v{APP_VERSION}",
            bg=bg,
            fg="#8888aa",
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        status_frame = tk.Frame(window, bg=bg)
        status_frame.pack(fill="x", padx=20, pady=(5, 10))
        self.status_label = tk.Label(
            status_frame,
            text=f"상태: {self.monitor.status}",
            bg=bg,
            fg="#00ff88",
            font=("Segoe UI", 10),
        )
        self.status_label.pack(anchor="w")

        tray_frame = tk.Frame(window, bg=bg)
        tray_frame.pack(fill="x", padx=20, pady=(0, 10))
        self.bg_button = tk.Button(
            tray_frame,
            text="백그라운드 실행 (트레이로 최소화)",
            bg="#8b5cf6",
            fg="white",
            font=("Segoe UI", 13, "bold"),
            bd=0,
            cursor="hand2",
            pady=10,
            activebackground="#7c3aed",
            activeforeground="white",
            command=self._minimize_to_tray,
        )
        self.bg_button.pack(fill="x", ipady=4)
        tk.Label(
            tray_frame,
            text="창을 닫으면 트레이로 숨겨집니다. 완전 종료는 트레이 메뉴에서 하세요.",
            bg=bg,
            fg="#666688",
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(4, 0))

        smt_card = tk.LabelFrame(
            window,
            text="  SMT 제어  ",
            bg=card_bg,
            fg=accent,
            font=("Segoe UI", 11, "bold"),
            bd=1,
            relief="groove",
            padx=15,
            pady=10,
        )
        smt_card.pack(fill="x", padx=20, pady=5)

        self.smt_var = tk.BooleanVar(value=self.config["enabled"])
        toggle_frame = tk.Frame(smt_card, bg=card_bg)
        toggle_frame.pack(fill="x")
        ttk.Label(
            toggle_frame,
            text="게임 실행 시 자동 SMT OFF:",
            style="Card.TLabel",
        ).pack(side="left")
        self.toggle_btn = tk.Button(
            toggle_frame,
            text="ON" if self.smt_var.get() else "OFF",
            bg="#27ae60" if self.smt_var.get() else "#e74c3c",
            fg="white",
            font=("Segoe UI", 11, "bold"),
            width=8,
            bd=0,
            cursor="hand2",
            command=self._toggle_smt,
        )
        self.toggle_btn.pack(side="right", padx=5)

        self.mask_info_label = ttk.Label(
            smt_card,
            text=self._mask_info_text(),
            style="Card.TLabel",
        )
        self.mask_info_label.pack(anchor="w", pady=(8, 0))
        self._update_status()

        mask_frame = tk.Frame(smt_card, bg=card_bg)
        mask_frame.pack(fill="x", pady=(5, 0))
        ttk.Label(mask_frame, text="커스텀 마스크 (Hex):", style="Card.TLabel").pack(side="left")
        self.mask_entry = tk.Entry(
            mask_frame,
            bg="#0a0a1a",
            fg=fg,
            insertbackground=fg,
            font=("Consolas", 10),
            bd=0,
            highlightthickness=1,
            highlightcolor=accent,
            width=12,
        )
        self.mask_entry.pack(side="left", padx=5)
        self.mask_entry.insert(0, self.config.get("custom_mask", ""))
        tk.Button(
            mask_frame,
            text="마스크 적용",
            bg=btn_bg,
            fg="white",
            font=("Segoe UI", 8),
            bd=0,
            command=self._apply_mask,
        ).pack(side="left", padx=2)
        ttk.Label(
            mask_frame,
            text="비워두면 자동 계산",
            style="Card.TLabel",
            foreground="#8888aa",
        ).pack(side="left", padx=5)

        info_card = tk.LabelFrame(
            window,
            text="  감시 방식  ",
            bg=card_bg,
            fg=accent,
            font=("Segoe UI", 11, "bold"),
            bd=1,
            relief="groove",
            padx=15,
            pady=10,
        )
        info_card.pack(fill="x", padx=20, pady=5)
        tk.Label(
            info_card,
            text="WMI 이벤트 기반으로 게임 시작/종료를 즉시 감지합니다.",
            bg=card_bg,
            fg="#00ff88",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            info_card,
            text="WMI가 없으면 폴링 모드로 자동 전환됩니다.",
            bg=card_bg,
            fg="#8888aa",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        process_card = tk.LabelFrame(
            window,
            text="  게임 프로세스 목록  ",
            bg=card_bg,
            fg=accent,
            font=("Segoe UI", 11, "bold"),
            bd=1,
            relief="groove",
            padx=15,
            pady=10,
        )
        process_card.pack(fill="x", padx=20, pady=5)
        self.process_listbox = tk.Listbox(
            process_card,
            bg="#0a0a1a",
            fg=fg,
            selectbackground=accent,
            font=("Consolas", 10),
            height=4,
            bd=0,
            highlightthickness=1,
            highlightcolor=accent,
        )
        self.process_listbox.pack(fill="x")
        for process_name in self.config["game_processes"]:
            self.process_listbox.insert("end", process_name)

        process_buttons = tk.Frame(process_card, bg=card_bg)
        process_buttons.pack(fill="x", pady=(8, 0))
        self.add_entry = tk.Entry(
            process_buttons,
            bg="#0a0a1a",
            fg=fg,
            insertbackground=fg,
            font=("Consolas", 10),
            bd=0,
            highlightthickness=1,
            highlightcolor=accent,
        )
        self.add_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.add_entry.insert(0, "프로세스 이름 입력...")
        self.add_entry.bind("<FocusIn>", lambda _event: self._clear_placeholder())
        tk.Button(
            process_buttons,
            text="추가",
            bg="#27ae60",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            bd=0,
            padx=8,
            command=self._add_process,
        ).pack(side="left", padx=2)
        tk.Button(
            process_buttons,
            text="삭제",
            bg="#e74c3c",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            bd=0,
            padx=8,
            command=self._delete_process,
        ).pack(side="left")

        startup_card = tk.LabelFrame(
            window,
            text="  시작 프로그램  ",
            bg=card_bg,
            fg=accent,
            font=("Segoe UI", 11, "bold"),
            bd=1,
            relief="groove",
            padx=15,
            pady=10,
        )
        startup_card.pack(fill="x", padx=20, pady=5)
        startup_row = tk.Frame(startup_card, bg=card_bg)
        startup_row.pack(fill="x")
        is_registered = StartupManager.is_registered()
        self.startup_status = ttk.Label(
            startup_row,
            text="등록됨" if is_registered else "미등록",
            style="Card.TLabel",
        )
        self.startup_status.pack(side="left")
        tk.Button(
            startup_row,
            text="해제",
            bg="#e74c3c",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            bd=0,
            padx=10,
            command=self._unregister_startup,
        ).pack(side="right", padx=2)
        tk.Button(
            startup_row,
            text="등록",
            bg="#27ae60",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            bd=0,
            padx=10,
            command=self._register_startup,
        ).pack(side="right", padx=2)

        bottom = tk.Frame(window, bg=bg)
        bottom.pack(fill="x", padx=20, pady=(10, 15))
        tk.Button(
            bottom,
            text="로그 보기",
            bg=btn_bg,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            bd=0,
            padx=15,
            pady=6,
            command=self._show_log,
        ).pack(side="left")
        tk.Button(
            bottom,
            text="설정 저장",
            bg=accent,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            bd=0,
            padx=15,
            pady=6,
            command=self._save,
        ).pack(side="right")

    def _mask_info_text(self) -> str:
        physical = psutil.cpu_count(logical=False) or get_logical_cpu_count()
        logical = get_logical_cpu_count()
        return f"CPU: {physical}코어 / {logical}스레드 | 적용 마스크: {hex(self.monitor.smt_off_mask)}"

    def _update_status(self) -> None:
        if self.root.winfo_exists():
            self.status_label.config(text=f"상태: {self.monitor.status}")
            if hasattr(self, "mask_info_label"):
                self.mask_info_label.config(text=self._mask_info_text())
            self.root.after(1000, self._update_status)

    def _toggle_smt(self) -> None:
        value = not self.smt_var.get()
        self.smt_var.set(value)
        self.config["enabled"] = value
        self.config.save()
        self.toggle_btn.config(
            text="ON" if value else "OFF",
            bg="#27ae60" if value else "#e74c3c",
        )
        if self.tray_icon:
            self.tray_icon.icon = create_icon(value)
        logger.info(f"자동 SMT OFF {'활성화' if value else '비활성화'}")
        self.monitor.wake_up()

    def _apply_mask(self) -> None:
        try:
            normalized_mask = parse_mask_or_raise(self.mask_entry.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.mask_entry.delete(0, "end")
        self.mask_entry.insert(0, normalized_mask)
        self.config["custom_mask"] = normalized_mask
        self.config.save()
        self.monitor.wake_up()
        if normalized_mask:
            message = f"마스크가 적용되었습니다.\n현재 값: 0x{normalized_mask}"
        else:
            message = "자동 계산 마스크가 적용되었습니다."
        messagebox.showinfo(APP_NAME, message)

    def _clear_placeholder(self) -> None:
        if self.add_entry.get() == "프로세스 이름 입력...":
            self.add_entry.delete(0, "end")

    def _add_process(self) -> None:
        name = normalize_process_name(self.add_entry.get())
        if not name:
            return
        if name in self.config["game_processes"]:
            messagebox.showinfo(APP_NAME, "이미 등록된 프로세스입니다.")
            return
        self.config["game_processes"].append(name)
        self.process_listbox.insert("end", name)
        self.add_entry.delete(0, "end")
        logger.info(f"프로세스 추가: {name}")

    def _delete_process(self) -> None:
        selected = self.process_listbox.curselection()
        if not selected:
            return
        name = self.process_listbox.get(selected[0])
        self.process_listbox.delete(selected[0])
        if name in self.config["game_processes"]:
            self.config["game_processes"].remove(name)
        logger.info(f"프로세스 삭제: {name}")

    def _register_startup(self) -> None:
        success, message = StartupManager.register()
        self.startup_status.config(text="등록됨" if success else "미등록")
        (messagebox.showinfo if success else messagebox.showerror)(APP_NAME, message)

    def _unregister_startup(self) -> None:
        success, message = StartupManager.unregister()
        self.startup_status.config(text="미등록" if success else "등록됨")
        (messagebox.showinfo if success else messagebox.showerror)(APP_NAME, message)

    def _show_log(self) -> None:
        log_window = tk.Toplevel(self.root)
        log_window.title(f"{APP_NAME} - 로그")
        log_window.geometry("700x420")
        log_window.configure(bg="#1a1a2e")

        text = scrolledtext.ScrolledText(
            log_window,
            bg="#0a0a1a",
            fg="#e0e0e0",
            font=("Consolas", 9),
            insertbackground="#e0e0e0",
            wrap="word",
        )
        text.pack(fill="both", expand=True, padx=10, pady=10)

        try:
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, "r", encoding="utf-8") as file:
                    for line in file.readlines()[-200:]:
                        text.insert("end", line)
            else:
                text.insert("end", "로그가 아직 없습니다.")
        except Exception as exc:
            text.insert("end", f"로그 읽기 실패: {exc}")

        text.config(state="disabled")
        text.see("end")

    def _save(self) -> None:
        try:
            self.config["custom_mask"] = parse_mask_or_raise(self.mask_entry.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.config.save()
        self.monitor.wake_up()
        logger.info("설정 저장 완료")
        messagebox.showinfo(APP_NAME, "설정이 저장되었습니다.")

    def _minimize_to_tray(self) -> None:
        self.config["custom_mask"] = self.mask_entry.get().strip()
        self.config.save()
        self.root.withdraw()

    def _start_tray(self) -> None:
        if self.tray_running:
            return

        menu = pystray.Menu(
            pystray.MenuItem(f"{APP_NAME} v{APP_VERSION}", None, enabled=False),
            pystray.MenuItem(f"by {APP_AUTHOR}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("설정 창 열기", self._show_from_tray),
            pystray.MenuItem("프로그램 폴더 열기", lambda: os.startfile(BASE_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("완전 종료", self._quit_app),
        )
        self.tray_icon = pystray.Icon(
            APP_NAME,
            create_icon(self.config["enabled"]),
            f"{APP_NAME} - {APP_AUTHOR}",
            menu,
        )
        self.tray_running = True
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _show_from_tray(self, icon=None, item=None) -> None:
        self.root.after(0, self._restore_window)

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_app(self, icon=None, item=None) -> None:
        logger.info(f"{APP_NAME} 종료")
        self.monitor.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)


def relaunch_with_pythonw_if_needed() -> None:
    if getattr(sys, "frozen", False):
        return

    python_exe = sys.executable
    if not python_exe.lower().endswith("python.exe"):
        return
    if "--nowindow" in sys.argv:
        return

    pythonw_exe = python_exe[:-10] + "pythonw.exe"
    if not os.path.exists(pythonw_exe):
        return

    args = [pythonw_exe, os.path.abspath(__file__)] + sys.argv[1:] + ["--nowindow"]
    subprocess.Popen(
        args,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    sys.exit()


def already_running() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() == 183


def main() -> None:
    relaunch_with_pythonw_if_needed()

    if already_running():
        if "--tray" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"{APP_NAME}가 이미 실행 중입니다.\n트레이 아이콘을 확인해 주세요.",
                APP_NAME,
                0x40,
            )
        sys.exit()

    if not is_admin():
        run_as_admin()
        return

    app = SMTManagerApp()
    app.run()


if __name__ == "__main__":
    main()
