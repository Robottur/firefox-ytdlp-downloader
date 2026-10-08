$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    py -3 -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required to build.' }
    $python = Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'
    & $python -m pip install --upgrade pip pyinstaller .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $python -m PyInstaller --clean --noconfirm --onefile --console --name ytdlp-host `
        --collect-all yt_dlp --collect-all yt_dlp_ejs --collect-all certifi `
        --hidden-import tkinter --hidden-import tkinter.filedialog `
        --distpath '..\dist\windows' --workpath '..\build\windows' tools\frozen_entry.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    Copy-Item install_windows.ps1,uninstall_windows.ps1 '..\dist\windows'
    Copy-Item '..\README.md' '..\dist\windows'
    Compress-Archive -Force -Path '..\dist\windows\*' -DestinationPath '..\dist\ytdlp-host-windows.zip'
} finally { Pop-Location }
