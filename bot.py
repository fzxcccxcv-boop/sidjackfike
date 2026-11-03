import ast
import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import traceback
from datetime import datetime
from collections import deque

import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "9078572432:AAFLQj4eUJA_Yr4mUDaog4_IUrRQYEkpsng"
OWNER_ID = 8684812026  # Replace with your numeric Telegram User ID

MANAGEMENT_FILE = "management.json"
SESSION_FILE = "session.json"
DEPLOYMENTS_FILE = "deployments.json"
STORAGE_DIR = "uploaded_files"
WORKSPACE_DIR = "user_workspaces"

os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

json_lock = threading.RLock()
process_lock = threading.RLock()
deployments_lock = threading.RLock()

# Quick-run processes (one-shot, terminal-driven, ephemeral)
runtime_processes = {}

# Deployments (persistent hosting, auto-restart, ports)
runtime_deployments = {}

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

PORT_RANGE_START = 8000
PORT_RANGE_END = 9000
FREE_DEPLOY_LIMIT = 1
PREMIUM_DEPLOY_LIMIT = 5
DEFAULT_MAX_RESTARTS = 10
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000


# ==========================================
# BOT API 9.4+ NATIVE COLOURED BUTTON WRAPPER
# ==========================================
class ColorInlineButton(InlineKeyboardButton):
    def __init__(self, text, callback_data=None, url=None, style=None, icon_custom_emoji_id=None, **kwargs):
        super().__init__(text=text, callback_data=callback_data, url=url, **kwargs)
        self.btn_style = style
        self.btn_icon_emoji = icon_custom_emoji_id

    def to_dict(self):
        data = super().to_dict()
        if self.btn_style in ("primary", "success", "danger"):
            data["style"] = self.btn_style
        if self.btn_icon_emoji:
            data["icon_custom_emoji_id"] = str(self.btn_icon_emoji)
        return data


def cbtn(text, callback_data=None, style=None, url=None, icon_custom_emoji_id=None):
    return ColorInlineButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style=style,
        icon_custom_emoji_id=icon_custom_emoji_id
    )


# ==========================================
# JSON STORE HELPERS
# ==========================================
def _atomic_write_json(filepath, data):
    temp_path = f"{filepath}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(temp_path, filepath)


def init_json_stores():
    with json_lock:
        if not os.path.exists(MANAGEMENT_FILE):
            _atomic_write_json(MANAGEMENT_FILE, {
                "settings": {
                    "bot_status": "ON",
                    "premium_price": "₹199 / Month",
                    "upi_id": "yourname@upi"
                },
                "users": {},
                "files": []
            })

        if not os.path.exists(SESSION_FILE):
            _atomic_write_json(SESSION_FILE, {"user_states": {}, "active_sessions": {}})
        else:
            sess = load_session()
            for uid, info in list(sess.get("active_sessions", {}).items()):
                pid = info.get("pid")
                if pid and psutil.pid_exists(pid):
                    try:
                        psutil.Process(pid).terminate()
                    except Exception:
                        pass
                sess["active_sessions"][uid]["status"] = "TERMINATED_ON_RESTART"
            sess["user_states"] = {}
            save_session(sess)

    if not os.path.exists(DEPLOYMENTS_FILE):
        _atomic_write_json(DEPLOYMENTS_FILE, {"next_id": 1, "deployments": {}})


