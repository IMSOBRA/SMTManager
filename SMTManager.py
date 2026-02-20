"""
╔══════════════════════════════════════════╗
║   SMT Manager v2.0                       ║
║   Vibe Coding BY SOBRA                   ║
╚══════════════════════════════════════════╝

마비노기 / 마비노기 영웅전 자동 SMT 관리 프로그램
WMI 이벤트 기반(Event-driven) 최적화 버전
설정 창에서 세팅 후 백그라운드 실행 → 트레이에서 상주
"""

import os
import sys
import json
import ctypes
import threading
import time
import subprocess
import logging

import psutil
import pystray
import pythoncom
import win32com.client
from PIL import Image, ImageDraw

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# ═══════════════════════════════════════════
# 상수
# ═══════════════════════════════════════════
APP_NAME = "SMT Manager"
APP_VERSION = "2.0"
APP_AUTHOR = "Vibe Coding BY SOBRA"
TASK_NAME = "SMTManager_SOBRA"

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "smt_config.json")
LOG_PATH = os.path.join(BASE_DIR, "smt_log.txt")

DEFAULT_CONFIG = {
    "enabled": True,
    "game_processes": ["Client", "heroes", "heroes_x64"],
    "custom_mask": ""  # 커스텀 마스크 지원
}

# ═══════════════════════════════════════════
# 로깅
# ═══════════════════════════════════════════
logger = logging.getLogger("SMTManager")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(file_handler)


# ═══════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        params = "--tray" if "--tray" in sys.argv else ""
    else:
        exe = sys.executable
        args_str = f'"{os.path.abspath(__file__)}"'
        if "--tray" in sys.argv:
            args_str += " --tray"
        params = args_str
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    sys.exit()

def calculate_smt_off_mask():
    logical = psutil.cpu_count(logical=True)
    physical = psutil.cpu_count(logical=False)
    if logical == physical:
        return (1 << logical) - 1
    mask = 0
    for i in range(0, logical, 2):
        mask |= (1 << i)
    return mask


# ═══════════════════════════════════════════
# 설정 관리
# ═══════════════════════════════════════════
class ConfigManager:
    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # 이전 버전 config와 호환되게 업데이트, 불필요한 값은 무시될 수 있음
                    for k, v in loaded.items():
                        if k in self.config:
                            self.config[k] = v
        except Exception:
            pass

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def __getitem__(self, key):
        return self.config[key]

    def __setitem__(self, key, value):
        self.config[key] = value

    def get(self, key, default=None):
        return self.config.get(key, default)


