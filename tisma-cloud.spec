# PyInstaller spec of the tisma-cloud executable: master server, computing node and administration in one program.
# Built by .github/workflows/server.yml for Windows and Linux: pip install ".[master,exe]" && pyinstaller tisma-cloud.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("tisma_cloud/static/*.html", "tisma_cloud/static")]
binaries = []
# Modules imported by name at run time: the protocols and loops of uvicorn, the binary driver of PostgreSQL, the window
# (tkinter, imported only without arguments).
hiddenimports = collect_submodules("uvicorn") + collect_submodules("tisma_cloud") + ["tkinter", "tkinter.ttk"]
for package in ("psycopg", "psycopg_binary"):
    d, b, h = collect_all(package)
    datas, binaries, hiddenimports = datas + d, binaries + b, hiddenimports + h
# The window of the control panel (pywebview, installed only on Windows); without it the panel opens in the browser.
try:
    import webview  # noqa: F401
    for package in ("webview", "clr_loader", "pythonnet"):
        d, b, h = collect_all(package)
        datas, binaries, hiddenimports = datas + d, binaries + b, hiddenimports + h
except ImportError:
    pass

a = Analysis(["tisma_cloud_exe.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="tisma-cloud", console=True, upx=False)
