import ast
import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
from datetime import datetime
from collections import deque

import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
OWNER_ID = 123456789  # Replace with your numeric Telegram User ID

MANAGEMENT_FILE = "management.json"
SESSION_FILE = "session.json"
STORAGE_DIR = "uploaded_files"
WORKSPACE_DIR = "user_workspaces"

os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(WORKSPACE_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Thread locks for atomic JSON read/writes and subprocess control
json_lock = threading.RLock()
process_lock = threading.RLock()

# Live runtime processes: {user_id_str: {"proc": Popen, "logs": deque, "filename": str, "start_time": float, "log_file": str}}
runtime_processes = {}
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


# ==========================================
# JSON MANAGEMENT & SESSION ENGINE
# ==========================================
def _atomic_write_json(filepath, data):
    temp_path = f"{filepath}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(temp_path, filepath)


def init_json_stores():
    with json_lock:
        if not os.path.exists(MANAGEMENT_FILE):
            default_mgmt = {
                "settings": {
                    "bot_status": "ON",
                    "premium_price": "₹199 / Month",
                    "upi_id": "yourname@upi"
                },
                "users": {},
                "files": []
            }
            _atomic_write_json(MANAGEMENT_FILE, default_mgmt)

        if not os.path.exists(SESSION_FILE):
            default_sess = {
                "user_states": {},
                "active_sessions": {}
            }
            _atomic_write_json(SESSION_FILE, default_sess)
        else:
            # Clean up any leftover processes from a previous run
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
# ADVANCED AST STATIC SECURITY SCANNER
# ==========================================
class AdvancedASTScanner(ast.NodeVisitor):
    BANNED_MODULES = {
        "subprocess", "shutil", "pty", "ctypes", "pickle", "marshal",
        "builtins", "importlib", "inspect", "winreg", "getpass",
        "platform", "psutil", "socket", "paramiko", "ftplib", "telnetlib",
        "webbrowser", "multiprocessing", "signal", "resource", "sysconfig"
    }

    BANNED_CALLS = {
        "eval", "exec", "compile", "__import__", "globals", "locals",
        "vars", "getattr", "setattr", "delattr", "breakpoint", "memoryview"
    }

    BANNED_OS_ATTRS = {
        "system", "popen", "spawn", "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "execl", "execle", "execlp",
        "execlpe", "execv", "execve", "execvp", "execvpe", "fork", "forkpty",
        "kill", "killpg", "environ", "getenv", "putenv", "unsetenv", "listdir",
        "scandir", "walk", "remove", "unlink", "rmdir", "removedirs", "rename",
        "replace", "chmod", "chown", "chroot", "startfile", "getlogin", "getuid",
        "geteuid", "getgid", "getpid", "getppid"
    }

    BANNED_DUNDERS = {
        "__subclasses__", "__bases__", "__mro__", "__globals__",
        "__builtins__", "__code__", "__closure__", "__dict__", "__class__"
    }

    SUSPICIOUS_STRINGS = [
        "169.254.169.254", "api.ipify.org", "ipinfo.io", "ifconfig.me",
        "icanhazip.com", "ident.me", "checkip.amazonaws.com", "wtfismyip.com",
        "cmd.exe", "powershell", "/bin/sh", "/bin/bash", "/etc/passwd",
        "/etc/shadow", "systeminfo", "ipconfig", "whoami", "netstat", "tasklist",
        "management.json", "session.json"
    ]

    def __init__(self):
        self.issues = []

    def visit_Import(self, node):
        for alias in node.names:
            root_mod = alias.name.split(".")[0]
            if root_mod in self.BANNED_MODULES:
                self.issues.append(f"Line {node.lineno}: Restricted module '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            root_mod = node.module.split(".")[0]
            if root_mod in self.BANNED_MODULES:
                self.issues.append(f"Line {node.lineno}: Restricted import from '{node.module}'")
            if root_mod == "os":
                for alias in node.names:
                    if alias.name in self.BANNED_OS_ATTRS or alias.name == "*":
                        self.issues.append(f"Line {node.lineno}: Restricted os import '{alias.name}'")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id in self.BANNED_CALLS:
                self.issues.append(f"Line {node.lineno}: Restricted function '{node.func.id}()'")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr in self.BANNED_OS_ATTRS:
                self.issues.append(f"Line {node.lineno}: Restricted method '.{node.func.attr}()'")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in self.BANNED_DUNDERS:
            self.issues.append(f"Line {node.lineno}: Restricted attribute '{node.attr}'")
        if isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in self.BANNED_OS_ATTRS:
                self.issues.append(f"Line {node.lineno}: Restricted 'os.{node.attr}'")
        if isinstance(node.value, ast.Name) and node.value.id == "sys":
            if node.attr in {"modules", "executable", "path", "argv", "platform"}:
                self.issues.append(f"Line {node.lineno}: Restricted 'sys.{node.attr}'")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            low_val = node.value.lower()
            for pattern in self.SUSPICIOUS_STRINGS:
                if pattern in low_val:
                    self.issues.append(f"Line {node.lineno}: Blocked host/system probe '{pattern}'")
        self.generic_visit(node)


def scan_python_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        scanner = AdvancedASTScanner()
        scanner.visit(tree)
        return len(scanner.issues) == 0, scanner.issues
    except SyntaxError as e:
        return False, [f"SyntaxError at line {e.lineno}: {e.msg}"]
    except Exception as e:
        return False, [f"Parse error: {str(e)}"]


# ==========================================
# PROCESS ENGINE & LIVE LOG STREAMER
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
    except Exception as e:
        log_buffer.append(f"[Stream Error] {str(e)}")
    finally:
        exit_code = proc.poll()
        log_buffer.append(f"[{datetime.now().strftime('%H:%M:%S')}] Process exited with code {exit_code}")
        sync_session_process(uid, proc.pid, filename, f"EXITED ({exit_code})", log_buffer)


def start_user_script(user_id, file_path, filename, startup_inputs=None):
    uid = str(user_id)
    with process_lock:
        if is_user_process_running(uid):
            return False, "You already have 1 active file running! Stop it first."

        user_work_dir = os.path.join(WORKSPACE_DIR, uid)
        os.makedirs(user_work_dir, exist_ok=True)
        target_script = os.path.join(user_work_dir, "main.py")
        log_file_path = os.path.join(user_work_dir, "console.log")

        shutil.copyfile(file_path, target_script)
        with open(log_file_path, "w", encoding="utf-8") as lf:
            lf.write(f"=== Session Started: {filename} ({datetime.now()}) ===\n")

        # Isolated environment variables
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
            "log_file": log_file_path
        }

        sync_session_process(uid, proc.pid, filename, "RUNNING", log_buffer)

        t = threading.Thread(
            target=_stream_stdout_worker,
            args=(uid, proc, log_buffer, filename, log_file_path),
            daemon=True
        )
        t.start()
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
        info["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Terminated by user/admin.")
        sync_session_process(uid, proc.pid, info["filename"], "STOPPED", info["logs"])
        return True


# ==========================================
# COLOURFUL INLINE KEYBOARDS
# ==========================================
def main_menu_kb(user_id):
    running = is_user_process_running(user_id)
    term_btn_text = "🟢 🖥️ Live Terminal (ACTIVE)" if running else "🔵 🖥️ Live Terminal (IDLE)"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🟣 📤 Upload .PY File", callback_data="upload_file"),
        InlineKeyboardButton(term_btn_text, callback_data="terminal_view")
    )
    kb.add(
        InlineKeyboardButton("🟠 ⚙️ Profile & Stats", callback_data="profile_settings"),
        InlineKeyboardButton("🟡 💎 Buy Premium", callback_data="buy_premium")
    )
    if int(user_id) == OWNER_ID:
        kb.add(InlineKeyboardButton("🔴 👑 Owner Control Panel", callback_data="owner_panel"))
    return kb


