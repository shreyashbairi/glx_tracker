"""GLX Tracker desktop UI (PySide6): tray / menu-bar icon, small main window, idle and sign-in dialogs."""

import logging
import sys
import threading
import time
import webbrowser

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
	QApplication,
	QDialog,
	QFormLayout,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QListWidget,
	QListWidgetItem,
	QMenu,
	QMessageBox,
	QPushButton,
	QSystemTrayIcon,
	QVBoxLayout,
	QWidget,
)

from glx_tracker import APP_NAME, DEFAULT_SERVER, __version__
from glx_tracker.api import Api, ApiError
from glx_tracker.config import Config, data_dir, setup_logging
from glx_tracker.outbox import Outbox
from glx_tracker.platforms.base import get_platform
from glx_tracker.syncer import Syncer
from glx_tracker.tracker import Tracker

log = logging.getLogger("glx_tracker.ui")

COLORS = {"running": "#2f9e6b", "paused": "#e8a33d", "stopped": "#8a8f98", "idle": "#e8c33d", "offline": "#c9534f"}


def hms(secs: int) -> str:
	secs = max(0, int(secs))
	return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def hm(secs: int) -> str:
	secs = max(0, int(secs))
	return f"{secs // 3600}h {secs % 3600 // 60:02d}m"


def dot_icon(color: str, ring: bool = False) -> QIcon:
	pm = QPixmap(64, 64)
	pm.fill(Qt.transparent)
	p = QPainter(pm)
	p.setRenderHint(QPainter.Antialiasing)
	p.setBrush(QColor(color))
	p.setPen(Qt.NoPen)
	p.drawEllipse(8, 8, 48, 48)
	p.setBrush(QColor("white"))
	# clock hands
	p.drawRoundedRect(29, 16, 6, 18, 3, 3)
	p.drawRoundedRect(29, 29, 16, 6, 3, 3)
	p.end()
	return QIcon(pm)


class Bridge(QObject):
	"""Thread-safe hop from tracker/sync threads to the UI thread."""

	changed = Signal()
	notice = Signal(str)
	idle = Signal(object)
	tasks_loaded = Signal(list)
	signed_out = Signal()


# ---------------------------------------------------------------------------- dialogs


class LoginDialog(QDialog):
	def __init__(self, config: Config, platform, parent=None):
		super().__init__(parent)
		self.config = config
		self.platform = platform
		self.result_info = None
		self.setWindowTitle(f"{APP_NAME} — sign in")
		self.setMinimumWidth(380)
		form = QFormLayout()
		self.server = QLineEdit(config.get("server") or DEFAULT_SERVER)
		self.email = QLineEdit(config.get("email") or "")
		self.password = QLineEdit()
		self.password.setEchoMode(QLineEdit.Password)
		form.addRow("ERPNext address", self.server)
		form.addRow("Email", self.email)
		form.addRow("Password", self.password)
		self.msg = QLabel("Use the same email and password as for ERPNext.\nYour password is not stored on this computer.")
		self.msg.setWordWrap(True)
		self.msg.setStyleSheet("color: gray")
		self.btn = QPushButton("Sign in")
		self.btn.setDefault(True)
		self.btn.clicked.connect(self.submit)
		lay = QVBoxLayout(self)
		lay.addLayout(form)
		lay.addWidget(self.msg)
		lay.addWidget(self.btn)

	def submit(self):
		server = self.server.text().strip().rstrip("/")
		if server and "://" not in server:
			server = "https://" + server
		email = self.email.text().strip()
		pwd = self.password.text()
		if not (server and email and pwd):
			self.msg.setText("Please fill in all three fields.")
			return
		self.btn.setEnabled(False)
		self.msg.setText("Signing in…")
		QApplication.processEvents()
		try:
			api = Api(server)
			os_name, os_version = self.platform.os_info()
			info = api.register(email, pwd, self.config.get("device_id"), self.platform.hostname(), os_name, os_version)
		except ApiError as e:
			self.msg.setText(str(e))
			self.msg.setStyleSheet("color: #c9534f")
			self.btn.setEnabled(True)
			return
		self.config.set("server", server)
		self.config.set("email", email)
		self.config.set("full_name", info.get("full_name"))
		self.config.set_token(info["device"], info["token"])
		self.result_info = info
		self.accept()