# ═══════════════════════════════════════════
# 게임 모니터 (WMI 이벤트 기반)
# ═══════════════════════════════════════════
class GameMonitor:
    def __init__(self, config):
        self.config = config
        self.processed_pids = {}
        self.running = False
        self.thread = None
        
        self.smt_off_mask = calculate_smt_off_mask()
        self._update_mask()
        
        self._status = "대기 중"
        self._active_games = 0

    @property
    def status(self):
        return self._status

    def _update_mask(self):
        custom = self.config.get("custom_mask", "").strip()
        if custom:
            try:
                self.smt_off_mask = int(custom, 16)
            except ValueError:
                self.smt_off_mask = calculate_smt_off_mask()
        else:
            self.smt_off_mask = calculate_smt_off_mask()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._wmi_loop, daemon=True)
        self.thread.start()
        logger.info("모니터링 시작 (WMI 이벤트 기반) | Vibe Coding BY SOBRA")

    def wake_up(self):
        """설정 변경 등 즉각 반영을 위해 초기화/재검사 수행"""
        if self.config["enabled"]:
            self._initial_scan()
        else:
            self._status = "비활성화됨"
            self.restore_all()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.5)

    def restore_all(self):
        """저장된 모든 PID를 다시 전체 코어 사용으로 복구"""
        if not self.processed_pids:
            return
            
        total_cpus = list(range(psutil.cpu_count(logical=True)))
        
        for pid, name in list(self.processed_pids.items()):
            if psutil.pid_exists(pid):
                try:
                    p = psutil.Process(pid)
                    p.cpu_affinity(total_cpus)
                    logger.info(f"🔄 SMT 복구: {name} (PID: {pid})")
                except (psutil.AccessDenied, psutil.NoSuchProcess) as e:
                    logger.warning(f"❌ 복구 실패: {name} (PID: {pid}) - {e}")
        self.processed_pids.clear()
        self._active_games = 0
        if self.config["enabled"]:
            self._status = "게임 대기 중..."

    def _apply_smt_off(self, pid, pname):
        if pid not in self.processed_pids:
            try:
                p = psutil.Process(pid)
                cpus = [i for i in range(psutil.cpu_count(logical=True))
                        if self.smt_off_mask & (1 << i)]
                p.cpu_affinity(cpus)
                self.processed_pids[pid] = pname
                self._active_games = len(self.processed_pids)
                self._status = f"게임 {self._active_games}개 감지됨 (SMT OFF)"
                logger.info(f"✅ SMT OFF 적용: {pname} (PID: {pid})")
            except (psutil.AccessDenied, psutil.NoSuchProcess) as e:
                logger.warning(f"❌ 적용 실패: {pname} (PID: {pid}) - {e}")

    def _initial_scan(self):
        self._update_mask()
        names = [n.lower() for n in self.config["game_processes"]]
        found = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info["name"]
                if pname is None:
                    continue
                if os.path.splitext(pname)[0].lower() in names:
                    self._apply_smt_off(proc.info["pid"], pname)
                    found += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if found == 0 and len(self.processed_pids) == 0:
            self._status = "게임 대기 중..."

    def _wmi_loop(self):
        # COM 객체 초기화
        pythoncom.CoInitialize()
        try:
            wmi = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            services = wmi.ConnectServer(".", "root\\cimv2")
            
            # 프로세스 시작 및 종료 이벤트를 이벤트 기반으로 가져옵니다 (0% 폴링 오버헤드).
            query = "SELECT * FROM Win32_ProcessTrace"
            events = services.ExecNotificationQuery(query)

            if self.config["enabled"]:
                self._initial_scan()
            else:
                self._status = "비활성화됨"

            while self.running:
                try:
                    # 블로킹 없는 검사를 위해 2초(2000ms) 대기 후 반복
                    event = events.NextEvent(2000)
                except pythoncom.com_error:
                    # Timeout 발생
                    continue
                except Exception as e:
                    logger.error(f"WMI 이벤트 처리 오류: {e}")
                    time.sleep(2)
                    continue

                if not self.running:
                    break
                if not self.config["enabled"]:
                    continue

                try:
                    ev_type = event.Path_.Class
                    pid = int(event.ProcessID)
                    pname = event.ProcessName

                    names = [n.lower() for n in self.config["game_processes"]]
                    name_base = os.path.splitext(pname)[0].lower()

                    if name_base in names:
                        if ev_type == "Win32_ProcessStartTrace":
                            logger.info(f"⚡ 생성 감지(WMI): {pname} (PID: {pid})")
                            self._apply_smt_off(pid, pname)
                        elif ev_type == "Win32_ProcessStopTrace":
                            if pid in self.processed_pids:
                                del_name = self.processed_pids.pop(pid)
                                logger.info(f"🔄 종료 감지(WMI): {del_name} (PID: {pid})")
                                self._active_games = len(self.processed_pids)
                                if self._active_games > 0:
                                    self._status = f"게임 {self._active_games}개 감지됨 (SMT OFF)"
                                else:
                                    self._status = "게임 대기 중..."
                except Exception as e:
                    pass

        finally:
            self.restore_all()
            pythoncom.CoUninitialize()