def terminal_kb(user_id):
    running = is_user_process_running(user_id)
    kb = InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            InlineKeyboardButton("🟢 🔄 Refresh Logs", callback_data="terminal_view"),
            InlineKeyboardButton("🔵 ⌨️ Send Input (stdin)", callback_data="terminal_stdin")
        )
        kb.add(
            InlineKeyboardButton("🟣 📥 Export Full Log", callback_data="terminal_export"),
            InlineKeyboardButton("🔴 🛑 Stop Script", callback_data="stop_process")
        )
    else:
        kb.add(
            InlineKeyboardButton("🟢 🔄 Refresh Status", callback_data="terminal_view"),
            InlineKeyboardButton("🟣 📥 Export Last Log", callback_data="terminal_export")
        )
        kb.add(InlineKeyboardButton("🟠 📤 Upload New File", callback_data="upload_file"))

    kb.add(InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_home"))
    return kb


def owner_panel_kb():
    mgmt = load_management()
    bot_status = mgmt["settings"].get("bot_status", "ON")
    toggle_btn = (
        "🟢 Bot Status: ON (Tap to Turn OFF)"
        if bot_status == "ON"
        else "🔴 Bot Status: OFF (Tap to Turn ON)"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton(toggle_btn, callback_data="admin_toggle_bot"))
    kb.add(
        InlineKeyboardButton("🔵 👥 Users & Active Jobs", callback_data="admin_users"),
        InlineKeyboardButton("🟣 🖥️ Server Health", callback_data="admin_server")
    )
    kb.add(
        InlineKeyboardButton("🟠 📂 Download User Files", callback_data="admin_files"),
        InlineKeyboardButton("🟡 💳 Set UPI & Price", callback_data="admin_edit_upi")
    )
    kb.add(
        InlineKeyboardButton("🟢 💎 Toggle User Premium", callback_data="admin_toggle_prem"),
        InlineKeyboardButton("🔴 🛑 Kill User Process", callback_data="admin_kill_proc")
    )
    kb.add(InlineKeyboardButton("📦 Export management.json & session.json", callback_data="admin_export_json"))
    kb.add(InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_home"))
    return kb


# ==========================================
# DASHBOARD & TERMINAL RENDER HELPERS
# ==========================================
def build_dashboard_text(user):
    uid = str(user.id)
    mgmt = load_management()
    u_info = mgmt["users"].get(uid, {})
    running = is_user_process_running(uid)
    proc_info = runtime_processes.get(uid)

    plan_badge = "💎 Premium" if u_info.get("is_premium") else "🆓 Free Tier (1 File Cap)"
    slot_badge = f"🟢 Running (<code>{proc_info['filename']}</code>)" if running else "⚪ Idle (0 / 1 Used)"

    return (
        f"✨ <b>Python Cloud File Hoster</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.first_name} (<code>{user.id}</code>)\n"
        f"🏅 <b>Membership:</b> {plan_badge}\n"
        f"⚡ <b>Slot Status:</b> {slot_badge}\n"
        f"📂 <b>Total Uploads:</b> {u_info.get('uploads_count', 0)}\n"
        f"🛡️ <b>AST Protection:</b> 🟢 Active\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Select a colourful action button below:</i>"
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
        f"🖥️ <b>Internal Live Terminal</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>File:</b> <code>{fname}</code> | 🆔 <b>PID:</b> <code>{pid}</code>\n"
        f"📡 <b>State:</b> {state_badge} | ⏱️ <b>Uptime:</b> {uptime}s\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<pre>{logs_escaped[-3200:]}</pre>"
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

    # --- HOME ---
    if data == "back_home":
        set_user_state(user_id, "IDLE")
        bot.edit_message_text(
            build_dashboard_text(user),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu_kb(user_id)
        )

    # --- UPLOAD FILE ---
    elif data == "upload_file":
        if is_user_process_running(user_id):
            bot.answer_callback_query(
                call.id,
                "⚠️ You already have 1 file running! Stop it from the Live Terminal first.",
                show_alert=True
            )
            return

        set_user_state(user_id, "AWAITING_FILE")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔴 ✖️ Cancel Upload", callback_data="back_home"))
        bot.edit_message_text(
            "🟣 <b>Upload Python Script</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📤 Please send your <code>.py</code> file as a document now.\n\n"
            "<i>🛡️ Your file will be scanned by the AST Security Engine and logged in management.json.</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    # --- START IMMEDIATELY WITHOUT STARTUP INPUTS ---
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
        term_text = build_terminal_text(user_id)
        bot.edit_message_text(
            term_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=terminal_kb(user_id)
        )

    # --- LIVE TERMINAL ---
    elif data == "terminal_view":
        set_user_state(user_id, "IDLE")
        term_text = build_terminal_text(user_id)
        if not term_text:
            bot.answer_callback_query(call.id, "⚪ No script has been executed in this session yet.", show_alert=True)
            return

        try:
            bot.edit_message_text(
                term_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=terminal_kb(user_id)
            )
            bot.answer_callback_query(call.id, "🟢 Terminal refreshed")
        except Exception:
            bot.answer_callback_query(call.id, "🟢 Logs are already up to date")

    elif data == "terminal_stdin":
        if not is_user_process_running(user_id):
            bot.answer_callback_query(call.id, "🔴 Script is not currently running.", show_alert=True)
            return
        set_user_state(user_id, "AWAITING_LIVE_STDIN")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Back to Terminal", callback_data="terminal_view"))
        bot.edit_message_text(
            "🔵 <b>⌨️ Send Live Input (stdin)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Type and send the line of text you want to pass directly into your running script:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "terminal_export":
        log_path = os.path.join(WORKSPACE_DIR, uid, "console.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, "⚠️ No log file found yet.", show_alert=True)
            return
        with open(log_path, "rb") as doc:
            bot.send_document(
                call.message.chat.id,
                doc,
                caption=f"🟣 <b>Full Terminal Log</b> (User <code>{user_id}</code>)"
            )
        bot.answer_callback_query(call.id, "📥 Log exported!")

    elif data == "stop_process":
        if stop_user_script(user_id):
            bot.answer_callback_query(call.id, "🛑 Script terminated!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "⚪ Process already stopped.")
        term_text = build_terminal_text(user_id)
        if term_text:
            bot.edit_message_text(term_text, call.message.chat.id, call.message.message_id, reply_markup=terminal_kb(user_id))

    # --- PROFILE & SETTINGS ---
    elif data == "profile_settings":
        u_info = mgmt["users"].get(uid, {})
        running = is_user_process_running(user_id)
        sess_state = get_user_state(user_id).get("step", "IDLE")

        prof_text = (
            f"🟠 <b>⚙️ User Profile & Settings</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Name:</b> {u_info.get('first_name', user.first_name)}\n"
            f"🪪 <b>User ID:</b> <code>{user_id}</code>\n"
            f"🏷️ <b>Username:</b> @{u_info.get('username', 'N/A')}\n"
            f"🏅 <b>Account Tier:</b> {'💎 Premium' if u_info.get('is_premium') else '🆓 Free'}\n"
            f"📊 <b>Total Files Uploaded:</b> {u_info.get('uploads_count', 0)}\n"
            f"🟢 <b>Active Running Slot:</b> {'1 / 1' if running else '0 / 1'}\n"
            f"🧭 <b>Session State:</b> <code>{sess_state}</code>\n"
            f"📅 <b>Registered On:</b> {u_info.get('joined_at', 'N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🟣 📤 Upload File", callback_data="upload_file"),
            InlineKeyboardButton("🟡 💎 Upgrade Plan", callback_data="buy_premium")
        )
        kb.add(InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_home"))
        bot.edit_message_text(prof_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    # --- BUY PREMIUM ---
    elif data == "buy_premium":
        price = mgmt["settings"].get("premium_price", "₹199 / Month")
        upi = mgmt["settings"].get("upi_id", "yourname@upi")
        prem_text = (
            f"🟡 <b>💎 Upgrade to Premium Membership</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Plan Price:</b> <code>{price}</code>\n"
            f"🏦 <b>Official UPI ID:</b> <code>{upi}</code>\n\n"
            f"<b>How to Activate:</b>\n"
            f"1️⃣ Complete the payment to the UPI ID above.\n"
            f"2️⃣ Send the screenshot along with your User ID (<code>{user_id}</code>) to the Bot Owner."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_home"))
        bot.edit_message_text(prem_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    # ==========================================
    # OWNER PANEL HANDLERS
    # ==========================================
    elif data == "owner_panel":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "IDLE")
        total_users = len(mgmt["users"])
        total_files = len(mgmt["files"])
        active_count = sum(1 for k in runtime_processes if is_user_process_running(k))

        panel_text = (
            f"🔴 <b>👑 Owner Administration Panel</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>Registered Users:</b> {total_users}\n"
            f"🟢 <b>Running Scripts:</b> {active_count}\n"
            f"📂 <b>Stored Files in JSON:</b> {total_files}\n"
            f"💳 <b>Current UPI:</b> <code>{mgmt['settings'].get('upi_id')}</code>\n"
            f"💰 <b>Current Price:</b> <code>{mgmt['settings'].get('premium_price')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        bot.edit_message_text(panel_text, call.message.chat.id, call.message.message_id, reply_markup=owner_panel_kb())

    elif data == "admin_toggle_bot":
        if user_id != OWNER_ID:
            return
        with json_lock:
            mgmt = load_management()
            curr = mgmt["settings"].get("bot_status", "ON")
            new_st = "OFF" if curr == "ON" else "ON"
            mgmt["settings"]["bot_status"] = new_st
            save_management(mgmt)
        bot.answer_callback_query(call.id, f"Bot status changed to {new_st}")
        call.data = "owner_panel"
        handle_callbacks(call)

    elif data == "admin_users":
        if user_id != OWNER_ID:
            return
        users_dict = mgmt["users"]
        lines = []
        for u_id_str, u_obj in list(users_dict.items())[-15:]:
            running_flag = "🟢 RUNNING" if is_user_process_running(u_id_str) else "⚪ IDLE"
            prem_flag = "💎" if u_obj.get("is_premium") else "🆓"
            lines.append(
                f"• {prem_flag} <code>{u_id_str}</code> (@{u_obj.get('username')}) | "
                f"Files: {u_obj.get('uploads_count', 0)} | {running_flag}"
            )
        user_list_str = "\n".join(lines) if lines else "No registered users yet."

        text = (
            f"🔵 <b>👥 Registered Users & Active Jobs</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{user_list_str}"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data="owner_panel"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "admin_server":
        if user_id != OWNER_ID:
            return
        cpu = psutil.cpu_percent(interval=0.3)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        boot_dt = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M")

        srv_text = (
            f"🟣 <b>🖥️ Server Hardware & JSON Status</b>\n"
            f"<i>(Host IP & credentials strictly masked)</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>CPU Load:</b> {cpu}%\n"
            f"🧠 <b>RAM Usage:</b> {ram.percent}% ({ram.used // (1024**2)} MB / {ram.total // (1024**2)} MB)\n"
            f"💾 <b>Disk Usage:</b> {disk.percent}% ({disk.used // (1024**3)} GB / {disk.total // (1024**3)} GB)\n"
            f"🐍 <b>Python Engine:</b> {sys.version.split()[0]}\n"
            f"⏱️ <b>Server Boot Time:</b> {boot_dt}"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data="owner_panel"))
        bot.edit_message_text(srv_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "admin_files":
        if user_id != OWNER_ID:
            return
        files_list = mgmt.get("files", [])[-10:]
        kb = InlineKeyboardMarkup(row_width=1)
        for f_entry in reversed(files_list):
            fid = f_entry["file_id"]
            badge = "🟢" if f_entry.get("status") == "SAFE" else "🔴"
            kb.add(
                InlineKeyboardButton(
                    f"{badge} #{fid} | {f_entry['filename']} (User: {f_entry['user_id']})",
                    callback_data=f"dlfile_{fid}"
                )
            )
        kb.add(InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data="owner_panel"))
        bot.edit_message_text(
            "🟠 <b>📂 Recent Uploaded Files (Tap to Download)</b>\n"
            "<i>All files uploaded by users are indexed in management.json:</i>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data.startswith("dlfile_"):
        if user_id != OWNER_ID:
            return
        fid = int(data.split("_")[1])
        target = next((x for x in mgmt.get("files", []) if x["file_id"] == fid), None)
        if target and os.path.exists(target["saved_path"]):
            with open(target["saved_path"], "rb") as doc:
                bot.send_document(
                    call.message.chat.id,
                    doc,
                    caption=(
                        f"📥 <b>File ID:</b> #{fid}\n"
                        f"📄 <b>Name:</b> <code>{target['filename']}</code>\n"
                        f"👤 <b>User ID:</b> <code>{target['user_id']}</code>\n"
                        f"🛡️ <b>Scan Status:</b> {target.get('status')}\n"
                        f"📅 <b>Uploaded:</b> {target['uploaded_at']}"
                    )
                )
            bot.answer_callback_query(call.id, "📥 File sent!")
        else:
            bot.answer_callback_query(call.id, "❌ File not found on disk.", show_alert=True)

    elif data == "admin_edit_upi":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_UPI")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Cancel", callback_data="owner_panel"))
        bot.edit_message_text(
            "🟡 <b>💳 Update Premium Price & UPI ID</b>\n\n"
            "Send the new details in this exact format:\n"
            "<code>Price | UPI_ID</code>\n\n"
            "Example:\n<code>₹249 / Month | merchant@okaxis</code>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "admin_toggle_prem":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_PREM_UID")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Cancel", callback_data="owner_panel"))
        bot.edit_message_text(
            "🟢 <b>💎 Grant / Revoke User Premium</b>\n\n"
            "Send the numeric <b>User ID</b> of the user whose Premium status you want to toggle:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "admin_kill_proc":
        if user_id != OWNER_ID:
            return
        set_user_state(user_id, "AWAITING_KILL_UID")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Cancel", callback_data="owner_panel"))
        bot.edit_message_text(
            "🔴 <b>🛑 Force-Stop User Script</b>\n\n"
            "Send the numeric <b>User ID</b> whose active process you want to terminate:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    elif data == "admin_export_json":
        if user_id != OWNER_ID:
            return
        for jf in (MANAGEMENT_FILE, SESSION_FILE):
            if os.path.exists(jf):
                with open(jf, "rb") as f:
                    bot.send_document(call.message.chat.id, f, caption=f"📦 <b>JSON Database:</b> <code>{jf}</code>")
        bot.answer_callback_query(call.id, "📦 Exported management.json & session.json")


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
        bot.reply_to(message, "🔴 Bot is currently offline.")
        return

    if is_user_process_running(user_id):
        bot.reply_to(
            message,
            "⚠️ <b>You already have 1 script running!</b>\nStop it from the Live Terminal before uploading another.",
            reply_markup=main_menu_kb(user_id)
        )
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        bot.reply_to(message, "❌ Only Python (<code>.py</code>) files are supported.", reply_markup=main_menu_kb(user_id))
        return

    file_info = bot.get_file(doc.file_id)
    downloaded_bytes = bot.download_file(file_info.file_path)

    safe_name = os.path.basename(doc.file_name)
    timestamp = int(time.time())
    saved_path = os.path.join(STORAGE_DIR, f"{user_id}_{timestamp}_{safe_name}")

    with open(saved_path, "wb") as f:
        f.write(downloaded_bytes)

    # AST Security Verification
    is_safe, issues = scan_python_file(saved_path)
    scan_status = "SAFE" if is_safe else "BLOCKED"

    # Save record in management.json
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
            f"🛡️ <b>AST Security Scanner Blocked File!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 <b>File:</b> <code>{safe_name}</code>\n"
            f"Your script attempted restricted system/host operations:\n\n"
            f"{issue_lines}",
            reply_markup=main_menu_kb(user_id)
        )
        return

    # Save session state in session.json
    set_user_state(
        user_id,
        "AWAITING_STARTUP_INPUT",
        temp_file=saved_path,
        temp_filename=safe_name
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🟢 ▶️ Start Immediately (No Inputs)", callback_data="run_no_input"))
    kb.add(InlineKeyboardButton("🔴 ✖️ Cancel", callback_data="back_home"))

    bot.reply_to(
        message,
        f"✅ <b>AST Security Check Passed!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 <b>File:</b> <code>{safe_name}</code>\n\n"
        f"⌨️ If your script requires startup <code>input()</code> values, reply with each input on a new line.\n"
        f"Or tap <b>🟢 Start Immediately</b> below:",
        reply_markup=kb
    )


# ==========================================
# SESSION-DRIVEN TEXT ROUTER
# ==========================================
@bot.message_handler(content_types=["text"])
def handle_text_states(message):
    user = message.from_user
    user_id = user.id
    text = (message.text or "").strip()

    register_or_update_user(user)
    state_obj = get_user_state(user_id)
    step = state_obj.get("step", "IDLE")

    # 1. Startup Inputs for Uploaded Script
    if step == "AWAITING_STARTUP_INPUT":
        saved_path = state_obj.get("temp_file")
        safe_name = state_obj.get("temp_filename")
        set_user_state(user_id, "IDLE")

        inputs_list = None if text.lower() == "none" else text.splitlines()
        ok, status_msg = start_user_script(user_id, saved_path, safe_name, startup_inputs=inputs_list)
        if not ok:
            bot.reply_to(message, f"⚠️ {status_msg}", reply_markup=main_menu_kb(user_id))
            return

        time.sleep(0.8)
        term_text = build_terminal_text(user_id)
        bot.reply_to(message, term_text, reply_markup=terminal_kb(user_id))
        return

    # 2. Live Stdin Input to Running Process
    elif step == "AWAITING_LIVE_STDIN":
        set_user_state(user_id, "IDLE")
        ok, res = send_live_stdin(user_id, text)
        time.sleep(0.5)
        term_text = build_terminal_text(user_id) or f"ℹ️ {res}"
        bot.reply_to(message, term_text, reply_markup=terminal_kb(user_id))
        return

    # 3. Owner: Update UPI & Price
    elif step == "AWAITING_UPI" and user_id == OWNER_ID:
        if "|" not in text:
            bot.reply_to(message, "❌ Invalid format. Send as: <code>Price | UPI_ID</code>")
            return
        price, upi = [x.strip() for x in text.split("|", 1)]
        with json_lock:
            mgmt = load_management()
            mgmt["settings"]["premium_price"] = price
            mgmt["settings"]["upi_id"] = upi
            save_management(mgmt)
        set_user_state(user_id, "IDLE")
        bot.reply_to(
            message,
            f"✅ <b>Updated in management.json!</b>\n💰 <b>Price:</b> {price}\n🏦 <b>UPI:</b> {upi}",
            reply_markup=owner_panel_kb()
        )
        return

    # 4. Owner: Toggle User Premium
    elif step == "AWAITING_PREM_UID" and user_id == OWNER_ID:
        target_uid = text.strip()
        with json_lock:
            mgmt = load_management()
            if target_uid not in mgmt["users"]:
                bot.reply_to(message, "❌ User ID not found in <code>management.json</code>.", reply_markup=owner_panel_kb())
                set_user_state(user_id, "IDLE")
                return
            curr_val = mgmt["users"][target_uid].get("is_premium", False)
            mgmt["users"][target_uid]["is_premium"] = not curr_val
            save_management(mgmt)
        set_user_state(user_id, "IDLE")
        badge = "💎 GRANTED" if not curr_val else "🆓 REVOKED"
        bot.reply_to(
            message,
            f"✅ Premium status for <code>{target_uid}</code>: <b>{badge}</b>",
            reply_markup=owner_panel_kb()
        )
        return

    # 5. Owner: Force-Kill User Process
    elif step == "AWAITING_KILL_UID" and user_id == OWNER_ID:
        target_uid = text.strip()
        stopped = stop_user_script(target_uid)
        set_user_state(user_id, "IDLE")
        msg_str = f"🛑 Terminated process for user <code>{target_uid}</code>." if stopped else f"⚪ No running script for <code>{target_uid}</code>."
        bot.reply_to(message, msg_str, reply_markup=owner_panel_kb())
        return

    # Default Fallback
    bot.reply_to(
        message,
        build_dashboard_text(user),
        reply_markup=main_menu_kb(user_id)
    )


if __name__ == "__main__":
    init_json_stores()
    print("✅ Hoster Bot initialized with management.json & session.json...")
    bot.infinity_polling(skip_pending=True)