def load_management():
    with json_lock:
        with open(MANAGEMENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_management(data):
    with json_lock:
        _atomic_write_json(MANAGEMENT_FILE, data)


def load_session():
    with json_lock:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_session(data):
    with json_lock:
        _atomic_write_json(SESSION_FILE, data)


def load_deployments():
    with deployments_lock:
        with open(DEPLOYMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_deployments(data):
    with deployments_lock:
        _atomic_write_json(DEPLOYMENTS_FILE, data)


def register_or_update_user(user):
    uid = str(user.id)
    with json_lock:
        mgmt = load_management()
        if uid not in mgmt["users"]:
            mgmt["users"][uid] = {
                "user_id": user.id,
                "username": user.username or "N/A",
                "first_name": user.first_name or "User",
                "is_premium": False,
                "uploads_count": 0,
                "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        else:
            mgmt["users"][uid]["username"] = user.username or "N/A"
            mgmt["users"][uid]["first_name"] = user.first_name or "User"
        save_management(mgmt)


def set_user_state(user_id, step, **kwargs):
    uid = str(user_id)
    with json_lock:
        sess = load_session()
        state_data = {"step": step, "updated_at": datetime.now().strftime("%H:%M:%S")}
        state_data.update(kwargs)
        sess["user_states"][uid] = state_data
        save_session(sess)


def get_user_state(user_id):
    uid = str(user_id)
    with json_lock:
        sess = load_session()
        return sess.get("user_states", {}).get(uid, {"step": "IDLE"})


def sync_session_process(user_id, pid, filename, status, logs_list):
    uid = str(user_id)
    with json_lock:
        sess = load_session()
        sess["active_sessions"][uid] = {
            "pid": pid,
            "filename": filename,
            "status": status,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_logs": list(logs_list)[-15:]
        }
        save_session(sess)


# ==========================================
# PYTHON PACKAGE AUTO / MANUAL INSTALLER
# ==========================================
MODULE_ERROR_RE = re.compile(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]")

PYPI_NAME_OVERRIDES = {
    "cv2": "opencv-python", "pil": "Pillow", "PIL": "Pillow",
    "bs4": "beautifulsoup4", "sklearn": "scikit-learn",
    "yaml": "PyYAML", "dotenv": "python-dotenv",
    "Crypto": "pycryptodome", "dateutil": "python-dateutil",
    "jwt": "PyJWT", "serial": "pyserial",
    "psycopg2": "psycopg2-binary", "mysql": "mysql-connector-python",
    "attr": "attrs", "OpenSSL": "pyOpenSSL",
    "telegram": "python-telegram-bot", "pptx": "python-pptx",
    "docx": "python-docx", "fitz": "PyMuPDF", "qrcode": "qrcode",
}


def _normalize_package_name(mod_name):
    root = (mod_name or "").split(".")[0]
    return PYPI_NAME_OVERRIDES.get(root, root)


def install_packages(packages, timeout=240):
    """Pip-install a set of packages (module names are auto-mapped to PyPI)."""
    pkgs = sorted({_normalize_package_name(p) for p in packages if p})
    if not pkgs:
        return True, "ℹ️ No packages to install."
    cmd = [sys.executable, "-m", "pip", "install", "--no-input",
           "--disable-pip-version-check", "--quiet", *pkgs]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, f"✅ Installed: <code>{', '.join(pkgs)}</code>"
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        return False, f"⚠️ pip failed for <code>{', '.join(pkgs)}</code>:\n<pre>{tail}</pre>"
    except subprocess.TimeoutExpired:
        return False, f"⚠️ pip timed out for <code>{', '.join(pkgs)}</code>"
    except Exception as e:
        return False, f"⚠️ pip error: {e}"


def _rerun_kb():
    kb = InlineKeyboardMarkup()
    kb.add(cbtn("▶️ Run Again", callback_data="terminal_rerun", style="success"))
    kb.add(cbtn("🖥️ Open Terminal", callback_data="terminal_view", style="primary"))
    return kb


def _auto_install_and_notify_quickrun(user_id, missing):
    """Called from the streaming thread after a quick-run dies with ModuleNotFoundError."""
    def _worker():
        ok, msg = install_packages(missing)
        try:
            bot.send_message(
                user_id,
                f"📦 <b>Missing packages detected</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Your script failed due to missing module(s): "
                f"<code>{', '.join(sorted(missing))}</code>\n\n{msg}",
                reply_markup=_rerun_kb()
            )
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


# ==========================================
# LIGHTWEIGHT RDP-GUARD AST SCANNER
# ==========================================
class RDPGuardScanner(ast.NodeVisitor):
    """
    Only blocks RDP / remote-shell enabling code.
    Everything else (subprocess, socket, shutil, os.system, psutil,
    importlib, webbrowser, ctypes-not-checked-via-call, etc.) is allowed
    so normal user scripts deploy successfully.
    """

    BANNED_MODULES = {
        "winreg",     # Windows registry — toggles fDenyTSConnections (RDP)
        "ctypes",     # Win32 API access — can flip RDP on
        "pty",        # Pseudo-terminal — spawns a real shell
        "paramiko",   # SSH client / server
        "asyncssh",   # SSH
        "telnetlib",  # Telnet
    }

    SUSPICIOUS_STRINGS = [
        "3389",
        "xrdp", "xrdp-sesman", "xorgxrdp",
        "rdesktop", "xfreerdp", "freerdp",
        "mstsc",
        "fdenytsconnections",
        "terminal server",
        "enable remote desktop", "enable-rdp", "enable_rdp",
        "rdp_enabled", "rdp_port",
        "netsh advfirewall", "netsh firewall",
        "reg add", "regedit",
        "x11vnc", "tigervnc", "realvnc", "vncserver",
        "anydesk", "teamviewer", "chrome remote desktop",
        "remmina", "vinagre", "guacamole",
    ]

    def __init__(self):
        self.issues = []

    def visit_Import(self, node):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in self.BANNED_MODULES:
                self.issues.append(f"Line {node.lineno}: Restricted module '{alias.name}' (RDP/remote access)")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            root = node.module.split(".")[0]
            if root in self.BANNED_MODULES:
                self.issues.append(f"Line {node.lineno}: Restricted import from '{node.module}' (RDP/remote access)")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            low = node.value.lower()
            for pattern in self.SUSPICIOUS_STRINGS:
                if pattern in low:
                    self.issues.append(f"Line {node.lineno}: Blocked RDP/remote-desktop pattern '{pattern}'")
                    break
        self.generic_visit(node)


def scan_python_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        scanner = RDPGuardScanner()
        scanner.visit(tree)
        return len(scanner.issues) == 0, scanner.issues
    except SyntaxError as e:
        return False, [f"SyntaxError at line {e.lineno}: {e.msg}"]
    except Exception as e:
        return False, [f"Parse error: {str(e)}"]


# ==========================================
# QUICK-RUN PROCESS ENGINE (one-shot terminal)
# ==========================================
def is_user_process_running(user_id):
    uid = str(user_id)
    with process_lock:
        info = runtime_processes.get(uid)
        if info and info["proc"].poll() is None:
            return True
        return False


def _stream_stdout_worker(user_id, proc, log_buffer, filename, log_file_path):
    uid = str(user_id)
    missing_modules = set()
    try:
        with open(log_file_path, "a", encoding="utf-8", errors="replace") as lf:
            for raw_line in iter(proc.stdout.readline, ''):
                if not raw_line:
                    break
                cleaned = ANSI_ESCAPE.sub('', raw_line).replace("\r", "").strip()
                if cleaned:
                    timestamped = f"[{datetime.now().strftime('%H:%M:%S')}] {cleaned}"
                    log_buffer.append(timestamped)
                    lf.write(timestamped + "\n")
                    lf.flush()
                    sync_session_process(uid, proc.pid, filename, "RUNNING", log_buffer)
                    m = MODULE_ERROR_RE.search(cleaned)
                    if m:
                        missing_modules.add(m.group(1).split(".")[0])
    except Exception as e:
        log_buffer.append(f"[Stream Error] {str(e)}")
    finally:
        exit_code = proc.poll()
        log_buffer.append(f"[{datetime.now().strftime('%H:%M:%S')}] Process exited with code {exit_code}")
        sync_session_process(uid, proc.pid, filename, f"EXITED ({exit_code})", log_buffer)
        if missing_modules:
            _auto_install_and_notify_quickrun(user_id, missing_modules)


def start_user_script(user_id, file_path, filename, startup_inputs=None):
    uid = str(user_id)
    with process_lock:
        if is_user_process_running(uid):
            return False, "You already have a quick-run script active! Stop it first."

        user_work_dir = os.path.join(WORKSPACE_DIR, uid, "quickrun")
        os.makedirs(user_work_dir, exist_ok=True)
        target_script = os.path.join(user_work_dir, "main.py")
        log_file_path = os.path.join(user_work_dir, "console.log")

        shutil.copyfile(file_path, target_script)
        with open(log_file_path, "w", encoding="utf-8") as lf:
            lf.write(f"=== Session Started: {filename} ({datetime.now()}) ===\n")

        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8"
        }
        if os.name == "nt":
            safe_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
            safe_env["USERPROFILE"] = os.path.abspath(user_work_dir)
        else:
            safe_env["HOME"] = os.path.abspath(user_work_dir)

        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            cwd=user_work_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=safe_env
        )

        log_buffer = deque(maxlen=30)
        log_buffer.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Launching {filename} (PID: {proc.pid})...")

        if startup_inputs:
            try:
                payload = "\n".join(startup_inputs) + "\n"
                proc.stdin.write(payload)
                proc.stdin.flush()
                log_buffer.append(f"[{datetime.now().strftime('%H:%M:%S')}] ⌨️ Sent {len(startup_inputs)} startup input(s).")
            except Exception as e:
                log_buffer.append(f"[Stdin Warning] {str(e)}")

        runtime_processes[uid] = {
            "proc": proc,
            "logs": log_buffer,
            "filename": filename,
            "start_time": time.time(),
            "log_file": log_file_path,
            "file_path": file_path,
            "startup_inputs": startup_inputs,
        }

        sync_session_process(uid, proc.pid, filename, "RUNNING", log_buffer)

        threading.Thread(
            target=_stream_stdout_worker,
            args=(uid, proc, log_buffer, filename, log_file_path),
            daemon=True
        ).start()
        return True, "Started"


def send_live_stdin(user_id, text_line):
    uid = str(user_id)
    with process_lock:
        info = runtime_processes.get(uid)
        if not info or info["proc"].poll() is not None:
            return False, "Process is not running."
        try:
            info["proc"].stdin.write(text_line + "\n")
            info["proc"].stdin.flush()
            info["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ➡️ Input sent: {text_line}")
            sync_session_process(uid, info["proc"].pid, info["filename"], "RUNNING", info["logs"])
            return True, "Input delivered."
        except Exception as e:
            return False, f"Failed to send input: {e}"


def stop_user_script(user_id):
    uid = str(user_id)
    with process_lock:
        info = runtime_processes.get(uid)
        if not info:
            return False
        proc = info["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        info["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Terminated.")
        sync_session_process(uid, proc.pid, info["filename"], "STOPPED", info["logs"])
        return True


# ==========================================
# DEPLOYMENT ENGINE (persistent hosting)
# ==========================================
def find_free_port():
    """Return a free TCP port from the configured range, avoiding used deployments."""
    with deployments_lock:
        data = load_deployments()
        reserved = {d.get("port") for d in data["deployments"].values() if d.get("port")}
        reserved.update(info.get("port") for info in runtime_deployments.values() if info.get("port"))

    import socket as _sock
    for port in range(PORT_RANGE_START, PORT_RANGE_END):
        if port in reserved:
            continue
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.close()
            return port
        except OSError:
            continue
    return None


def count_user_deployments(user_id):
    data = load_deployments()
    uid = int(user_id)
    return sum(1 for d in data["deployments"].values() if d["user_id"] == uid)


def user_deploy_limit(user_id):
    if int(user_id) == OWNER_ID:
        return 999
    mgmt = load_management()
    u = mgmt["users"].get(str(user_id), {})
    return PREMIUM_DEPLOY_LIMIT if u.get("is_premium") else FREE_DEPLOY_LIMIT


def _rotate_log_if_needed(log_path):
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > LOG_MAX_BYTES:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-LOG_KEEP_LINES:])
    except Exception:
        pass


