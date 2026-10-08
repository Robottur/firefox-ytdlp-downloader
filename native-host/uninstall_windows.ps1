$ErrorActionPreference = 'Stop'
$key = 'HKCU:\Software\Mozilla\NativeMessagingHosts\ytdlp_downloader.host'
if (Test-Path $key) {
    $manifestPath = (Get-Item $key).GetValue('')
    if (Test-Path $manifestPath) {
        $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
        if (Test-Path $manifest.path) {
            & $manifest.path --stop-service
            if ($LASTEXITCODE -ne 0) { throw 'Finish or cancel active downloads first.' }
        }
    }
    Remove-Item $key
}
Write-Host 'Firefox registration removed. Downloaded files, history and installed host files were kept.'
