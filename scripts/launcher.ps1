param([ValidateSet('menu')][string]$Command = 'menu')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $root
try {
    $python = Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    & $python -m bookpiano menu --root $root
    if ($LASTEXITCODE -ne 0) { throw '菜单未正常启动，请检查 Python 安装和上方提示。' }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host '按 Enter 关闭窗口' | Out-Null
}