# ═══════════════════════════════════════════
# 시작 프로그램 관리
# ═══════════════════════════════════════════
class StartupManager:
    @staticmethod
    def register():
        if getattr(sys, 'frozen', False):
            tr_cmd = f'\\"{sys.executable}\\" --tray'
        else:
            python_exe = sys.executable
            if python_exe.lower().endswith("python.exe"):
                python_exe = python_exe[:-10] + "pythonw.exe"
            tr_cmd = f'\\"{python_exe}\\" \\"{os.path.abspath(__file__)}\\" --tray'

        cmd = (f'schtasks /Create /TN "{TASK_NAME}" '
               f'/TR "{tr_cmd}" /SC ONLOGON /RL HIGHEST /F /DELAY 0000:30')
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if r.returncode == 0:
                logger.info("🚀 시작 프로그램 등록 완료! (--tray 인자 추가됨)")
                return True, "시작 프로그램에 등록되었습니다!\n(재부팅 시 트레이 아이콘으로 자동 실행됩니다.)"
            return False, f"등록 실패: {r.stderr}"
        except Exception as e:
            return False, f"오류: {e}"

    @staticmethod
    def unregister():
        cmd = f'schtasks /Delete /TN "{TASK_NAME}" /F'
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if r.returncode == 0:
                logger.info("🗑️ 시작 프로그램 해제!")
                return True, "시작 프로그램에서 해제되었습니다!"
            return False, f"해제 실패: {r.stderr}"
        except Exception as e:
            return False, f"오류: {e}"

    @staticmethod
    def is_registered():
        try:
            r = subprocess.run(f'schtasks /Query /TN "{TASK_NAME}" 2>nul',
                               shell=True, capture_output=True, text=True)
            return r.returncode == 0
        except:
            return False


# ═══════════════════════════════════════════
# 트레이 아이콘 이미지
# ═══════════════════════════════════════════
def create_icon(active=True):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if active:
        d.rounded_rectangle([4, 4, 60, 60], radius=8, fill=(30, 144, 255), outline=(255, 255, 255), width=2)
        d.rectangle([18, 18, 46, 46], fill=(20, 100, 200))
        d.rectangle([22, 22, 42, 42], fill=(50, 170, 255))
        for y in [12, 28, 44]:
            d.rectangle([8, y, 16, y+4], fill=(255, 255, 255))
            d.rectangle([48, y, 56, y+4], fill=(255, 255, 255))
    else:
        d.rounded_rectangle([4, 4, 60, 60], radius=8, fill=(100, 100, 100), outline=(180, 180, 180), width=2)
        d.rectangle([18, 18, 46, 46], fill=(80, 80, 80))
        d.rectangle([22, 22, 42, 42], fill=(120, 120, 120))
        d.line([18, 18, 46, 46], fill=(255, 80, 80), width=3)
        d.line([46, 18, 18, 46], fill=(255, 80, 80), width=3)
    return img