def _deployment_log_streamer(deploy_id, proc, logs, log_file):
    missing_modules = set()
    try:
        with open(log_file, "a", encoding="utf-8", errors="replace") as lf:
            for raw_line in iter(proc.stdout.readline, ''):
                if not raw_line:
                    break
                cleaned = ANSI_ESCAPE.sub('', raw_line).replace("\r", "").strip()
                if cleaned:
                    ts = f"[{datetime.now().strftime('%H:%M:%S')}] {cleaned}"
                    logs.append(ts)
                    lf.write(ts + "\n")
                    lf.flush()
                    m = MODULE_ERROR_RE.search(cleaned)
                    if m:
                        missing_modules.add(m.group(1).split(".")[0])
    except Exception as e:
        logs.append(f"[Stream Error] {e}")
    finally:
        try:
            _rotate_log_if_needed(log_file)
        except Exception:
            pass
        if missing_modules:
            def _install_worker():
                ok, msg = install_packages(missing_modules)
                with deployments_lock:
                    d = load_deployments()
                dep = d["deployments"].get(deploy_id)
                if dep:
                    try:
                        bot.send_message(
                            dep["user_id"],
                            f"📦 <b>Deployment #{deploy_id}</b> auto-install result:\n{msg}"
                        )
                    except Exception:
                        pass
            threading.Thread(target=_install_worker, daemon=True).start()


def create_deployment(user_id, filename, file_path, auto_restart=True, port=None):
    uid = int(user_id)
    if count_user_deployments(uid) >= user_deploy_limit(uid):
        return None, f"You have reached your deployment limit ({user_deploy_limit(uid)})."

    if port is None:
        port = find_free_port()
        if port is None:
            return None, "No free port available in the configured range."

    with deployments_lock:
        data = load_deployments()
        dep_id = str(data["next_id"])
        data["next_id"] += 1
        data["deployments"][dep_id] = {
            "deploy_id": dep_id,
            "user_id": uid,
            "filename": filename,
            "file_path": file_path,
            "port": port,
            "env": {},
            "auto_restart": auto_restart,
            "max_restarts": DEFAULT_MAX_RESTARTS,
            "restart_count": 0,
            "status": "CREATED",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": None,
            "last_exit_code": None
        }
        save_deployments(data)
    return dep_id, "ok"


