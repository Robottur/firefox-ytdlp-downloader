[CmdletBinding()]
param(
    [string]$Executable = (Join-Path $PSScriptRoot 'ytdlp-host.exe'),
    [switch]$FromSource,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'ytdlp-host')
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$InstallDir = (Resolve-Path $InstallDir).Path
$target = Join-Path $InstallDir 'ytdlp-host.exe'
$sourceLauncher = Join-Path $InstallDir 'venv\Scripts\ytdlp-host.exe'
$old = if (Test-Path $target) { $target } elseif (Test-Path $sourceLauncher) { $sourceLauncher } else { $null }
if ($old) {
    & $old --stop-service
    if ($LASTEXITCODE -ne 0) { throw 'Finish or cancel active downloads, then run the installer again.' }
    Start-Sleep -Seconds 2
}
if ($FromSource) {
    $venv = Join-Path $InstallDir 'venv'
    if (!(Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
            if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer.' }
            & py -3 -m venv $venv
        } else {
            & python -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
            if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer.' }
            & python -m venv $venv
        }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
    }
    & (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade $PSScriptRoot 'yt-dlp[default]'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the host dependencies.' }
    $target = $sourceLauncher
} else {
    if (!(Test-Path $Executable)) {
        throw 'Place the built ytdlp-host.exe beside this installer, pass -Executable, or use -FromSource with Python 3.11+.'
    }
    if ((Resolve-Path $Executable).Path -ne $target) { Copy-Item -Force $Executable $target }
}
$manifestPath = Join-Path $InstallDir 'ytdlp_downloader.host.json'
$manifest = @{
    name = 'ytdlp_downloader.host'
    description = 'yt-dlp Downloader companion'
    path = $target
    type = 'stdio'
    allowed_extensions = @('ytdlp-downloader-573c69b6aa75@local')
} | ConvertTo-Json
[IO.File]::WriteAllText($manifestPath, $manifest, [Text.UTF8Encoding]::new($false))
$key = 'HKCU:\Software\Mozilla\NativeMessagingHosts\ytdlp_downloader.host'
New-Item -Path $key -Force | Out-Null
Set-Item -Path $key -Value $manifestPath
Write-Host "Installed and registered: $target"
Write-Host 'Install ffmpeg and Deno if missing: winget install Gyan.FFmpeg; winget install DenoLand.Deno'
Write-Host 'Restart Firefox after changing PATH. Load extension/manifest.json at about:debugging for development.'