# ═══════════════════════════════════════════
# 메인 앱 (tkinter 메인스레드 + pystray 백그라운드)
# ═══════════════════════════════════════════
class SMTManagerApp:
    def __init__(self):
        self.config = ConfigManager()
        self.monitor = GameMonitor(self.config)
        self.tray_icon = None
        self.tray_running = False

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} - {APP_AUTHOR}")
        # 세로 길이를 체크 간격이 빠졌으므로 살짝 줄입니다
        self.root.geometry("520x680")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        try:
            import io, base64
            buf = io.BytesIO()
            create_icon(True).save(buf, format="PNG")
            self._icon_photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
            self.root.iconphoto(True, self._icon_photo)
        except:
            pass

        self._build_ui()
        self.monitor.start()
        self._start_tray()

        if "--tray" in sys.argv:
            logger.info("시작 프로그램(또는 --tray 인자)에 의해 백그라운드로 조용히 실행됨.")
            self.root.withdraw()

    def run(self):
        logger.info(f"═══ {APP_NAME} v{APP_VERSION} 시작 | {APP_AUTHOR} ═══")
        self.root.mainloop()

    def _build_ui(self):
        w = self.root
        style = ttk.Style(w)
        style.theme_use("clam")

        bg = "#1a1a2e"
        fg = "#e0e0e0"
        accent = "#1e90ff"
        card_bg = "#16213e"
        btn_bg = "#0f3460"

        style.configure("Card.TLabel", background=card_bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=card_bg, foreground=accent, font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", background=bg, foreground="#00ff88", font=("Segoe UI", 10))

        # ─── 헤더 ───
        header = tk.Frame(w, bg=bg)
        header.pack(fill="x", padx=20, pady=(15, 5))
        tk.Label(header, text=f"⚡ {APP_NAME}", bg=bg, fg="#ffffff",
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(header, text=f"{APP_AUTHOR} | v{APP_VERSION}", bg=bg, fg="#8888aa",
                 font=("Segoe UI", 9)).pack(anchor="w")

        # ─── 상태 ───
        sf = tk.Frame(w, bg=bg)
        sf.pack(fill="x", padx=20, pady=(5, 10))
        self.status_label = tk.Label(sf, text=f"📡 {self.monitor.status}",
                                      bg=bg, fg="#00ff88", font=("Segoe UI", 10))
        self.status_label.pack(anchor="w")
        self._update_status()

        # ─── 🚀 백그라운드 실행 버튼 ───
        bg_btn_frame = tk.Frame(w, bg=bg)
        bg_btn_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.bg_button = tk.Button(
            bg_btn_frame,
            text="🚀 백그라운드 실행 (트레이로 숨기기)",
            bg="#8b5cf6", fg="white",
            font=("Segoe UI", 13, "bold"),
            bd=0, cursor="hand2", pady=10,
            activebackground="#7c3aed", activeforeground="white",
            command=self._minimize_to_tray
        )
        self.bg_button.pack(fill="x", ipady=4)

        tk.Label(bg_btn_frame, text="💡 창을 닫아도 트레이에 상주합니다. 완전 종료는 트레이에서 해주세요!",
                 bg=bg, fg="#666688", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        # ─── SMT 토글 및 코어 마스크 ───
        card1 = tk.LabelFrame(w, text="  SMT 제어  ", bg=card_bg, fg=accent,
                              font=("Segoe UI", 11, "bold"), bd=1, relief="groove", padx=15, pady=10)
        card1.pack(fill="x", padx=20, pady=5)

        self.smt_var = tk.BooleanVar(value=self.config["enabled"])
        tf = tk.Frame(card1, bg=card_bg)
        tf.pack(fill="x")
        ttk.Label(tf, text="게임 감지 시 자동 SMT OFF:", style="Card.TLabel").pack(side="left")
        self.toggle_btn = tk.Button(tf,
                                     text="ON ✅" if self.smt_var.get() else "OFF ❌",
                                     bg="#27ae60" if self.smt_var.get() else "#e74c3c",
                                     fg="white", font=("Segoe UI", 11, "bold"),
                                     width=8, bd=0, cursor="hand2", command=self._toggle_smt)
        self.toggle_btn.pack(side="right", padx=5)

        cpu_info = f"CPU: {psutil.cpu_count(logical=False)}코어 / {psutil.cpu_count(logical=True)}스레드"
        self.mask_info_label = ttk.Label(card1, text=f"ℹ️ {cpu_info} | SMT OFF 적용 마스크: {hex(self.monitor.smt_off_mask)}", style="Card.TLabel")
        self.mask_info_label.pack(anchor="w", pady=(8, 0))
        
        mf = tk.Frame(card1, bg=card_bg)
        mf.pack(fill="x", pady=(5, 0))
        ttk.Label(mf, text="커스텀 마스크(Hex):", style="Card.TLabel").pack(side="left")
        
        self.mask_entry = tk.Entry(mf, bg="#0a0a1a", fg=fg, insertbackground=fg,
                                   font=("Consolas", 10), bd=0, highlightthickness=1, highlightcolor=accent, width=12)
        self.mask_entry.pack(side="left", padx=5)
        self.mask_entry.insert(0, self.config.get("custom_mask", ""))
        
        tk.Button(mf, text="수동 적용", bg=btn_bg, fg="white", font=("Segoe UI", 8), bd=0, 
                  command=self._apply_mask).pack(side="left", padx=2)
        ttk.Label(mf, text="(비워두면 자동 계산)", style="Card.TLabel", foreground="#8888aa").pack(side="left", padx=5)

        # ─── 감지 방식 알림 (WMI 이벤트 기반) ───
        card2 = tk.LabelFrame(w, text="  감지 시스템  ", bg=card_bg, fg=accent,
                              font=("Segoe UI", 11, "bold"), bd=1, relief="groove", padx=15, pady=10)
        card2.pack(fill="x", padx=20, pady=5)

        tk.Label(card2, text="⚡ WMI 이벤트 트리거 즉시 감지 활성화 (0% 점유율)",
                 bg=card_bg, fg="#00ff88", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(card2, text="프로세스 켜짐/꺼짐을 OS 이벤트 수준에서 딜레이 없이 감지합니다.",
                 bg=card_bg, fg="#8888aa", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        # ─── 게임 프로세스 ───
        card3 = tk.LabelFrame(w, text="  게임 프로세스 목록  ", bg=card_bg, fg=accent,
                              font=("Segoe UI", 11, "bold"), bd=1, relief="groove", padx=15, pady=10)
        card3.pack(fill="x", padx=20, pady=5)

        self.process_listbox = tk.Listbox(card3, bg="#0a0a1a", fg=fg, selectbackground=accent,
                                           font=("Consolas", 10), height=3, bd=0,
                                           highlightthickness=1, highlightcolor=accent)
        self.process_listbox.pack(fill="x")
        for p in self.config["game_processes"]:
            self.process_listbox.insert("end", p)

        bf = tk.Frame(card3, bg=card_bg)
        bf.pack(fill="x", pady=(8, 0))
        self.add_entry = tk.Entry(bf, bg="#0a0a1a", fg=fg, insertbackground=fg,
                                   font=("Consolas", 10), bd=0, highlightthickness=1, highlightcolor=accent)
        self.add_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.add_entry.insert(0, "프로세스 이름 입력...")
        self.add_entry.bind("<FocusIn>", lambda e: self._clear_ph())
        tk.Button(bf, text="➕ 추가", bg="#27ae60", fg="white",
                  font=("Segoe UI", 9, "bold"), bd=0, padx=8, command=self._add_proc).pack(side="left", padx=2)
        tk.Button(bf, text="➖ 삭제", bg="#e74c3c", fg="white",
                  font=("Segoe UI", 9, "bold"), bd=0, padx=8, command=self._del_proc).pack(side="left")

        # ─── 시작 프로그램 ───
        card4 = tk.LabelFrame(w, text="  시작 프로그램  ", bg=card_bg, fg=accent,
                              font=("Segoe UI", 11, "bold"), bd=1, relief="groove", padx=15, pady=10)
        card4.pack(fill="x", padx=20, pady=5)

        sb = tk.Frame(card4, bg=card_bg)
        sb.pack(fill="x")
        is_reg = StartupManager.is_registered()
        self.startup_status = ttk.Label(sb, text="✅ 등록됨" if is_reg else "❌ 미등록", style="Card.TLabel")
        self.startup_status.pack(side="left")
        tk.Button(sb, text="🗑️ 해제", bg="#e74c3c", fg="white",
                  font=("Segoe UI", 9, "bold"), bd=0, padx=10,
                  command=self._unreg).pack(side="right", padx=2)
        tk.Button(sb, text="🚀 등록", bg="#27ae60", fg="white",
                  font=("Segoe UI", 9, "bold"), bd=0, padx=10,
                  command=self._reg).pack(side="right", padx=2)

        # ─── 하단 ───
        bottom = tk.Frame(w, bg=bg)
        bottom.pack(fill="x", padx=20, pady=(10, 15))
        tk.Button(bottom, text="📋 로그 보기", bg=btn_bg, fg="white",
                  font=("Segoe UI", 10, "bold"), bd=0, padx=15, pady=6,
                  command=self._show_log).pack(side="left")
        tk.Button(bottom, text="💾 설정 저장", bg=accent, fg="white",
                  font=("Segoe UI", 10, "bold"), bd=0, padx=15, pady=6,
                  command=self._save).pack(side="right")

    def _update_status(self):
        if self.root.winfo_exists():
            self.status_label.config(text=f"📡 {self.monitor.status}")
            self.root.after(1000, self._update_status)

    def _toggle_smt(self):
        v = not self.smt_var.get()
        self.smt_var.set(v)
        self.config["enabled"] = v
        self.config.save()
        self.toggle_btn.config(text="ON ✅" if v else "OFF ❌", bg="#27ae60" if v else "#e74c3c")
        if self.tray_icon:
            self.tray_icon.icon = create_icon(v)
        logger.info(f"SMT 자동 적용: {'활성화' if v else '비활성화'}")
        self.monitor.wake_up()

    def _apply_mask(self):
        val = self.mask_entry.get().strip()
        self.config["custom_mask"] = val
        self.config.save()
        self.monitor._update_mask()
        
        cpu_info = f"CPU: {psutil.cpu_count(logical=False)}코어 / {psutil.cpu_count(logical=True)}스레드"
        self.mask_info_label.config(text=f"ℹ️ {cpu_info} | SMT OFF 적용 마스크: {hex(self.monitor.smt_off_mask)}")
        messagebox.showinfo(APP_NAME, f"마스크가 적용되었습니다!\n적용 값: {hex(self.monitor.smt_off_mask)}")
        self.monitor.wake_up()

    def _clear_ph(self):
        if self.add_entry.get() == "프로세스 이름 입력...":
            self.add_entry.delete(0, "end")

    def _add_proc(self):
        name = self.add_entry.get().strip()
        if name and name != "프로세스 이름 입력...":
            if name not in self.config["game_processes"]:
                self.config["game_processes"].append(name)
                self.process_listbox.insert("end", name)
                self.add_entry.delete(0, "end")
                logger.info(f"프로세스 추가: {name}")

    def _del_proc(self):
        sel = self.process_listbox.curselection()
        if sel:
            name = self.process_listbox.get(sel[0])
            self.process_listbox.delete(sel[0])
            if name in self.config["game_processes"]:
                self.config["game_processes"].remove(name)
            logger.info(f"프로세스 제거: {name}")

    def _reg(self):
        ok, msg = StartupManager.register()
        self.startup_status.config(text="✅ 등록됨" if ok else "❌ 미등록")
        (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg)

    def _unreg(self):
        ok, msg = StartupManager.unregister()
        self.startup_status.config(text="❌ 미등록" if ok else "✅ 등록됨")
        (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg)

    def _show_log(self):
        lw = tk.Toplevel(self.root)
        lw.title(f"{APP_NAME} - 로그")
        lw.geometry("600x400")
        lw.configure(bg="#1a1a2e")
        t = scrolledtext.ScrolledText(lw, bg="#0a0a1a", fg="#e0e0e0", font=("Consolas", 9),
                                       insertbackground="#e0e0e0", wrap="word")
        t.pack(fill="both", expand=True, padx=10, pady=10)
        try:
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-200:]:
                        t.insert("end", line)
            else:
                t.insert("end", "로그가 아직 없습니다.")
        except Exception as e:
            t.insert("end", f"로그 로드 실패: {e}")
        t.config(state="disabled")
        t.see("end")

    def _save(self):
        self.config["custom_mask"] = self.mask_entry.get().strip()
        self.config.save()
        logger.info("설정 저장 완료")
        messagebox.showinfo(APP_NAME, "설정이 저장되었습니다! ✅")
        self.monitor.wake_up()

    def _minimize_to_tray(self):
        self.config["custom_mask"] = self.mask_entry.get().strip()
        self.config.save()
        self.root.withdraw()

    def _start_tray(self):
        if self.tray_running: return
        menu = pystray.Menu(
            pystray.MenuItem(f"⚡ {APP_NAME} v{APP_VERSION}", None, enabled=False),
            pystray.MenuItem(f"   {APP_AUTHOR}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⚙️ 설정 열기", self._show_from_tray),
            pystray.MenuItem("📋 로그 폴더 열기", lambda: os.startfile(BASE_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ 완전 종료", self._quit_app),
        )
        self.tray_icon = pystray.Icon(
            APP_NAME,
            create_icon(self.config["enabled"]),
            f"{APP_NAME} - {APP_AUTHOR}",
            menu
        )
        self.tray_running = True
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _show_from_tray(self, icon=None, item=None):
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_app(self, icon=None, item=None):
        logger.info(f"═══ {APP_NAME} 완전 종료 지시 | {APP_AUTHOR} ═══")
        self.monitor.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

def main():
    if not getattr(sys, 'frozen', False):
        python_exe = sys.executable
        if python_exe.lower().endswith("python.exe") and "--nowindow" not in sys.argv:
            pythonw_exe = python_exe[:-10] + "pythonw.exe"
            if os.path.exists(pythonw_exe):
                args = [pythonw_exe, os.path.abspath(__file__)] + sys.argv[1:] + ["--nowindow"]
                subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                sys.exit()

    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "SMTManager_SOBRA_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        if "--tray" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(
                0, f"{APP_NAME}이(가) 이미 백그라운드에서 실행 중입니다!\n우측 하단 트레이 아이콘(^)을 확인하세요.",
                APP_NAME, 0x40
            )
        sys.exit()

    if not is_admin():
        run_as_admin()
        return

    app = SMTManagerApp()
    app.run()

if __name__ == "__main__":
    main()