def start_deployment(deploy_id, _is_restart=False):
    with deployments_lock:
        data = load_deployments()
        dep = data["deployments"].get(deploy_id)
        if not dep:
            return False, "Deployment not found."

        info = runtime_deployments.get(deploy_id)
        if info and info["proc"].poll() is None:
            return False, "Already running."

        if not os.path.exists(dep["file_path"]):
            return False, "Source file missing on disk."

        work_dir = os.path.join(WORKSPACE_DIR, str(dep["user_id"]), f"dep_{deploy_id}")
        os.makedirs(work_dir, exist_ok=True)
        target_script = os.path.join(work_dir, "main.py")
        log_path = os.path.join(work_dir, "console.log")

        try:
            shutil.copyfile(dep["file_path"], target_script)
        except Exception as e:
            return False, f"Copy failed: {e}"

        if not _is_restart:
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write(f"=== Deployment {deploy_id} start: {datetime.now()} ===\n")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PORT": str(dep.get("port", "8000")),
            "DEPLOY_ID": deploy_id,
            "HOME": os.path.abspath(work_dir),
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
            env["USERPROFILE"] = os.path.abspath(work_dir)
        env.update(dep.get("env") or {})

        try:
            popen_kwargs = dict(
                cwd=work_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            proc = subprocess.Popen([sys.executable, "-u", "main.py"], **popen_kwargs)
        except Exception as e:
            return False, f"Launch failed: {e}"

        logs = deque(maxlen=200)
        logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Started PID {proc.pid} on port {dep.get('port')}")

        runtime_deployments[deploy_id] = {
            "proc": proc,
            "logs": logs,
            "log_file": log_path,
            "start_time": time.time(),
            "port": dep.get("port"),
            "user_id": dep["user_id"],
        }

        dep["status"] = "RUNNING"
        dep["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_deployments(data)

        threading.Thread(
            target=_deployment_log_streamer,
            args=(deploy_id, proc, logs, log_path),
            daemon=True
        ).start()
        return True, "Started"


def stop_deployment(deploy_id, set_status="STOPPED"):
    with deployments_lock:
        info = runtime_deployments.get(deploy_id)
        if info:
            proc = info["proc"]
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                except Exception:
                    pass
            del runtime_deployments[deploy_id]

        data = load_deployments()
        if deploy_id in data["deployments"]:
            data["deployments"][deploy_id]["status"] = set_status
            save_deployments(data)
    return True


def delete_deployment(deploy_id):
    stop_deployment(deploy_id, set_status="DELETED")
    with deployments_lock:
        data = load_deployments()
        if deploy_id in data["deployments"]:
            dep = data["deployments"].pop(deploy_id)
            save_deployments(data)
            try:
                work_dir = os.path.join(WORKSPACE_DIR, str(dep["user_id"]), f"dep_{deploy_id}")
                if os.path.isdir(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
    return True


def deployment_status(deploy_id):
    info = runtime_deployments.get(deploy_id)
    if info and info["proc"].poll() is None:
        return "RUNNING", info["proc"].pid, int(time.time() - info["start_time"])
    data = load_deployments()
    dep = data["deployments"].get(deploy_id)
    if not dep:
        return "DELETED", None, 0
    return dep.get("status", "STOPPED"), None, 0


# ==========================================
# DEPLOYMENT SUPERVISOR (auto-restart)
# ==========================================
def _supervisor_tick():
    with deployments_lock:
        data = load_deployments()

    for dep_id, dep in list(data["deployments"].items()):
        info = runtime_deployments.get(dep_id)

        # Process died - decide restart or mark stopped
        if info is not None and info["proc"].poll() is not None:
            exit_code = info["proc"].returncode
            del runtime_deployments[dep_id]

            with deployments_lock:
                fresh = load_deployments()
                d = fresh["deployments"].get(dep_id)
                if not d:
                    continue
                d["last_exit_code"] = exit_code

                can_restart = (
                    d.get("auto_restart", True)
                    and d.get("status") == "RUNNING"
                    and d.get("restart_count", 0) < d.get("max_restarts", DEFAULT_MAX_RESTARTS)
                )
                if can_restart:
                    d["restart_count"] = d.get("restart_count", 0) + 1
                    save_deployments(fresh)
                    delay = min(2 * d["restart_count"], 30)
                    threading.Timer(delay, lambda: start_deployment(dep_id, _is_restart=True)).start()
                else:
                    d["status"] = "CRASHED" if exit_code != 0 else "STOPPED"
                    save_deployments(fresh)


def supervisor_loop():
    while True:
        try:
            time.sleep(5)
            _supervisor_tick()
            # Rotate logs occasionally
            for info in list(runtime_deployments.values()):
                try:
                    _rotate_log_if_needed(info["log_file"])
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()


def boot_deployments():
    """On bot startup, restart any deployment that was RUNNING before shutdown."""
    data = load_deployments()
    for dep_id, dep in data["deployments"].items():
        if dep.get("status") == "RUNNING" and dep.get("auto_restart"):
            print(f"[Boot] Restarting deployment #{dep_id} ({dep.get('filename')})")
            start_deployment(dep_id, _is_restart=True)


# ==========================================
# INLINE KEYBOARDS
# ==========================================
def main_menu_kb(user_id):
    running = is_user_process_running(user_id)
    term_text = "🖥️ Quick Terminal (ACTIVE)" if running else "🖥️ Quick Terminal (IDLE)"
    term_style = "success" if running else "primary"

    with deployments_lock:
        data = load_deployments()
    dep_count = sum(1 for d in data["deployments"].values() if d["user_id"] == int(user_id))
    active_deps = sum(
        1 for dep_id, info in runtime_deployments.items()
        if info["user_id"] == int(user_id) and info["proc"].poll() is None
    )
    dep_label = f"🚀 My Deployments ({active_deps}/{dep_count})"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        cbtn("📤 Upload .PY File", callback_data="upload_file", style="primary"),
        cbtn(dep_label, callback_data="deployments_menu", style="success" if active_deps else "primary")
    )
    kb.add(
        cbtn(term_text, callback_data="terminal_view", style=term_style),
        cbtn("⚙️ Profile & Stats", callback_data="profile_settings")
    )
    kb.add(
        cbtn("💎 Buy Premium", callback_data="buy_premium", style="success")
    )
    if int(user_id) == OWNER_ID:
        kb.add(cbtn("👑 Owner Control Panel", callback_data="owner_panel", style="danger"))
    return kb


def terminal_kb(user_id):
    running = is_user_process_running(user_id)
    kb = InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            cbtn("🔄 Refresh Logs", callback_data="terminal_view", style="success"),
            cbtn("⌨️ Send Input (stdin)", callback_data="terminal_stdin", style="primary")
        )
        kb.add(
            cbtn("📥 Export Full Log", callback_data="terminal_export"),
            cbtn("🛑 Stop Script", callback_data="stop_process", style="danger")
        )
    else:
        kb.add(
            cbtn("🔄 Refresh Status", callback_data="terminal_view", style="primary"),
            cbtn("📥 Export Last Log", callback_data="terminal_export")
        )
        kb.add(cbtn("📤 Upload New File", callback_data="upload_file", style="success"))
    kb.add(cbtn("📦 Install Package (pip)", callback_data="terminal_install_pkg", style="primary"))
    kb.add(cbtn("⬅️ Back to Main Menu", callback_data="back_home"))
    return kb


def deployments_menu_kb(user_id):
    with deployments_lock:
        data = load_deployments()
    user_deps = [(k, v) for k, v in data["deployments"].items() if v["user_id"] == int(user_id)]

    kb = InlineKeyboardMarkup(row_width=1)
    for dep_id, dep in sorted(user_deps, key=lambda x: int(x[0]), reverse=True):
        state, _, _ = deployment_status(dep_id)
        if state == "RUNNING":
            icon, style = "🟢", "success"
        elif state == "CRASHED":
            icon, style = "🔴", "danger"
        elif state == "STOPPED":
            icon, style = "⚪", "primary"
        else:
            icon, style = "⚙️", "primary"
        kb.add(cbtn(
            f"{icon} #{dep_id} • {dep['filename']}  :{dep.get('port', 'N/A')}",
            callback_data=f"dep_view_{dep_id}",
            style=style
        ))
    kb.add(cbtn("➕ New Deployment", callback_data="dep_new", style="success"))
    kb.add(cbtn("⬅️ Back to Main Menu", callback_data="back_home"))
    return kb


def deployment_detail_kb(deploy_id):
    state, _, _ = deployment_status(deploy_id)
    kb = InlineKeyboardMarkup(row_width=2)
    if state == "RUNNING":
        kb.add(
            cbtn("🔄 Refresh", callback_data=f"dep_view_{deploy_id}", style="success"),
            cbtn("🔁 Restart", callback_data=f"dep_restart_{deploy_id}", style="primary")
        )
        kb.add(
            cbtn("📥 Export Log", callback_data=f"dep_logs_{deploy_id}"),
            cbtn("🛑 Stop", callback_data=f"dep_stop_{deploy_id}", style="danger")
        )
    else:
        kb.add(
            cbtn("🔄 Refresh", callback_data=f"dep_view_{deploy_id}", style="primary"),
            cbtn("▶️ Start", callback_data=f"dep_start_{deploy_id}", style="success")
        )
        kb.add(
            cbtn("📥 Export Log", callback_data=f"dep_logs_{deploy_id}"),
            cbtn("🗑️ Delete", callback_data=f"dep_delete_{deploy_id}", style="danger")
        )
    kb.add(cbtn("⬅️ Back to Deployments", callback_data="deployments_menu"))
    return kb


def deploy_source_kb(user_id):
    mgmt = load_management()
    safe_files = [
        f for f in mgmt["files"]
        if f["user_id"] == int(user_id) and f.get("status") == "SAFE"
    ]
    kb = InlineKeyboardMarkup(row_width=1)
    for f in safe_files[-10:]:
        kb.add(cbtn(
            f"📄 {f['filename']} (#{f['file_id']})",
            callback_data=f"dep_create_{f['file_id']}",
            style="success"
        ))
    kb.add(cbtn("⬅️ Back to Deployments", callback_data="deployments_menu"))
    return kb


def owner_panel_kb():
    mgmt = load_management()
    bot_status = mgmt["settings"].get("bot_status", "ON")
    if bot_status == "ON":
        toggle_btn = cbtn("🟢 Bot Status: ON (Tap to Turn OFF)", callback_data="admin_toggle_bot", style="success")
    else:
        toggle_btn = cbtn("🔴 Bot Status: OFF (Tap to Turn ON)", callback_data="admin_toggle_bot", style="danger")

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(toggle_btn)
    kb.add(
        cbtn("👥 Users & Active Jobs", callback_data="admin_users", style="primary"),
        cbtn("🖥️ Server Health", callback_data="admin_server", style="primary")
    )
    kb.add(
        cbtn("📂 Download User Files", callback_data="admin_files", style="success"),
        cbtn("💳 Set UPI & Price", callback_data="admin_edit_upi", style="success")
    )
    kb.add(
        cbtn("💎 Toggle User Premium", callback_data="admin_toggle_prem", style="primary"),
        cbtn("🛑 Kill User Process", callback_data="admin_kill_proc", style="danger")
    )
    kb.add(cbtn("🚀 All Deployments", callback_data="admin_deployments", style="danger"))
    kb.add(cbtn("📦 Export management.json & session.json", callback_data="admin_export_json"))
    kb.add(cbtn("⬅️ Back to Main Menu", callback_data="back_home"))
    return kb


# ==========================================
# TEXT RENDER HELPERS
# ==========================================
def build_dashboard_text(user):
    uid = str(user.id)
    mgmt = load_management()
    u_info = mgmt["users"].get(uid, {})
    running = is_user_process_running(uid)

    with deployments_lock:
        data = load_deployments()
    my_deps = [d for d in data["deployments"].values() if d["user_id"] == user.id]
    active_deps = sum(1 for d in my_deps if deployment_status(d["deploy_id"])[0] == "RUNNING")
    limit = user_deploy_limit(user.id)

    plan_badge = "💎 Premium" if u_info.get("is_premium") else "🆓 Free Tier"
    quick_badge = "🟢 Running" if running else "⚪ Idle"

    return (
        f"✨ <b>Python Cloud Hosting Platform</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.first_name} (<code>{user.id}</code>)\n"
        f"🏅 <b>Plan:</b> {plan_badge}  •  <b>Deploy slots:</b> {active_deps}/{limit}\n"
        f"🚀 <b>Deployments:</b> {active_deps} running / {len(my_deps)} total\n"
        f"🖥️ <b>Quick Terminal:</b> {quick_badge}\n"
        f"📂 <b>Files Uploaded:</b> {u_info.get('uploads_count', 0)}\n"
        f"🛡️ <b>RDP Guard:</b> 🟢 Active\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Select an action below:</i>"
    )


def build_terminal_text(user_id):
    uid = str(user_id)
    info = runtime_processes.get(uid)
    sess = load_session()
    saved_sess = sess.get("active_sessions", {}).get(uid)

    if not info and not saved_sess:
        return None

    if info:
        proc = info["proc"]
        is_alive = proc.poll() is None
        state_badge = "🟢 RUNNING" if is_alive else f"🔴 EXITED ({proc.returncode})"
        uptime = int(time.time() - info["start_time"])
        fname = info["filename"]
        pid = proc.pid
        raw_logs = list(info["logs"])[-16:]
    else:
        state_badge = f"🔴 {saved_sess.get('status', 'STOPPED')}"
        uptime = 0
        fname = saved_sess.get("filename", "N/A")
        pid = saved_sess.get("pid", "N/A")
        raw_logs = saved_sess.get("last_logs", [])

    logs_joined = "\n".join(raw_logs) if raw_logs else "Waiting for console output..."
    logs_escaped = logs_joined.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        f"🖥️ <b>Quick Run Terminal</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>File:</b> <code>{fname}</code> | 🆔 <b>PID:</b> <code>{pid}</code>\n"
        f"📡 <b>State:</b> {state_badge} | ⏱️ <b>Uptime:</b> {uptime}s\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<pre>{logs_escaped[-3200:]}</pre>"
    )


