# PyInstaller spec: `pyinstaller agentdesk.spec` (build_exe.ps1 on Windows, build_unix.sh on macOS / Linux do this).
# Onedir, no console. Windows: AgentDesk.exe with the .ico embedded. macOS: also AgentDesk.app (icon from
# agentdesk/web/agentdesk.icns if there is one). Linux: the dist/AgentDesk folder.
# Only code, the web page and the icon go in; data/, accounts/ and .env are never collected.
import os
import re
import sys

from PyInstaller.utils.hooks import collect_all

WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
ICO, ICNS = "agentdesk/web/agentdesk.ico", "agentdesk/web/agentdesk.icns"
with open("agentdesk/__init__.py", encoding="utf-8") as f:
    VERSION = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M).group(1)

datas = [("agentdesk/web/index.html", "agentdesk/web"), (ICO, "agentdesk/web")]  # the .ico is also the window icon
binaries, hiddenimports = [], []
if WIN:
    hiddenimports += ["webview.platforms.edgechromium", "webview.platforms.winforms", "pystray._win32"]
elif MAC:
    hiddenimports += ["webview.platforms.cocoa", "pystray._darwin"]
else:
    hiddenimports += ["webview.platforms.gtk", "pystray._xorg", "pystray._appindicator", "pystray._gtk"]
if os.path.exists("agentdesk/_buildinfo.py"):  # written by tools/release.py buildinfo
    hiddenimports.append("agentdesk._buildinfo")
for pkg in ("claude_agent_sdk", "webview"):  # claude_agent_sdk/_bundled/claude(.exe) is the CLI the runs use
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# Linux: pywebview's window is GTK 3 + WebKit2GTK 4.1 (through PyGObject).
hooksconfig = {} if WIN or MAC else {"gi": {"module-versions": {"Gtk": "3.0", "WebKit2": "4.1"}}}

a = Analysis(["run_agentdesk.py"], pathex=["."], datas=datas, binaries=binaries, hiddenimports=hiddenimports,
             hooksconfig=hooksconfig, excludes=["pytest", "tkinter"])
pyz = PYZ(a.pure)
icon = ICO if WIN else (ICNS if MAC and os.path.exists(ICNS) else None)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AgentDesk", console=False, icon=icon,
          version="version_info.txt" if WIN and os.path.exists("version_info.txt") else None)
coll = COLLECT(exe, a.binaries, a.datas, name="AgentDesk")
if MAC:
    app = BUNDLE(coll, name="AgentDesk.app", icon=icon, bundle_identifier="io.github.janmaslovitz.agentdesk", version=VERSION,
                 info_plist={"CFBundleShortVersionString": VERSION, "NSHighResolutionCapable": True})
