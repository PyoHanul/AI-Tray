import os, json, time, threading, subprocess, webbrowser, requests
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item

# ── 1. 설정 관리 및 초기화 ───────────────────────────────────────────────
CONFIG_FILE = "services_config.json"
DEFAULT_CONFIG = {"services": [
    {"id": "ollama", "name": "Ollama API", "command": "ollama serve",
     "check_url": "http://127.0.0.1:11434/api/tags", "work_dir": "", "show_web_btn": False, "color": "#00ff00"},
    {"id": "open_webui", "name": "Open WebUI", "command": "open-webui serve",
     "check_url": "http://127.0.0.1:8080", "work_dir": "", "show_web_btn": True, "color": "#00ffff"},
    {"id": "comfyui", "name": "ComfyUI", "command": "python main.py",
     "check_url": "http://127.0.0.1:8188", "work_dir": "C:\\ComfyUI_windows_portable\\ComfyUI",
     "show_web_btn": True, "color": "#ff9900"},
]}

def load_config():  # 설정 JSON 파일 읽기
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for s in cfg.get("services", []):
            s.setdefault("show_web_btn", bool(s.get("check_url")))
        return cfg
    except Exception:
        return DEFAULT_CONFIG

def save_config(cfg):  # 현재 설정 데이터 저장
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)

# ── 2. 전역 변수 및 스타일 상수 ───────────────────────────────────────────
CHECK_INTERVAL = 1
runtime_data = {}
http_session = requests.Session()
tray_icon = root = None
config = load_config()

active_right_view = "console"
active_console_id = None
console_text_widgets, console_frames, console_scrollbars, console_tab_buttons, main_tab_buttons = {}, {}, {}, {}, {}
paned = left_panel = right_panel = frame_right_settings = None
scroll_left = left_canvas = None

PAD, HEADER_HEIGHT = 6, 40
COLOR_ACTIVE_BG, COLOR_INACTIVE_BG = "#007acc", "#2b2b2b"
COLOR_ACTIVE_FG, COLOR_INACTIVE_FG = "#ffffff", "#888888"
FONT_HEADER_TAB = ("Malgun Gothic", 9, "bold")
FONT_UI, FONT_UI_S, FONT_UI_SB = ("Malgun Gothic", 9), ("Malgun Gothic", 8), ("Malgun Gothic", 8, "bold")
TRAY_STATE_COLOR = {"ONLINE": (46, 204, 113, 255), "STARTING": (241, 196, 15, 255)}
TRAY_OFFLINE_COLOR = (231, 76, 60, 255)

def set_tab_active_style(btn, is_active):  # 상단 탭 활성화/비활성화 디자인 적용
    if is_active:
        btn.config(bg=COLOR_ACTIVE_BG, fg=COLOR_ACTIVE_FG, activebackground=COLOR_ACTIVE_BG, activeforeground=COLOR_ACTIVE_FG)
    else:
        btn.config(bg=COLOR_INACTIVE_BG, fg=COLOR_INACTIVE_FG, activebackground="#383838", activeforeground="#ffffff")

def create_header_tab_button(parent, text, command):  # 상단 탭 버튼 생성자
    return tk.Button(parent, text=text, font=FONT_HEADER_TAB, bg=COLOR_INACTIVE_BG, fg=COLOR_INACTIVE_FG,
                      bd=0, relief="flat", cursor="hand2", command=command)

def mk_btn(parent, text, bg, fg, command=None, font=FONT_UI_S, **kw):  # 공용 버튼 생성 헬퍼
    return tk.Button(parent, text=text, font=font, bg=bg, fg=fg, bd=0, cursor="hand2", command=command, **kw)

# ── 3. 프로세스 제어 및 실시간 로그 수집 ─────────────────────────────────
def enqueue_output(stream, service_id):  # 프로세스 표준 출력을 비동기로 읽어 콘솔로 전달
    for line in iter(stream.readline, ''):
        if not line:
            break
        if root and root.winfo_exists():
            root.after(0, append_console_text, service_id, line)
    stream.close()

def append_console_text(service_id, text):  # 콘솔 위젯 로그 기록
    widget = console_text_widgets.get(service_id)
    if widget and widget.winfo_exists():
        widget.configure(state='normal')
        widget.insert(tk.END, text)
        widget.see(tk.END)
        widget.configure(state='disabled')