def build_deployment_detail_text(deploy_id):
    with deployments_lock:
        data = load_deployments()
    dep = data["deployments"].get(deploy_id)
    if not dep:
        return None

    state, pid, uptime = deployment_status(deploy_id)
    info = runtime_deployments.get(deploy_id)
    logs = list(info["logs"])[-20:] if info else []
    logs_str = "\n".join(logs) if logs else "<i>No recent output</i>"
    logs_escaped = logs_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    state_icon = {"RUNNING": "🟢 RUNNING", "STOPPED": "⚪ STOPPED",
                  "CRASHED": "🔴 CRASHED", "CREATED": "⚙️ CREATED"}.get(state, state)

    return (
        f"🚀 <b>Deployment #{deploy_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>File:</b> <code>{dep['filename']}</code>\n"
        f"🌐 <b>Port:</b> <code>{dep.get('port', 'N/A')}</code>\n"
        f"📊 <b>Status:</b> {state_icon}\n"
        f"🆔 <b>PID:</b> <code>{pid or 'N/A'}</code>\n"
        f"⏱️ <b>Uptime:</b> {uptime}s\n"
        f"🔄 <b>Restarts:</b> {dep.get('restart_count', 0)}/{dep.get('max_restarts', DEFAULT_MAX_RESTARTS)}\n"
        f"♾️ <b>Auto-restart:</b> {'Yes' if dep.get('auto_restart') else 'No'}\n"
        f"📅 <b>Created:</b> {dep.get('created_at', 'N/A')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Recent logs:</b>\n<pre>{logs_escaped[-3000:]}</pre>"
    )


# ==========================================
# BOT HANDLERS
# ==========================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = message.from_user
    register_or_update_user(user)
    set_user_state(user.id, "IDLE")

    mgmt = load_management()
    if mgmt["settings"].get("bot_status") == "OFF" and user.id != OWNER_ID:
        bot.reply_to(message, "🔴 <b>Bot is currently offline for maintenance by the Owner.</b>")
        return

    bot.send_message(
        message.chat.id,
        build_dashboard_text(user),
        reply_markup=main_menu_kb(user.id)
    )


