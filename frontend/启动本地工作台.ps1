param(
    [switch]$NoOpen,
    [switch]$Check,
    [switch]$Restart,
    [ValidateRange(30, 1800)][int]$TimeoutSeconds = 300
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    throw '需要安装 Node.js 22.14 或更新版本，并将其加入 PATH。'
}
$launcherArgs = @((Join-Path $PSScriptRoot 'scripts/start-workbench.mjs'), '--timeout', $TimeoutSeconds)
if ($NoOpen) { $launcherArgs += '--no-open' }
if ($Check) { $launcherArgs += '--check' }
if ($Restart) { $launcherArgs += '--restart' }
& node.exe @launcherArgs
if ($LASTEXITCODE -ne 0) { throw '工作台启动失败，请查看上方诊断。' }