def check_service_health(check_url, proc=None):  # HTTP 체크 또는 프로세스 생존 확인
    if check_url and check_url.strip():
        try:
            return http_session.get(check_url, timeout=1).status_code < 400
        except Exception:
            return False
    return proc is not None and proc.poll() is None

def start_custom_service(service_id):  # 커스텀 서비스 비동기 실행
    srv = next((s for s in config["services"] if s["id"] == service_id), None)
    if not srv:
        return
    rt = runtime_data[service_id]
    rt["state"] = "STARTING"

    if service_id in console_text_widgets:
        w = console_text_widgets[service_id]
        w.configure(state='normal'); w.delete('1.0', tk.END); w.configure(state='disabled')

    try:
        proc = subprocess.Popen(srv["command"], shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, cwd=srv["work_dir"].strip() or None,
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
        rt["proc"] = proc
        threading.Thread(target=enqueue_output, args=(proc.stdout, service_id), daemon=True).start()
    except Exception as e:
        rt["state"] = "OFFLINE"
        messagebox.showerror("실행 에러", f"[{srv['name']}] 실행 실패:\n{e}")
        return

    for _ in range(30):
        if rt["state"] != "STARTING":
            break
        if check_service_health(srv["check_url"], rt["proc"]):
            rt["state"] = "ONLINE"
            break
        time.sleep(1)

    if rt["state"] == "STARTING":
        rt["state"] = "ONLINE" if (rt["proc"] and rt["proc"].poll() is None) else "OFFLINE"

def stop_custom_service(service_id):  # 프로세스 강제 종료
    srv = next((s for s in config["services"] if s["id"] == service_id), None)
    rt = runtime_data[service_id]
    rt["state"] = "STOPPING"
    if rt["proc"]:
        try:
            subprocess.run(f"taskkill /F /T /PID {rt['proc'].pid}", shell=True, capture_output=True)
        except Exception as e:
            messagebox.showerror("종료 에러", f"[{srv['name']}] 종료 실패:\n{e}")
    rt["proc"], rt["state"] = None, "OFFLINE"

# ── 4. 트레이 및 백그라운드 모니터링 ─────────────────────────────────────
def create_dynamic_tray_icon():  # 트레이 아이콘 동적 그리기
    img = Image.new("RGBA", (64, 64), (30, 30, 30, 255))
    draw = ImageDraw.Draw(img)
    cnt = len(config["services"])
    if cnt > 0:
        bh = max(4, int(48 / cnt))
        for i, srv in enumerate(config["services"]):
            st = runtime_data.get(srv["id"], {}).get("state", "OFFLINE")
            y = 8 + i * (bh + 2)
            if y + bh <= 56:
                draw.rectangle([8, y, 56, y + bh], fill=TRAY_STATE_COLOR.get(st, TRAY_OFFLINE_COLOR))
    return img

def monitor_loop():  # 주기적 Health Check 스레드
    global tray_icon
    while True:
        try:
            summary = []
            for srv in config["services"]:
                sid = srv["id"]
                if sid not in runtime_data:
                    continue
                rt = runtime_data[sid]
                if rt["state"] != "STARTING":
                    rt["state"] = "ONLINE" if check_service_health(srv["check_url"], rt["proc"]) else "OFFLINE"
                summary.append(f"• {srv['name']}: {rt['state']}")

            if tray_icon:
                tray_icon.icon = create_dynamic_tray_icon()
                tray_icon.title = "AI System Manager\n" + "\n".join(summary[:5])
            if root and root.winfo_exists():
                root.after(0, update_gui_elements)
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL)

def update_gui_elements():  # UI 상태 실시간 갱신
    for srv in config["services"]:
        sid = srv["id"]
        if sid not in runtime_data:
            continue
        rt = runtime_data[sid]
        st = rt["state"]
        if not rt.get("lbl_status"):
            continue

        if st == "ONLINE":
            rt["lbl_status"].config(text="● RUNNING", fg="#2ecc71")
            rt["btn_toggle"].config(text="OFF", bg="#d32f2f", fg="#ffffff", state="normal",
                command=lambda s=sid: threading.Thread(target=stop_custom_service, args=(s,), daemon=True).start())
        elif st == "STARTING":
            rt["lbl_status"].config(text="⏳ LOADING", fg="#f1c40f")
            rt["btn_toggle"].config(text="...", bg="#424242", fg="#aaaaaa", state="disabled")
        else:
            rt["lbl_status"].config(text="● STOPPED", fg="#e57373")
            rt["btn_toggle"].config(text="ON", bg="#2e7d32", fg="#ffffff", state="normal",
                command=lambda s=sid: threading.Thread(target=start_custom_service, args=(s,), daemon=True).start())

