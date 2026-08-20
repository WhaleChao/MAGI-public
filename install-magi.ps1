param(
    [switch]$Apply,
    [switch]$Full,
    [switch]$BootstrapDatabase,
    [switch]$NoService,
    [string]$Name = "MAGI"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = $null

if (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = "py"
    $PythonPrefix = @("-3.12")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = "python"
    $PythonPrefix = @()
} else {
    Write-Error "找不到 Python。請先從 python.org 安裝 Python 3.12，並勾選 Add Python to PATH。"
    exit 2
}

$Arguments = @()
$Arguments += $PythonPrefix
$Arguments += @("$Root\scripts\magi_selfhost.py", "--target", "windows", "--source", $Root, "--name", $Name, "install")
if ($Apply) { $Arguments += "--apply" }
if ($Full) { $Arguments += "--full" }
if ($NoService) { $Arguments += "--no-service" }
if ($BootstrapDatabase) { $Arguments += "--bootstrap-database" }

Write-Host "MAGI Windows 自架安裝器"
if (-not $Apply) {
    Write-Host "目前為安全預覽，不會修改電腦。確認內容後加上 -Apply。" -ForegroundColor Yellow
}
& $Python @Arguments
exit $LASTEXITCODE