@bot.message_handler(commands=["pip"])
def cmd_pip(message):
    user = message.from_user
    register_or_update_user(user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "📦 Usage: <code>/pip package1 package2</code>\nExample: <code>/pip requests numpy</code>")
        return
    packages = [p.strip() for p in re.split(r"[,\s]+", parts[1]) if p.strip()]
    bot.reply_to(message, f"⏳ Installing: <code>{', '.join(packages)}</code>...")
    def _w():
        ok, msg = install_packages(packages)
        try:
            bot.send_message(message.chat.id, msg)
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user = call.from_user
    user_id = user.id
    uid = str(user_id)
    data = call.data

    register_or_update_user(user)
    mgmt = load_management()

    if mgmt["settings"].get("bot_status") == "OFF" and user_id != OWNER_ID:
        bot.answer_callback_query(call.id, "🔴 Bot is currently turned OFF by the Owner.", show_alert=True)
        return

    # ---------------- HOME ----------------
    if data == "back_home":
        set_user_state(user_id, "IDLE")
        bot.edit_message_text(
            build_dashboard_text(user),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu_kb(user_id)
        )

    # ---------------- UPLOAD ----------------
    elif data == "upload_file":
        set_user_state(user_id, "AWAITING_FILE")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("✖️ Cancel Upload", callback_data="back_home", style="danger"))
        bot.edit_message_text(
            "📤 <b>Upload Python Script</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Send your <code>.py</code> file as a document.\n\n"
            "<i>🛡️ Scanned by RDP Guard. After upload you can <b>Quick Run</b> it or "
            "<b>Deploy as Hosting</b> (persistent + auto-restart + port).</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "run_no_input":
        st = get_user_state(user_id)
        if st.get("step") != "AWAITING_STARTUP_INPUT":
            bot.answer_callback_query(call.id, "⚠️ Session expired. Upload the file again.", show_alert=True)
            return
        saved_path = st.get("temp_file")
        safe_name = st.get("temp_filename")
        set_user_state(user_id, "IDLE")
        ok, msg = start_user_script(user_id, saved_path, safe_name, startup_inputs=None)
        if not ok:
            bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
            return
        time.sleep(0.8)
        bot.edit_message_text(
            build_terminal_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=terminal_kb(user_id)
        )

    elif data == "deploy_now":
        st = get_user_state(user_id)
        if st.get("step") != "AWAITING_STARTUP_INPUT":
            bot.answer_callback_query(call.id, "⚠️ Session expired. Upload again.", show_alert=True)
            return
        saved_path = st.get("temp_file")
        safe_name = st.get("temp_filename")
        set_user_state(user_id, "IDLE")

        dep_id, msg = create_deployment(user_id, safe_name, saved_path, auto_restart=True)
        if not dep_id:
            bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
            return
        ok, smsg = start_deployment(dep_id)
        if not ok:
            bot.answer_callback_query(call.id, f"⚠️ {smsg}", show_alert=True)
            return
        time.sleep(0.6)
        bot.edit_message_text(
            build_deployment_detail_text(dep_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=deployment_detail_kb(dep_id)
        )

    # ---------------- QUICK TERMINAL ----------------
    elif data == "terminal_view":
        set_user_state(user_id, "IDLE")
        term_text = build_terminal_text(user_id)
        if not term_text:
            bot.answer_callback_query(call.id, "⚪ No quick-run session yet.", show_alert=True)
            return
        try:
            bot.edit_message_text(
                term_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=terminal_kb(user_id)
            )
            bot.answer_callback_query(call.id, "🟢 Refreshed")
        except Exception:
            bot.answer_callback_query(call.id, "🟢 Already up to date")

    elif data == "terminal_stdin":
        if not is_user_process_running(user_id):
            bot.answer_callback_query(call.id, "🔴 No active quick-run process.", show_alert=True)
            return
        set_user_state(user_id, "AWAITING_LIVE_STDIN")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Terminal", callback_data="terminal_view", style="primary"))
        bot.edit_message_text(
            "⌨️ <b>Send Live Input (stdin)</b>\n"
            "Type the line to pipe into your running script:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "terminal_export":
        log_path = os.path.join(WORKSPACE_DIR, uid, "quickrun", "console.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, "⚠️ No log file yet.", show_alert=True)
            return
        with open(log_path, "rb") as doc:
            bot.send_document(call.message.chat.id, doc,
                              caption=f"📥 <b>Quick-Run Log</b> (User <code>{user_id}</code>)")
        bot.answer_callback_query(call.id, "📥 Sent")

    elif data == "stop_process":
        if stop_user_script(user_id):
            bot.answer_callback_query(call.id, "🛑 Stopped", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "⚪ Already stopped")
        term_text = build_terminal_text(user_id)
        if term_text:
            bot.edit_message_text(term_text, call.message.chat.id, call.message.message_id,
                                  reply_markup=terminal_kb(user_id))

    elif data == "terminal_install_pkg":
        set_user_state(user_id, "AWAITING_PKG_NAME")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Terminal", callback_data="terminal_view", style="primary"))
        bot.edit_message_text(
            "📦 <b>Install Python Package</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Send one or more package names (space or comma separated).\n\n"
            "Example: <code>requests numpy pandas</code>\n"
            "<i>Installed into the bot's Python env via pip. Already-installed packages are skipped.</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "terminal_rerun":
        info = runtime_processes.get(uid)
        if not info or not info.get("file_path") or not os.path.exists(info["file_path"]):
            bot.answer_callback_query(call.id, "⚠️ Original file no longer available. Upload again.", show_alert=True)
            return
        ok, msg = start_user_script(
            user_id, info["file_path"], info["filename"],
            startup_inputs=info.get("startup_inputs")
        )
        if not ok:
            bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
            return
        time.sleep(0.8)
        bot.edit_message_text(
            build_terminal_text(user_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=terminal_kb(user_id)
        )

    # ---------------- DEPLOYMENTS ----------------
    elif data == "deployments_menu":
        set_user_state(user_id, "IDLE")
        bot.edit_message_text(
            "🚀 <b>My Deployments</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Hosted scripts run 24/7 with auto-restart. Tap one to manage, or add a new one.</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=deployments_menu_kb(user_id)
        )

    elif data == "dep_new":
        limit = user_deploy_limit(user_id)
        if count_user_deployments(user_id) >= limit:
            bot.answer_callback_query(
                call.id,
                f"⚠️ Deployment limit reached ({limit}). Delete one or upgrade to Premium.",
                show_alert=True
            )
            return
        kb = deploy_source_kb(user_id)
        bot.edit_message_text(
            "🆕 <b>New Deployment</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Pick one of your RDP-safe uploaded files to deploy:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data.startswith("dep_create_"):
        try:
            file_id = int(data.replace("dep_create_", ""))
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Invalid file ID", show_alert=True)
            return

        mgmt = load_management()
        file_entry = next((f for f in mgmt["files"] if f["file_id"] == file_id), None)
        if not file_entry or file_entry["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ File not found or not yours.", show_alert=True)
            return

        if count_user_deployments(user_id) >= user_deploy_limit(user_id):
            bot.answer_callback_query(call.id, "⚠️ Deployment limit reached.", show_alert=True)
            return

        dep_id, msg = create_deployment(user_id, file_entry["filename"], file_entry["saved_path"], auto_restart=True)
        if not dep_id:
            bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
            return
        ok, smsg = start_deployment(dep_id)
        if not ok:
            bot.answer_callback_query(call.id, f"⚠️ {smsg}", show_alert=True)
            return
        time.sleep(0.6)
        bot.edit_message_text(
            build_deployment_detail_text(dep_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=deployment_detail_kb(dep_id)
        )

    elif data.startswith("dep_view_"):
        dep_id = data.replace("dep_view_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        text = build_deployment_detail_text(dep_id)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=deployment_detail_kb(dep_id))
            bot.answer_callback_query(call.id, "🔄 Refreshed")
        except Exception:
            bot.answer_callback_query(call.id, "🔄 Already up to date")

    elif data.startswith("dep_stop_"):
        dep_id = data.replace("dep_stop_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        stop_deployment(dep_id, set_status="STOPPED")
        bot.answer_callback_query(call.id, "🛑 Stopped")
        bot.edit_message_text(build_deployment_detail_text(dep_id), call.message.chat.id,
                              call.message.message_id, reply_markup=deployment_detail_kb(dep_id))

    elif data.startswith("dep_start_"):
        dep_id = data.replace("dep_start_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        # Reset restart counter when manually started
        with deployments_lock:
            data2 = load_deployments()
            data2["deployments"][dep_id]["restart_count"] = 0
            save_deployments(data2)
        ok, msg = start_deployment(dep_id)
        bot.answer_callback_query(call.id, "▶️ Started" if ok else f"⚠️ {msg}",
                                  show_alert=not ok)
        time.sleep(0.4)
        bot.edit_message_text(build_deployment_detail_text(dep_id), call.message.chat.id,
                              call.message.message_id, reply_markup=deployment_detail_kb(dep_id))

    elif data.startswith("dep_restart_"):
        dep_id = data.replace("dep_restart_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        stop_deployment(dep_id, set_status="STOPPED")
        with deployments_lock:
            data2 = load_deployments()
            data2["deployments"][dep_id]["restart_count"] = 0
            save_deployments(data2)
        time.sleep(0.5)
        ok, msg = start_deployment(dep_id)
        bot.answer_callback_query(call.id, "🔁 Restarted" if ok else f"⚠️ {msg}",
                                  show_alert=not ok)
        time.sleep(0.4)
        bot.edit_message_text(build_deployment_detail_text(dep_id), call.message.chat.id,
                              call.message.message_id, reply_markup=deployment_detail_kb(dep_id))

    elif data.startswith("dep_delete_"):
        dep_id = data.replace("dep_delete_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        delete_deployment(dep_id)
        bot.answer_callback_query(call.id, "🗑️ Deleted")
        bot.edit_message_text(
            "🚀 <b>My Deployments</b>\n\n<i>Deployment deleted.</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=deployments_menu_kb(user_id)
        )

    elif data.startswith("dep_logs_"):
        dep_id = data.replace("dep_logs_", "")
        with deployments_lock:
            data2 = load_deployments()
        dep = data2["deployments"].get(dep_id)
        if not dep or dep["user_id"] != user_id:
            bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
            return
        log_path = os.path.join(WORKSPACE_DIR, str(dep["user_id"]), f"dep_{dep_id}", "console.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, "⚠️ No log file yet.", show_alert=True)
            return
        with open(log_path, "rb") as doc:
            bot.send_document(call.message.chat.id, doc,
                              caption=f"📥 <b>Deployment #{dep_id} log</b> — <code>{dep['filename']}</code>")
        bot.answer_callback_query(call.id, "📥 Sent")

    # ---------------- PROFILE ----------------
    elif data == "profile_settings":
        u_info = mgmt["users"].get(uid, {})
        running = is_user_process_running(user_id)
        sess_state = get_user_state(user_id).get("step", "IDLE")
        with deployments_lock:
            data2 = load_deployments()
        my_deps = [d for d in data2["deployments"].values() if d["user_id"] == user_id]

        prof_text = (
            f"⚙️ <b>Profile & Settings</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Name:</b> {u_info.get('first_name', user.first_name)}\n"
            f"🪪 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🏷️ <b>Username:</b> @{u_info.get('username', 'N/A')}\n"
            f"🏅 <b>Plan:</b> {'💎 Premium' if u_info.get('is_premium') else '🆓 Free'}\n"
            f"🚀 <b>Deploy limit:</b> {user_deploy_limit(user_id)}  •  <b>Used:</b> {len(my_deps)}\n"
            f"📊 <b>Files Uploaded:</b> {u_info.get('uploads_count', 0)}\n"
            f"🖥️ <b>Quick-run active:</b> {'Yes' if running else 'No'}\n"
            f"🧭 <b>Session state:</b> <code>{sess_state}</code>\n"
            f"📅 <b>Registered:</b> {u_info.get('joined_at', 'N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            cbtn("📤 Upload File", callback_data="upload_file", style="primary"),
            cbtn("🚀 My Deployments", callback_data="deployments_menu", style="success")
        )
        kb.add(cbtn("⬅️ Back to Menu", callback_data="back_home"))
        bot.edit_message_text(prof_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    # ---------------- PREMIUM ----------------
    elif data == "buy_premium":
        price = mgmt["settings"].get("premium_price", "₹199 / Month")
        upi = mgmt["settings"].get("upi_id", "yourname@upi")
        prem_text = (
            f"💎 <b>Upgrade to Premium</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Plan Price:</b> <code>{price}</code>\n"
            f"🏦 <b>UPI ID:</b> <code>{upi}</code>\n\n"
            f"<b>Premium benefits:</b>\n"
            f"• Up to {PREMIUM_DEPLOY_LIMIT} concurrent deployments (free: {FREE_DEPLOY_LIMIT})\n"
            f"• Priority support\n\n"
            f"<b>How to activate:</b>\n"
            f"1️⃣ Pay to the UPI above.\n"
            f"2️⃣ Send the screenshot + your User ID (<code>{user_id}</code>) to the Bot Owner."
        )
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Menu", callback_data="back_home", style="primary"))
        bot.edit_message_text(prem_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    # ==========================================
    # OWNER PANEL
    # ==========================================
    elif data == "owner_panel":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "IDLE")
        with deployments_lock:
            data2 = load_deployments()
        all_deps = data2["deployments"]
        active = sum(1 for d in all_deps if deployment_status(d)[0] == "RUNNING")
        quick = sum(1 for k in runtime_processes if is_user_process_running(k))

        panel_text = (
            f"👑 <b>Owner Administration Panel</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>Users:</b> {len(mgmt['users'])}\n"
            f"🚀 <b>Deployments:</b> {active} running / {len(all_deps)} total\n"
            f"🖥️ <b>Quick-run active:</b> {quick}\n"
            f"📂 <b>Files stored:</b> {len(mgmt['files'])}\n"
            f"💳 <b>UPI:</b> <code>{mgmt['settings'].get('upi_id')}</code>\n"
            f"💰 <b>Price:</b> <code>{mgmt['settings'].get('premium_price')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        bot.edit_message_text(panel_text, call.message.chat.id, call.message.message_id,
                              reply_markup=owner_panel_kb())

    elif data == "admin_toggle_bot":
        if user_id != OWNER_ID:
            return
        with json_lock:
            m = load_management()
            new_st = "OFF" if m["settings"].get("bot_status") == "ON" else "ON"
            m["settings"]["bot_status"] = new_st
            save_management(m)
        bot.answer_callback_query(call.id, f"Bot status: {new_st}")
        call.data = "owner_panel"
        handle_callbacks(call)

    elif data == "admin_users":
        if user_id != OWNER_ID:
            return
        users_dict = mgmt["users"]
        lines = []
        for u_id_str, u_obj in list(users_dict.items())[-15:]:
            run_flag = "🟢" if is_user_process_running(u_id_str) else "⚪"
            prem_flag = "💎" if u_obj.get("is_premium") else "🆓"
            with deployments_lock:
                d2 = load_deployments()
            dep_n = sum(1 for d in d2["deployments"].values() if str(d["user_id"]) == u_id_str)
            lines.append(
                f"• {prem_flag} <code>{u_id_str}</code> (@{u_obj.get('username')}) | "
                f"Files: {u_obj.get('uploads_count', 0)} | Deps: {dep_n} | Quick: {run_flag}"
            )
        user_list_str = "\n".join(lines) if lines else "No users yet."
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Owner Panel", callback_data="owner_panel", style="primary"))
        bot.edit_message_text(f"👥 <b>Users</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{user_list_str}",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "admin_server":
        if user_id != OWNER_ID:
            return
        cpu = psutil.cpu_percent(interval=0.3)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        boot_dt = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M")
        srv_text = (
            f"🖥️ <b>Server Health</b>\n"
            f"<i>(host identifiers masked)</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ CPU: <b>{cpu}%</b>\n"
            f"🧠 RAM: <b>{ram.percent}%</b> ({ram.used // (1024**2)} / {ram.total // (1024**2)} MB)\n"
            f"💾 Disk: <b>{disk.percent}%</b> ({disk.used // (1024**3)} / {disk.total // (1024**3)} GB)\n"
            f"🐍 Python: {sys.version.split()[0]}\n"
            f"⏱️ Boot: {boot_dt}"
        )
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Owner Panel", callback_data="owner_panel", style="primary"))
        bot.edit_message_text(srv_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "admin_files":
        if user_id != OWNER_ID:
            return
        files_list = mgmt.get("files", [])[-10:]
        kb = InlineKeyboardMarkup(row_width=1)
        for f_entry in reversed(files_list):
            fid = f_entry["file_id"]
            is_safe = f_entry.get("status") == "SAFE"
            kb.add(cbtn(
                f"📥 #{fid} | {f_entry['filename']} (User: {f_entry['user_id']})",
                callback_data=f"dlfile_{fid}",
                style="success" if is_safe else "danger"
            ))
        kb.add(cbtn("⬅️ Back to Owner Panel", callback_data="owner_panel", style="primary"))
        bot.edit_message_text("📂 <b>Recent Files</b>\n<i>Green = RDP-Safe | Red = Blocked</i>",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data.startswith("dlfile_"):
        if user_id != OWNER_ID:
            return
        fid = int(data.split("_")[1])
        target = next((x for x in mgmt.get("files", []) if x["file_id"] == fid), None)
        if target and os.path.exists(target["saved_path"]):
            with open(target["saved_path"], "rb") as doc:
                bot.send_document(call.message.chat.id, doc, caption=(
                    f"📥 <b>File #{fid}</b>\n"
                    f"📄 {target['filename']}\n"
                    f"👤 User: <code>{target['user_id']}</code>\n"
                    f"🛡️ Status: {target.get('status')}\n"
                    f"📅 {target['uploaded_at']}"
                ))
            bot.answer_callback_query(call.id, "📥 Sent")
        else:
            bot.answer_callback_query(call.id, "❌ Not found on disk.", show_alert=True)

    elif data == "admin_edit_upi":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_UPI")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("✖️ Cancel", callback_data="owner_panel", style="danger"))
        bot.edit_message_text(
            "💳 <b>Update Price & UPI</b>\n\n"
            "Send in this exact format:\n<code>Price | UPI_ID</code>\n\n"
            "Example:\n<code>₹249 / Month | merchant@okaxis</code>",
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )

    elif data == "admin_toggle_prem":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_PREM_UID")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("✖️ Cancel", callback_data="owner_panel", style="danger"))
        bot.edit_message_text(
            "💎 <b>Toggle User Premium</b>\n\nSend the numeric User ID:",
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )

    elif data == "admin_kill_proc":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_KILL_UID")
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("✖️ Cancel", callback_data="owner_panel", style="danger"))
        bot.edit_message_text(
            "🛑 <b>Force-Stop User</b>\n\nSend the numeric User ID to terminate their quick-run "
            "and all their deployments:",
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )

    elif data == "admin_deployments":
        if user_id != OWNER_ID:
            return
        with deployments_lock:
            d2 = load_deployments()
        lines = []
        for dep_id, dep in sorted(d2["deployments"].items(), key=lambda x: int(x[0]), reverse=True)[:20]:
            state, _, uptime = deployment_status(dep_id)
            icon = {"RUNNING": "🟢", "STOPPED": "⚪", "CRASHED": "🔴"}.get(state, "⚙️")
            lines.append(
                f"{icon} <b>#{dep_id}</b> — <code>{dep['filename']}</code> "
                f"(user {dep['user_id']}, port {dep.get('port')}, {state}, {uptime}s)"
            )
        body = "\n".join(lines) if lines else "No deployments."
        kb = InlineKeyboardMarkup()
        kb.add(cbtn("⬅️ Back to Owner Panel", callback_data="owner_panel", style="primary"))
        bot.edit_message_text(f"🚀 <b>All Deployments</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{body}",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "admin_export_json":
        if user_id != OWNER_ID:
            return
        for jf in (MANAGEMENT_FILE, SESSION_FILE, DEPLOYMENTS_FILE):
            if os.path.exists(jf):
                with open(jf, "rb") as f:
                    bot.send_document(call.message.chat.id, f, caption=f"📦 <code>{jf}</code>")
        bot.answer_callback_query(call.id, "📦 Exported")


# ==========================================
# DOCUMENT UPLOAD HANDLER
# ==========================================
@bot.message_handler(content_types=["document"])
def handle_document_message(message):
    user = message.from_user
    user_id = user.id
    register_or_update_user(user)

    mgmt = load_management()
    if mgmt["settings"].get("bot_status") == "OFF" and user_id != OWNER_ID:
        bot.reply_to(message, "🔴 Bot offline.")
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        bot.reply_to(message, "❌ Only <code>.py</code> files.",
                     reply_markup=main_menu_kb(user_id))
        return

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {e}", reply_markup=main_menu_kb(user_id))
        return

    safe_name = os.path.basename(doc.file_name)
    timestamp = int(time.time())
    saved_path = os.path.join(STORAGE_DIR, f"{user_id}_{timestamp}_{safe_name}")
    with open(saved_path, "wb") as f:
        f.write(downloaded_bytes)

    is_safe, issues = scan_python_file(saved_path)
    scan_status = "SAFE" if is_safe else "BLOCKED"

    with json_lock:
        mgmt = load_management()
        uid = str(user_id)
        mgmt["users"][uid]["uploads_count"] = mgmt["users"][uid].get("uploads_count", 0) + 1
        new_file_id = len(mgmt["files"]) + 1
        mgmt["files"].append({
            "file_id": new_file_id,
            "user_id": user_id,
            "filename": safe_name,
            "saved_path": saved_path,
            "status": scan_status,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        save_management(mgmt)

    if not is_safe:
        issue_lines = "\n".join([f"• <code>{i}</code>" for i in issues[:8]])
        set_user_state(user_id, "IDLE")
        bot.reply_to(
            message,
            f"🛡️ <b>RDP Guard Blocked File</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 <b>File:</b> <code>{safe_name}</code>\n"
            f"This file contains RDP / remote-desktop setup code that "
            f"could expose the host server:\n\n{issue_lines}",
            reply_markup=main_menu_kb(user_id)
        )
        return

    set_user_state(
        user_id,
        "AWAITING_STARTUP_INPUT",
        temp_file=saved_path,
        temp_filename=safe_name
    )

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(cbtn("🚀 Deploy as Hosting (24/7 + auto-restart + port)", callback_data="deploy_now", style="success"))
    kb.add(cbtn("▶️ Run Once (quick terminal)", callback_data="run_no_input", style="primary"))
    kb.add(cbtn("✖️ Cancel", callback_data="back_home", style="danger"))

    bot.reply_to(
        message,
        f"✅ <b>RDP Guard Check Passed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>File:</b> <code>{safe_name}</code>\n\n"
        f"Choose one:\n"
        f"• <b>🚀 Deploy as Hosting</b> — persistent, auto-restart, receives <code>$PORT</code> env\n"
        f"• <b>▶️ Run Once</b> — one-shot, terminal with stdin (send startup inputs as text reply)\n\n"
        f"<i>If you chose Run Once and your script needs startup inputs, reply with each "
        f"input on a new line (send <code>none</code> if no inputs).</i>",
        reply_markup=kb
    )


# ==========================================
# TEXT STATE ROUTER
# ==========================================
@bot.message_handler(content_types=["text"])
def handle_text_states(message):
    user = message.from_user
    user_id = user.id
    text = (message.text or "").strip()

    register_or_update_user(user)
    state_obj = get_user_state(user_id)
    step = state_obj.get("step", "IDLE")

    if step == "AWAITING_STARTUP_INPUT":
        saved_path = state_obj.get("temp_file")
        safe_name = state_obj.get("temp_filename")
        set_user_state(user_id, "IDLE")
        inputs_list = None if text.lower() == "none" else text.splitlines()
        ok, msg = start_user_script(user_id, saved_path, safe_name, startup_inputs=inputs_list)
        if not ok:
            bot.reply_to(message, f"⚠️ {msg}", reply_markup=main_menu_kb(user_id))
            return
        time.sleep(0.8)
        bot.reply_to(message, build_terminal_text(user_id), reply_markup=terminal_kb(user_id))
        return

    elif step == "AWAITING_LIVE_STDIN":
        set_user_state(user_id, "IDLE")
        ok, res = send_live_stdin(user_id, text)
        time.sleep(0.5)
        term_text = build_terminal_text(user_id) or f"ℹ️ {res}"
        bot.reply_to(message, term_text, reply_markup=terminal_kb(user_id))
        return

    elif step == "AWAITING_PKG_NAME":
        set_user_state(user_id, "IDLE")
        packages = [p.strip() for p in re.split(r"[,\s]+", text) if p.strip()]
        if not packages:
            bot.reply_to(message, "❌ No package names provided.", reply_markup=terminal_kb(user_id))
            return
        bot.reply_to(message, f"⏳ Installing: <code>{', '.join(packages)}</code>...")
        def _w():
            ok, msg = install_packages(packages)
            try:
                bot.send_message(message.chat.id, msg, reply_markup=terminal_kb(user_id))
            except Exception:
                pass
        threading.Thread(target=_w, daemon=True).start()
        return

    elif step == "AWAITING_UPI" and user_id == OWNER_ID:
        if "|" not in text:
            bot.reply_to(message, "❌ Format: <code>Price | UPI_ID</code>")
            return
        price, upi = [x.strip() for x in text.split("|", 1)]
        with json_lock:
            m = load_management()
            m["settings"]["premium_price"] = price
            m["settings"]["upi_id"] = upi
            save_management(m)
        set_user_state(user_id, "IDLE")
        bot.reply_to(message, f"✅ Updated.\n💰 {price}\n🏦 {upi}",
                     reply_markup=owner_panel_kb())
        return

    elif step == "AWAITING_PREM_UID" and user_id == OWNER_ID:
        target_uid = text.strip()
        with json_lock:
            m = load_management()
            if target_uid not in m["users"]:
                bot.reply_to(message, "❌ User not found.", reply_markup=owner_panel_kb())
                set_user_state(user_id, "IDLE")
                return
            curr = m["users"][target_uid].get("is_premium", False)
            m["users"][target_uid]["is_premium"] = not curr
            save_management(m)
        set_user_state(user_id, "IDLE")
        badge = "💎 GRANTED" if not curr else "🆓 REVOKED"
        bot.reply_to(message, f"✅ Premium for <code>{target_uid}</code>: <b>{badge}</b>",
                     reply_markup=owner_panel_kb())
        return

    elif step == "AWAITING_KILL_UID" and user_id == OWNER_ID:
        target_uid = text.strip()
        stopped_quick = stop_user_script(target_uid)
        # Also stop all their deployments
        with deployments_lock:
            d2 = load_deployments()
        stopped_deps = 0
        for dep_id, dep in d2["deployments"].items():
            if str(dep["user_id"]) == target_uid:
                stop_deployment(dep_id, set_status="STOPPED")
                stopped_deps += 1
        set_user_state(user_id, "IDLE")
        bot.reply_to(
            message,
            f"🛑 User <code>{target_uid}</code>\n"
            f"• Quick-run stopped: {'Yes' if stopped_quick else 'No active'}\n"
            f"• Deployments stopped: {stopped_deps}",
            reply_markup=owner_panel_kb()
        )
        return

    bot.reply_to(message, build_dashboard_text(user), reply_markup=main_menu_kb(user_id))


# ==========================================
# BOOT
# ==========================================
if __name__ == "__main__":
    init_json_stores()
    print("✅ Hoster Bot initializing (RDP Guard + Deployment Engine)...")

    # Boot any deployments marked RUNNING
    try:
        boot_deployments()
    except Exception:
        traceback.print_exc()

    # Start supervisor
    threading.Thread(target=supervisor_loop, daemon=True).start()

    print("✅ Ready — polling Telegram...")
    bot.infinity_polling(skip_pending=True)
