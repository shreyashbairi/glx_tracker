# GLX Tracker

Desktop time tracker for Globalex's ERPNext (replaces the Hubstaff app). It runs in the menu bar (Mac) or the system tray (Windows) and sends time and activity to the `glx_timetrack` app on https://erp.globalex.me.

## What it records
One general work timer per person (not tied to Tasks or Projects).
- Timer start / pause (break) / stop.
- Per second: whether the keyboard and/or mouse was used (counts only, never which keys).
- The app in front (name only) and, for browsers, the website domain (e.g. `sellercentral.amazon.ae`, never the full address or page content).
- No screenshots, no window titles.

Data is grouped into 10-minute blocks, as in Hubstaff. If the computer is offline, everything is kept and sent later.

## Idle time
After 5 minutes without keyboard or mouse input (set in ERPNext → Timetrack Settings) the time counts as idle. When you come back you choose **Keep**, **Discard**, or **Discard and stop timer**. If you don't come back for 30 minutes, the timer stops at the moment you left. Time while the computer sleeps is never counted.

## Install (employees)

### Mac
1. Download `GLX-Tracker-macOS-arm64.zip` (Apple Silicon, M1/M2/M3/M4) or `-intel.zip`, double-click it, and drag **GLX Tracker** into **Applications**.
2. The app is not signed by Apple. Open it once; when macOS blocks it, go to **System Settings → Privacy & Security**, scroll down and click **Open Anyway**. (If macOS says the app is "damaged", run `xattr -dr com.apple.quarantine "/Applications/GLX Tracker.app"` in Terminal and open it again.)
3. Sign in with your ERPNext email and password.
4. When macOS asks, allow **Input Monitoring** (System Settings → Privacy & Security → Input Monitoring → GLX Tracker on). Without it, keyboard activity shows as 0.
5. The first time you use Chrome/Safari/Edge with the timer on, macOS asks whether GLX Tracker may control the browser. Click **OK** (this is how it reads the domain).
6. The clock icon appears in the menu bar. It starts automatically at login.

After an update, macOS may ask for these permissions again.

### Windows
1. Download `GLX-Tracker-Windows.zip`, right-click → **Extract All…** to `C:\Users\<you>\AppData\Local\GLX Tracker` (or any folder you keep).
2. Open `GLX Tracker.exe`. If SmartScreen warns, click **More info → Run anyway**.
3. Sign in with your ERPNext email and password. The clock icon sits in the tray (click ^ if hidden). It starts automatically at login.

## Using it
- Press **Start** in the window or the tray/menu-bar menu when you start work. **Pause** starts a break (not counted as work); **Resume** continues; **Stop** ends the day.
- You can also start / pause / stop from ERPNext on the **Time Tracker** page. The app reacts within ~15 seconds.
- Closing the window keeps tracking; use **Quit** in the icon menu to exit (this stops the timer).

## Development
```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[build,test]"
python tests/test_tracker.py            # state machine tests (fake clock, no GUI)
python -m glx_tracker --simulate --debug  # GUI with a simulated computer (works on Linux)
python -m glx_tracker.headless --server http://localhost:8000 --email a@b --password x --replay 30
pyinstaller packaging/glx_tracker.spec --noconfirm   # build for the current OS
```
Builds for macOS and Windows are made by GitHub Actions (`.github/workflows/build.yml`) when a tag `v*` is pushed; the zips are attached to the GitHub release.

Files: settings and log in `~/Library/Application Support/GLX Tracker` (Mac) or `%APPDATA%\GLX Tracker` (Windows); the sign-in token is in the Keychain / Windows Credential Manager.