class IdleDialog(QDialog):
	def __init__(self, tracker: Tracker, parent=None):
		super().__init__(parent)
		self.tracker = tracker
		self.setWindowTitle(f"{APP_NAME} — you were away")
		self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
		self.setMinimumWidth(420)
		self.label = QLabel()
		self.label.setWordWrap(True)
		f = QFont()
		f.setPointSize(f.pointSize() + 1)
		self.label.setFont(f)
		keep = QPushButton("Keep idle time")
		discard = QPushButton("Discard idle time")
		stop = QPushButton("Discard and stop timer")
		discard.setDefault(True)
		keep.clicked.connect(lambda: self.answer("keep"))
		discard.clicked.connect(lambda: self.answer("discard"))
		stop.clicked.connect(lambda: self.answer("stop"))
		row = QHBoxLayout()
		for b in (keep, discard, stop):
			row.addWidget(b)
		hint = QLabel("Keep = you were working away from the keyboard (a call, a meeting, reading paper documents).")
		hint.setWordWrap(True)
		hint.setStyleSheet("color: gray")
		lay = QVBoxLayout(self)
		lay.addWidget(self.label)
		lay.addWidget(hint)
		lay.addLayout(row)
		self.timer = QTimer(self)
		self.timer.timeout.connect(self.refresh)

	def show_for(self, idle):
		self.idle = idle
		self.refresh()
		self.timer.start(5000)
		self.show()
		self.raise_()
		self.activateWindow()

	def refresh(self):
		if self.tracker.idle is None:
			self.timer.stop()
			self.hide()
			return
		start = self.tracker.idle.start
		end = int(self.tracker.idle.returned or time.time())
		mins = max(1, (end - start) // 60)
		what = "Your computer was asleep" if self.tracker.idle.reason == "sleep" else "No keyboard or mouse input"
		self.label.setText(
			f"{what} from {time.strftime('%H:%M', time.localtime(start))} to "
			f"{time.strftime('%H:%M', time.localtime(end))} ({mins} min).\nWhat should happen with this time?"
		)

	def answer(self, action):
		self.timer.stop()
		self.tracker.resolve_idle(action)
		self.hide()


# ---------------------------------------------------------------------------- main window


class MainWindow(QWidget):
	def __init__(self, app: "TrackerApp"):
		super().__init__()
		self.app = app
		self.setWindowTitle(APP_NAME)
		self.setMinimumSize(380, 560)
		big = QFont()
		big.setPointSize(big.pointSize() + 14)
		big.setBold(True)

		self.state_lbl = QLabel()
		self.task_lbl = QLabel()
		self.task_lbl.setWordWrap(True)
		self.clock_lbl = QLabel("0:00:00")
		self.clock_lbl.setFont(big)
		self.today_lbl = QLabel()
		self.today_lbl.setStyleSheet("color: gray")

		self.start_btn = QPushButton("Start")
		self.start_btn.setMinimumHeight(34)
		self.pause_btn = QPushButton("Pause")
		self.pause_btn.setMinimumHeight(34)
		self.start_btn.clicked.connect(self.on_start_stop)
		self.pause_btn.clicked.connect(self.on_pause)
		btns = QHBoxLayout()
		btns.addWidget(self.start_btn, 2)
		btns.addWidget(self.pause_btn, 1)

		self.search = QLineEdit()
		self.search.setPlaceholderText("Search tasks…")
		self.search.textChanged.connect(self.fill_tasks)
		reload_btn = QPushButton("↻")
		reload_btn.setFixedWidth(34)
		reload_btn.setToolTip("Reload tasks from ERPNext")
		reload_btn.clicked.connect(self.app.load_tasks)
		srow = QHBoxLayout()
		srow.addWidget(self.search)
		srow.addWidget(reload_btn)
		self.list = QListWidget()
		self.list.itemDoubleClicked.connect(lambda _i: self.start_selected())
		self.switch_btn = QPushButton("Start on selected task")
		self.switch_btn.clicked.connect(self.start_selected)

		self.sync_lbl = QLabel()
		self.sync_lbl.setStyleSheet("color: gray; font-size: 11px")
		self.sync_lbl.setWordWrap(True)
		link = QLabel(f'<a href="#">Open Time Tracker in ERPNext</a> · v{__version__}')
		link.setStyleSheet("font-size: 11px")
		link.linkActivated.connect(lambda _l: self.app.open_web())

		lay = QVBoxLayout(self)
		lay.addWidget(self.state_lbl)
		lay.addWidget(self.clock_lbl)
		lay.addWidget(self.task_lbl)
		lay.addWidget(self.today_lbl)
		lay.addLayout(btns)
		lay.addSpacing(8)
		lay.addLayout(srow)
		lay.addWidget(self.list, 1)
		lay.addWidget(self.switch_btn)
		lay.addWidget(self.sync_lbl)
		lay.addWidget(link)
		self.tasks = []

	# ------------------------------------------------------------------
	def set_tasks(self, tasks):
		self.tasks = tasks
		self.fill_tasks()

	def fill_tasks(self):
		q = self.search.text().strip().lower()
		current = self.app.tracker.status().get("task")
		self.list.clear()
		none = QListWidgetItem("— No task —")
		none.setData(Qt.UserRole, None)
		if not q:
			self.list.addItem(none)
		for t in self.tasks:
			text = f"{t.get('subject')}"
			if t.get("project_name"):
				text += f"   ·  {t['project_name']}"
			if q and q not in text.lower() and q not in t["name"].lower():
				continue
			item = QListWidgetItem(("▶ " if t["name"] == current else "") + text)
			item.setData(Qt.UserRole, t)
			item.setToolTip(t["name"] + (f"  due {t['due']}" if t.get("due") else ""))
			self.list.addItem(item)

	def selected_task(self):
		item = self.list.currentItem()
		return item.data(Qt.UserRole) if item else None

	def start_selected(self):
		if self.list.currentItem() is None:
			return
		self.app.start(self.selected_task())

	def on_start_stop(self):
		st = self.app.tracker.state
		if st == "running":
			self.app.tracker.stop("User")
		elif st == "paused":
			self.app.tracker.stop("User")
		else:
			self.app.start(self.selected_task() if self.list.currentItem() else self.app.last_task())

	def on_pause(self):
		if self.app.tracker.state == "running":
			self.app.tracker.pause()
		elif self.app.tracker.state == "paused":
			self.app.tracker.resume()

	def refresh(self):
		tr = self.app.tracker
		st = tr.status()
		state = st["state"]
		if state == "running" and st["idle"]:
			self.state_lbl.setText("● Idle — waiting for your answer")
			color = COLORS["idle"]
		else:
			self.state_lbl.setText({"running": "● Tracking", "paused": "● On a break", "stopped": "● Timer stopped"}[state])
			color = COLORS[state]
		self.state_lbl.setStyleSheet(f"color: {color}; font-weight: bold")
		self.clock_lbl.setText(hms(tr.session_seconds()) if state == "running" else "0:00:00")
		self.task_lbl.setText(st["subject"] or ("Break" if state == "paused" else "Pick a task below and press Start"))
		self.today_lbl.setText(f"Today: {hm(tr.today_seconds())}")
		self.start_btn.setText("Stop" if state in ("running", "paused") else "Start")
		self.pause_btn.setText("Resume" if state == "paused" else "Pause")
		self.pause_btn.setEnabled(state in ("running", "paused"))
		self.switch_btn.setText("Switch to selected task" if state == "running" else "Start on selected task")
		s = self.app.syncer
		queued = self.app.outbox.count()
		if s and s.online:
			self.sync_lbl.setText("Connected · all data sent" if not queued else f"Connected · sending {queued} item(s)…")
		elif s:
			self.sync_lbl.setText(f"Offline — {queued} item(s) waiting. Tracking continues. ({s.last_error or ''})")

	def closeEvent(self, ev):
		# closing the window keeps the tracker running in the tray / menu bar
		ev.ignore()
		self.hide()


# ---------------------------------------------------------------------------- app


class TrackerApp:
	def __init__(self, argv, simulate=False, debug=False):
		self.qt = QApplication(argv)
		self.qt.setApplicationName(APP_NAME)
		self.qt.setQuitOnLastWindowClosed(False)
		setup_logging(debug)
		log.info("starting %s %s", APP_NAME, __version__)

		self.lock = QLockFile(str(data_dir() / "tracker.lock"))
		if not self.lock.tryLock(100):
			QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running (look for the clock icon).")
			sys.exit(0)

		if sys.platform == "darwin":
			try:
				from AppKit import NSApplication

				NSApplication.sharedApplication().setActivationPolicy_(1)  # menu-bar app, no Dock icon
			except Exception:
				pass

		self.config = Config()
		self.platform = get_platform(simulate=simulate)
		self.outbox = Outbox(data_dir() / "outbox.db")
		self.tracker = Tracker(self.platform, self.outbox, self.config)
		self.tracker.recover()
		self.syncer = None
		self.api = None
		self.bridge = Bridge()
		self.idle_dialog = IdleDialog(self.tracker)
		self._quitting = False

		self.tracker.on_change = self.bridge.changed.emit
		self.tracker.on_notice = self.bridge.notice.emit
		self.tracker.on_idle_return = self.bridge.idle.emit
		self.bridge.changed.connect(self.refresh)
		self.bridge.notice.connect(self.notify)
		self.bridge.idle.connect(self.idle_dialog.show_for)
		self.bridge.signed_out.connect(self.on_signed_out)

		self.window = MainWindow(self)
		self.bridge.tasks_loaded.connect(self.window.set_tasks)
		self.make_tray()

		self.tick_timer = QTimer()
		self.tick_timer.timeout.connect(self.on_tick)
		self.tick_timer.start(1000)
		self.qt.aboutToQuit.connect(self.on_quit)

	# ------------------------------------------------------------------ lifecycle
	def run(self):
		if not self.config.signed_in and not self.sign_in():
			return 0
		self.connect()
		self.check_permissions()
		self.apply_autostart()
		self.window.show()
		return self.qt.exec()

	def sign_in(self) -> bool:
		dlg = LoginDialog(self.config, self.platform)
		if dlg.exec() != QDialog.Accepted:
			return False
		if dlg.result_info:
			self.tracker.apply_settings(dlg.result_info.get("settings"))
		return True

	def connect(self):
		self.api = Api(self.config.get("server"), self.config.get("device_id"), self.config.get_token())
		if self.syncer:
			self.syncer.stop()
		self.syncer = Syncer(self.api, self.tracker, self.outbox, self.config)
		self.syncer.on_status = self.bridge.changed.emit
		self.syncer.on_auth_lost = self.bridge.signed_out.emit
		self.syncer.start()
		self.platform.start()
		self.url_thread = threading.Thread(target=self.url_loop, daemon=True, name="glx-url")
		self.url_thread.start()
		self.load_tasks()

	def on_signed_out(self):
		was_running = self.tracker.state != "stopped"
		if was_running:
			self.tracker.stop("User")
		self.notify("You were signed out of ERPNext. Please sign in again.")
		if self.sign_in():
			self.connect()

	def on_quit(self):
		if self._quitting:
			return
		self._quitting = True
		if self.tracker.state != "stopped":
			self.tracker.stop("Quit")
		if self.syncer and self.api:
			try:
				self.api.timeout = 5
				self.syncer.sync_once()
			except Exception:
				pass
			self.syncer.stop()
		self.platform.stop()

	def quit(self):
		if self.tracker.state == "running":
			if (
				QMessageBox.question(None, APP_NAME, "The timer is running. Stop it and quit?")
				!= QMessageBox.Yes
			):
				return
		self.qt.quit()

	# ------------------------------------------------------------------ periodic
	def on_tick(self):
		try:
			self.tracker.tick()
		except Exception:
			log.exception("tick failed")
		if self.syncer:
			while not self.syncer.commands.empty():
				self.run_command(self.syncer.commands.get())
		self.refresh()

	def url_loop(self):
		while not self._quitting:
			try:
				fg = self.tracker.fg
				if self.tracker.state == "running" and fg.browser and self.tracker.track_urls:
					self.tracker.set_url(fg.app, self.platform.browser_url(fg))
			except Exception:
				log.debug("url lookup failed", exc_info=True)
			time.sleep(2)

	def run_command(self, cmd: dict):
		action = cmd.get("action")
		log.info("command from ERPNext: %s", cmd)
		try:
			if action == "start":
				task = None
				if cmd.get("task"):
					task = next((t for t in self.window.tasks if t["name"] == cmd["task"]), None) or {
						"name": cmd["task"],
						"subject": cmd["task"],
					}
				self.start(task)
			elif action == "stop":
				self.tracker.stop("Web")
			elif action == "pause":
				self.tracker.pause()
			elif action == "resume":
				self.tracker.resume()
		finally:
			self.syncer.command_done(cmd.get("id") or 0)

	# ------------------------------------------------------------------ actions
	def start(self, task):
		if self.syncer and self.syncer.settings.get("require_task") and not (task and task.get("name")):
			self.notify("Please pick a task first.")
			self.window.show()
			return
		self.tracker.start(task)
		self.syncer and self.syncer.wake()

	def last_task(self):
		recent = self.config.get("recent_tasks") or []
		return recent[0] if recent else None

	def load_tasks(self):
		def work():
			try:
				tasks = self.api.tasks() or []
				self.bridge.tasks_loaded.emit(tasks)
			except Exception as e:
				log.warning("could not load tasks: %s", e)

		threading.Thread(target=work, daemon=True).start()

	def open_web(self):
		webbrowser.open(f"{self.config.get('server')}/desk/time-tracker")

	def notify(self, text: str):
		log.info("notice: %s", text)
		if QSystemTrayIcon.supportsMessages():
			self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.Information, 8000)
		else:
			QMessageBox.information(None, APP_NAME, text)

	def check_permissions(self):
		missing = self.platform.missing_permissions()
		if not missing or self.config.get("perm_prompted"):
			return
		self.config.set("perm_prompted", True)
		QMessageBox.information(
			None,
			APP_NAME,
			"To measure keyboard activity, allow GLX Tracker under\n"
			"System Settings → Privacy & Security → Input Monitoring.\n\n"
			"Mouse activity, apps and websites are measured without it.",
		)
		try:
			self.platform.request_permissions()
		except Exception:
			pass

	def apply_autostart(self):
		try:
			if getattr(sys, "frozen", False):
				cmd = [sys.executable]
			else:
				cmd = [sys.executable, "-m", "glx_tracker"]
			self.platform.set_autostart(bool(self.config.get("autostart", True)), cmd)
		except Exception:
			log.warning("autostart setup failed", exc_info=True)

	def toggle_autostart(self, checked):
		self.config.set("autostart", bool(checked))
		self.apply_autostart()

	def sign_out(self):
		if QMessageBox.question(None, APP_NAME, "Sign out of ERPNext on this computer?") != QMessageBox.Yes:
			return
		self.tracker.stop("User")
		try:
			self.syncer.sync_once()
			self.api.signout()
		except Exception:
			pass
		self.config.set_token(self.config.get("device_id"), None)
		self.qt.quit()

	# ------------------------------------------------------------------ tray
	def make_tray(self):
		self.icons = {k: dot_icon(v) for k, v in COLORS.items()}
		self.tray = QSystemTrayIcon(self.icons["stopped"])
		self.tray.setToolTip(APP_NAME)
		menu = QMenu()
		self.m_status = QAction("")
		self.m_status.setEnabled(False)
		self.m_today = QAction("")
		self.m_today.setEnabled(False)
		self.m_start = QAction("Start timer")
		self.m_start.triggered.connect(lambda: self.window.on_start_stop())
		self.m_pause = QAction("Pause")
		self.m_pause.triggered.connect(lambda: self.window.on_pause())
		m_open = QAction("Open GLX Tracker…")
		m_open.triggered.connect(self.show_window)
		m_web = QAction("Open Time Tracker in ERPNext")
		m_web.triggered.connect(self.open_web)
		self.m_auto = QAction("Start at login")
		self.m_auto.setCheckable(True)
		self.m_auto.setChecked(bool(self.config.get("autostart", True)))
		self.m_auto.toggled.connect(self.toggle_autostart)
		m_signout = QAction("Sign out")
		m_signout.triggered.connect(self.sign_out)
		m_quit = QAction("Quit")
		m_quit.triggered.connect(self.quit)
		for a in (self.m_status, self.m_today):
			menu.addAction(a)
		menu.addSeparator()
		for a in (self.m_start, self.m_pause, m_open, m_web):
			menu.addAction(a)
		menu.addSeparator()
		for a in (self.m_auto, m_signout, m_quit):
			menu.addAction(a)
		self._menu_actions = [self.m_status, self.m_today, self.m_start, self.m_pause, m_open, m_web, self.m_auto, m_signout, m_quit]
		self.tray.setContextMenu(menu)
		self.tray.activated.connect(self.on_tray_activated)
		self.tray.show()
		self._tray_menu = menu

	def on_tray_activated(self, reason):
		if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) and sys.platform != "darwin":
			self.show_window()

	def show_window(self):
		self.window.show()
		self.window.raise_()
		self.window.activateWindow()

	def refresh(self):
		st = self.tracker.status()
		state = st["state"]
		key = "idle" if state == "running" and st["idle"] else state
		if self.syncer and not self.syncer.online and state == "stopped":
			key = "offline"
		self.tray.setIcon(self.icons[key])
		today = hm(self.tracker.today_seconds())
		if state == "running":
			self.m_status.setText(f"Tracking: {st['subject']}")
			self.tray.setToolTip(f"{APP_NAME} — {st['subject']} · today {today}")
		elif state == "paused":
			self.m_status.setText("On a break")
			self.tray.setToolTip(f"{APP_NAME} — on a break · today {today}")
		else:
			self.m_status.setText("Timer stopped")
			self.tray.setToolTip(f"{APP_NAME} — stopped · today {today}")
		self.m_today.setText(f"Today: {today}")
		self.m_start.setText("Stop timer" if state in ("running", "paused") else "Start timer")
		self.m_pause.setText("Resume" if state == "paused" else "Pause")
		self.m_pause.setEnabled(state in ("running", "paused"))
		if self.window.isVisible():
			self.window.refresh()


def main(argv=None):
	argv = list(sys.argv if argv is None else argv)
	simulate = "--simulate" in argv
	debug = "--debug" in argv
	app = TrackerApp([a for a in argv if a not in ("--simulate", "--debug")], simulate=simulate, debug=debug)
	return app.run()
