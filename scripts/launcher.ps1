param(
    [ValidateSet('setup','update','status','demo','init','install','remove','prepare','configure','connect-feishu')]
    [string]$Command = 'status'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $root
try {
    $python = Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    switch ($Command) {
        'prepare' {
            # Playwright is used for first setup and automatic CAS login; data APIs use the standard library.
            $venvPython = Join-Path $root '.venv/Scripts/python.exe'
            if (-not (Test-Path -LiteralPath $venvPython)) {
                & $python -m venv (Join-Path $root '.venv')
                if ($LASTEXITCODE -ne 0) { throw '创建本地 Python 环境失败。' }
            }
            & $venvPython -m pip install --disable-pip-version-check --retries 1 --timeout 30 -r (Join-Path $root 'requirements-login.txt')
            if ($LASTEXITCODE -ne 0) { throw '登录组件安装失败；没有访问预约网站。' }
            Write-Host '登录组件已准备好，请先双击“配置账号与飞书.cmd”。'
        }
        'install' { & (Join-Path $PSScriptRoot 'tasks.ps1') -PythonPath $python }
        'remove' { & (Join-Path $PSScriptRoot 'tasks.ps1') -Remove }
        'update' { & $python -m bookpiano update --root $root --force }
        default { & $python -m bookpiano $Command --root $root }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
}
Read-Host '按 Enter 关闭窗口' | Out-Null
