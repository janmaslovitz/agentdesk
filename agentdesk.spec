# PyInstaller spec: `pyinstaller agentdesk.spec` (build_exe.ps1 does this). Onedir, no console, icon embedded.
# Only code, the web page and the icon go in; data/, accounts/ and .env are never collected.
from PyInstaller.utils.hooks import collect_all

datas = [("agentdesk/web/index.html", "agentdesk/web"), ("agentdesk/web/agentdesk.ico", "agentdesk/web")]
binaries, hiddenimports = [], ["webview.platforms.edgechromium", "webview.platforms.winforms"]
for pkg in ("claude_agent_sdk", "webview"):  # claude_agent_sdk/_bundled/claude.exe is the CLI the runs use
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(["run_agentdesk.py"], pathex=["."], datas=datas, binaries=binaries, hiddenimports=hiddenimports,
             excludes=["pytest", "tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AgentDesk", console=False, icon="agentdesk/web/agentdesk.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="AgentDesk")