# ── 5. UI 화면 제어 및 통합 레이아웃 구축 ────────────────────────────────
def show_right_view(view_type):  # 우측 메인 패널 화면 교체
    global active_right_view
    active_right_view = view_type
    if view_type == "dashboard":
        show_right_view("console")
        return

    is_settings = (view_type == "settings")
    set_tab_active_style(main_tab_buttons["dashboard"], not is_settings)
    set_tab_active_style(main_tab_buttons["settings"], is_settings)

    if is_settings:
        for btn in console_tab_buttons.values():
            set_tab_active_style(btn, False)
        for w in console_text_widgets.values():
            w.pack_forget()
        for sb in console_scrollbars.values():
            sb.pack_forget()
        for f in console_frames.values():
            f.pack_forget()
        frame_right_settings.pack(fill="both", expand=True)
    else:
        frame_right_settings.pack_forget()
        select_console_tab(active_console_id or (config["services"][0]["id"] if config["services"] else None))

def select_console_tab(service_id):  # 우측 콘솔 탭 선택 활성화
    global active_console_id, active_right_view
    if not service_id:
        return
    active_console_id, active_right_view = service_id, "console"
    set_tab_active_style(main_tab_buttons["dashboard"], True)
    set_tab_active_style(main_tab_buttons["settings"], False)
    for sid, btn in console_tab_buttons.items():
        set_tab_active_style(btn, sid == service_id)

    frame_right_settings.pack_forget()
    for sid, widget in console_text_widgets.items():
        frame, scrollbar = console_frames[sid], console_scrollbars[sid]
        if sid == service_id:
            frame.pack(fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            widget.pack(side="left", fill="both", expand=True)
        else:
            widget.pack_forget()
            scrollbar.pack_forget()
            frame.pack_forget()

def bind_scroll(canvas, axis, targets, drag=True):
    """마우스 휠(+선택적 드래그) 스크롤을 canvas에 바인딩하는 공용 헬퍼"""
    scroll = canvas.xview_scroll if axis == "x" else canvas.yview_scroll
    pos_attr = "x_root" if axis == "x" else "y_root"

    def on_wheel(e):
        if e.delta:
            scroll(int(-1 * (e.delta / 120)), "units")
        elif e.num == 4:
            scroll(-1, "units")
        elif e.num == 5:
            scroll(1, "units")

    drag_data = {"p": 0}
    def on_press(e):
        drag_data["p"] = getattr(e, pos_attr)
    def on_motion(e):
        p = getattr(e, pos_attr)
        dp, drag_data["p"] = drag_data["p"] - p, p
        if dp:
            scroll(int(dp / 5), "units")

    for w in targets:
        w.bind("<MouseWheel>", on_wheel)
        if drag:
            w.bind("<ButtonPress-1>", on_press, add="+")
            w.bind("<B1-Motion>", on_motion, add="+")

def collect_widgets(widget):  # 위젯과 하위 모든 자식을 재귀 수집
    out = [widget]
    for c in widget.winfo_children():
        out.extend(collect_widgets(c))
    return out

def rebuild_left_service_cards():  # 좌측 패널 서비스 카드 목록을 config 순서에 맞춰 재구축
    if not scroll_left:
        return
    for widget in scroll_left.winfo_children():
        widget.destroy()

    for srv in config["services"]:
        sid = srv["id"]
        runtime_data.setdefault(sid, {"proc": None, "state": "OFFLINE", "lbl_status": None, "btn_toggle": None})

        card = tk.Frame(scroll_left, bg="#252526", bd=1, relief="solid")
        card.pack(fill="x", expand=True, pady=(0, PAD))

        f_head = tk.Frame(card, bg="#252526")
        f_head.pack(fill="x", expand=True, padx=PAD, pady=(PAD, PAD // 2))
        tk.Label(f_head, text=srv["name"], font=("Malgun Gothic", 9, "bold"), bg="#252526", fg="#ffffff").pack(side="left")
        lbl_status = tk.Label(f_head, text="● CHECK", font=("Consolas", 8, "bold"), bg="#252526", fg="#f1c40f")
        lbl_status.pack(side="right")

        f_btns = tk.Frame(card, bg="#252526")
        f_btns.pack(fill="x", expand=True, padx=PAD, pady=(PAD // 2, PAD))

        has_open = srv.get("show_web_btn", True) and srv["check_url"].strip()
        cols = ["open", "console", "toggle"] if has_open else ["console", "toggle"]

        for idx, btn_type in enumerate(cols):
            f_btns.columnconfigure(idx, weight=1)
            if btn_type == "open":
                btn = mk_btn(f_btns, "열기", "#007acc", "#ffffff", lambda u=srv["check_url"]: webbrowser.open(u))
            elif btn_type == "console":
                btn = mk_btn(f_btns, "콘솔", "#3a3d41", "#ffffff", lambda s=sid: select_console_tab(s))
            else:
                btn = mk_btn(f_btns, "ON", "#2e7d32", "#ffffff", None, font=("Malgun Gothic", 8, "bold"))
                runtime_data[sid]["btn_toggle"] = btn
            btn.grid(row=0, column=idx, sticky="ew",
                     padx=(0 if idx == 0 else PAD // 2, 0 if idx == len(cols) - 1 else PAD // 2))

        runtime_data[sid]["lbl_status"] = lbl_status
        bind_scroll(left_canvas, "y", collect_widgets(card), drag=True)

def build_gui_layout():  # 전체 UI 레이아웃 구축 (좌/우 패널, 콘솔, 설정 화면)
    global paned, left_panel, right_panel, frame_right_settings, left_canvas, scroll_left

    style = ttk.Style()
    style.theme_use('clam')
    style.configure("Vertical.TScrollbar", gripcount=0, background="#2d2d2d", darkcolor="#1e1e1e",
                     lightcolor="#3f3f3f", troughcolor="#1e1e1e", bordercolor="#1e1e1e", arrowcolor="#aaaaaa")
    style.map("Vertical.TScrollbar", background=[('active', '#3f3f3f'), ('pressed', '#007acc')])

    paned = tk.PanedWindow(root, orient="horizontal", bg="#1e1e1e", bd=0, sashwidth=2)
    paned.pack(fill="both", expand=True)

    # [좌측 패널]
    left_panel = tk.Frame(paned, bg="#181818", width=240)
    paned.add(left_panel, minsize=230)

    left_header = tk.Frame(left_panel, bg="#181818", height=HEADER_HEIGHT)
    left_header.pack(fill="x", side="top", padx=PAD, pady=(PAD, 0))
    left_header.pack_propagate(False)

    btn_dash = create_header_tab_button(left_header, "통합 대시보드", lambda: show_right_view("dashboard"))
    btn_dash.pack(side="left", fill="both", expand=True, padx=(0, PAD // 2))
    main_tab_buttons["dashboard"] = btn_dash

    btn_sett = create_header_tab_button(left_header, "서비스 설정", lambda: show_right_view("settings"))
    btn_sett.pack(side="left", fill="both", expand=True, padx=(PAD // 2, 0))
    main_tab_buttons["settings"] = btn_sett

    left_canvas = tk.Canvas(left_panel, bg="#181818", highlightthickness=0, bd=0)
    scroll_left = tk.Frame(left_canvas, bg="#181818")
    canvas_win = left_canvas.create_window((0, 0), window=scroll_left, anchor="nw")
    scroll_left.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
    left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfig(canvas_win, width=e.width))
    left_canvas.pack(fill="both", expand=True, padx=PAD, pady=PAD)

    rebuild_left_service_cards()

    # [우측 패널]
    right_panel = tk.Frame(paned, bg="#181818")
    paned.add(right_panel, minsize=400)

    header_canvas = tk.Canvas(right_panel, bg="#181818", height=HEADER_HEIGHT, highlightthickness=0, bd=0)
    header_canvas.pack(fill="x", side="top", padx=PAD, pady=(PAD, 0))
    header_canvas.pack_propagate(False)

    header_scroll_frame = tk.Frame(header_canvas, bg="#181818")
    canvas_tab_win = header_canvas.create_window((0, 0), window=header_scroll_frame, anchor="nw")

    def _update_header_scrollregion(event):
        header_canvas.configure(scrollregion=header_canvas.bbox("all"))
        header_canvas.itemconfig(canvas_tab_win, height=HEADER_HEIGHT)
    header_scroll_frame.bind("<Configure>", _update_header_scrollregion)
    bind_scroll(header_canvas, "x", [header_canvas, header_scroll_frame], drag=False)

    for srv in config["services"]:
        sid = srv["id"]
        btn = create_header_tab_button(header_scroll_frame, f"{srv['name']} 콘솔", command=lambda s=sid: select_console_tab(s))
        btn.config(padx=14)
        btn.pack(side="left", fill="y", padx=(0, PAD // 2), pady=2)
        bind_scroll(header_canvas, "x", [btn], drag=True)
        console_tab_buttons[sid] = btn

    right_body = tk.Frame(right_panel, bg="#0c0c0c")
    right_body.pack(fill="both", expand=True, padx=PAD, pady=PAD)

    for srv in config["services"]:
        sid = srv["id"]
        f_container = tk.Frame(right_body, bg="#0c0c0c")
        txt_widget = tk.Text(f_container, bg="#0c0c0c", fg=srv.get("color", "#00ff00"), insertbackground="white",
                              font=("Consolas", 9), state='disabled', bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(f_container, orient="vertical", command=txt_widget.yview)
        txt_widget.configure(yscrollcommand=scrollbar.set)
        console_text_widgets[sid] = txt_widget
        console_frames[sid] = f_container
        console_scrollbars[sid] = scrollbar

    frame_right_settings = tk.Frame(right_body, bg="#1e1e1e")
    mk_btn(frame_right_settings, "+ 새 커스텀 서비스 추가", "#2e7d32", "#ffffff",
           lambda: open_service_editor_ui(root), font=FONT_HEADER_TAB, pady=8).pack(fill="x", padx=PAD, pady=PAD)

    # [서비스 순서 변경 - 깜빡임 없는 인라인 재배치 및 좌측 카드 동기화]
    service_items_frames = []
    reorder_state = {"dragged_idx": None}
    frame_list = tk.Frame(frame_right_settings, bg="#1e1e1e")
    frame_list.pack(fill="both", expand=True, padx=PAD, pady=0)

    def create_service_row_items():  # 설정 창 내 서비스 리스트 UI 구성
        for widget in frame_list.winfo_children():
            widget.destroy()
        service_items_frames.clear()

        def on_drag_start(idx, event):
            reorder_state["dragged_idx"] = idx

        def on_drag_release(event):
            dragged_idx = reorder_state["dragged_idx"]
            if dragged_idx is not None:
                mouse_y = event.y_root
                target_idx = dragged_idx
                for i, f_item in enumerate(service_items_frames):
                    y1 = f_item.winfo_rooty()
                    if y1 <= mouse_y <= y1 + f_item.winfo_height():
                        target_idx = i
                        break
                if dragged_idx != target_idx:
                    config["services"].insert(target_idx, config["services"].pop(dragged_idx))
                    save_config(config)
                    create_service_row_items()
                    rebuild_left_service_cards()
            reorder_state["dragged_idx"] = None

        for idx, srv in enumerate(config["services"]):
            sid = srv["id"]
            f_item = tk.Frame(frame_list, bg="#252526", bd=1, relief="solid", cursor="hand2")
            f_item.pack(fill="x", pady=(0, PAD))
            service_items_frames.append(f_item)

            lbl_drag_handle = tk.Label(f_item, text="≡", font=("Malgun Gothic", 11, "bold"), bg="#252526", fg="#888888", cursor="fleur")
            lbl_drag_handle.pack(side="left", padx=(PAD, 0), pady=PAD)
            lbl_info = tk.Label(f_item, text=f"{srv['name']} [{srv['command']}]", font=FONT_UI, bg="#252526", fg="#ffffff", cursor="fleur")
            lbl_info.pack(side="left", padx=PAD, pady=PAD)

            mk_btn(f_item, "삭제", "#d32f2f", "#ffffff", lambda s=sid: remove_service_ui(s),
                   font=FONT_UI_SB, width=5).pack(side="right", padx=(2, PAD))
            mk_btn(f_item, "수정", "#3a3d41", "#ffffff", lambda s=sid: open_service_editor_ui(root, s),
                   font=FONT_UI_SB, width=5).pack(side="right", padx=2)

            for widget_elem in (f_item, lbl_drag_handle, lbl_info):
                widget_elem.bind("<ButtonPress-1>", lambda e, i=idx: on_drag_start(i, e))
                widget_elem.bind("<ButtonRelease-1>", on_drag_release)

    create_service_row_items()

    root.after(20, lambda: paned.sash_place(0, 240, 0))
    if config["services"]:
        select_console_tab(config["services"][0]["id"])

# ── 6. 서비스 편집 및 모달 Dialog ─────────────────────────────────────────
def open_service_editor_ui(parent_window, service_id=None):  # 서비스 추가/수정 Toplevel 팝업
    is_edit = service_id is not None
    target_srv = next((s for s in config["services"] if s["id"] == service_id), None) if is_edit else None

    top = tk.Toplevel(parent_window)
    top.title("서비스 수정" if is_edit else "새 서비스 추가")
    top.geometry("440x360")
    top.configure(bg="#252526")
    top.grab_set(); top.resizable(False, False)

    def make_entry(text, default_val=""):
        f = tk.Frame(top, bg="#252526")
        f.pack(fill="x", padx=20, pady=6)
        tk.Label(f, text=text, bg="#252526", fg="#cccccc", width=14, anchor="w", font=FONT_UI_SB).pack(side="left")
        ent = tk.Entry(f, bg="#333333", fg="#ffffff", insertbackground="white", bd=1, relief="solid")
        ent.insert(0, default_val)
        ent.pack(side="right", fill="x", expand=True, ipady=3)
        return ent

    ent_name = make_entry("서비스 이름:", target_srv["name"] if is_edit else "")
    ent_cmd = make_entry("실행 명령어:", target_srv["command"] if is_edit else "")
    ent_url = make_entry("접속 URL (선택):", target_srv["check_url"] if is_edit else "")
    ent_dir = make_entry("작업 디렉터리:", target_srv["work_dir"] if is_edit else "")

    chk_frame = tk.Frame(top, bg="#252526")
    chk_frame.pack(fill="x", padx=20, pady=6)
    var_web_btn = tk.BooleanVar(value=target_srv["show_web_btn"] if is_edit else True)
    tk.Checkbutton(chk_frame, text=" [열기] 버튼 표시", variable=var_web_btn, bg="#252526", fg="#ffffff",
                   selectcolor="#333333", activebackground="#252526", activeforeground="#ffffff", font=FONT_UI).pack(side="left")

    def save_service():
        name, cmd = ent_name.get().strip(), ent_cmd.get().strip()
        if not name or not cmd:
            messagebox.showwarning("입력 오류", "서비스 이름과 실행 명령어는 필수 항목입니다.")
            return
        data = {"name": name, "command": cmd, "check_url": ent_url.get().strip(),
                "work_dir": ent_dir.get().strip(), "show_web_btn": var_web_btn.get()}
        if is_edit:
            target_srv.update(data)
        else:
            data.update({"id": f"custom_{int(time.time())}", "color": "#00ff00"})
            config["services"].append(data)
        save_config(config)
        top.destroy()
        rebuild_gui()
        show_right_view("settings")

    mk_btn(top, "저장 완료" if is_edit else "추가하기", "#007acc", "#ffffff", save_service,
           font=FONT_HEADER_TAB, pady=8).pack(fill="x", padx=20, pady=15)

def remove_service_ui(service_id):  # 서비스 삭제
    srv = next((s for s in config["services"] if s["id"] == service_id), None)
    if srv and messagebox.askyesno("삭제 확인", f"[{srv['name']}] 서비스를 목록에서 삭제하시겠습니까?"):
        config["services"] = [s for s in config["services"] if s["id"] != service_id]
        save_config(config)
        rebuild_gui()
        show_right_view("settings")

def rebuild_gui():  # UI 재구축
    console_text_widgets.clear(); console_frames.clear(); console_scrollbars.clear()
    console_tab_buttons.clear(); main_tab_buttons.clear()
    for w in root.winfo_children():
        w.destroy()
    build_gui_layout()

def open_gui():
    if root:
        root.deiconify(); root.lift(); root.focus_force()

def hide_gui():
    root.withdraw()

def exit_app():
    if tray_icon:
        tray_icon.stop()
    if root:
        root.after(0, root.destroy)

def run_tray():
    global tray_icon
    menu = pystray.Menu(item('대시보드 열기', open_gui, default=True), pystray.Menu.SEPARATOR, item('시스템 종료', exit_app))
    tray_icon = pystray.Icon("AISystemMonitor", create_dynamic_tray_icon(), "AI System Manager", menu)
    tray_icon.run()

# ── 7. 프로그램 진입점 ─────────────────────────────────────────────────
def main():
    global root
    root = tk.Tk()
    root.title("Multi-Service Controller")
    root.geometry("1280x720")
    root.configure(bg="#1e1e1e")
    root.protocol("WM_DELETE_WINDOW", hide_gui)

    build_gui_layout()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=run_tray, daemon=True).start()
    root.mainloop()

if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        with open("crash.log", "a", encoding="utf-8") as f:
            f.write(f"Error: {err}\n")