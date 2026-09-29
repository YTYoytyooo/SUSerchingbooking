param(
    [string]$PythonPath,
    [switch]$Remove,
    [switch]$ExportOnly,
    [ValidateSet('InteractiveToken','Password')]
    [string]$LogonType = 'InteractiveToken'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$hash = [System.Security.Cryptography.SHA256]::Create()
try { $suffix = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-','').Substring(0,12) }
finally { $hash.Dispose() }
$taskName = "Bookpiano-$suffix"
if ($Remove) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Host '已移除本项目的自动更新任务；数据文件和登录凭据保留。'
    return
}
if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $ExportOnly) {
    Push-Location -LiteralPath $root
    try {
        & $PythonPath -m bookpiano check-ready --root $root
        if ($LASTEXITCODE -ne 0) { throw '请先完成首次配置和实际数据核对。' }
    } finally { Pop-Location }
}
# The interactive option works after Windows sign-in. Password logon supports execution before sign-in.
if (-not $ExportOnly -and $LogonType -eq 'InteractiveToken') {
    Write-Host '1：登录 Windows 后自动运行（无需保存 Windows 密码，推荐）'
    Write-Host '2：开机后即使未登录也运行（需要 Windows 账户密码，可能需要管理员权限）'
    $choice = Read-Host '选择 1 或 2'
    if ($choice -eq '2') { $LogonType = 'Password' }
    elseif ($choice -ne '1') { throw '已取消安装。' }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$pythonWindowless = Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonWindowless) { $PythonPath = $pythonWindowless }
$escape = { param([string]$value) [Security.SecurityElement]::Escape($value) }
$userXml = & $escape $identity
$pythonXml = & $escape $PythonPath
$rootXml = & $escape $root
$argsXml = & $escape ('-m bookpiano update --root "' + $root + '"')
$boot = if ($LogonType -eq 'Password') { '<BootTrigger><Enabled>true</Enabled><Delay>PT2M</Delay></BootTrigger>' } else { '' }
# Local checks repeat every ten minutes; the Python guard prevents requests once successful or at the daily limit.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>致诚大会议室：05:00 更新，开机/登录补更新。预约查询每天最多两次。</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger><Repetition><Interval>PT10M</Interval><Duration>PT23H59M</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2026-01-01T05:00:00+08:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT2M</Delay><UserId>$userXml</UserId></LogonTrigger>
    $boot
  </Triggers>
  <Principals><Principal id="Author"><UserId>$userXml</UserId><LogonType>$LogonType</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT5M</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="Author"><Exec><Command>$pythonXml</Command><Arguments>$argsXml</Arguments><WorkingDirectory>$rootXml</WorkingDirectory></Exec></Actions>
</Task>
"@
if ($ExportOnly) {
    $destination = Join-Path $root 'task-preview.xml'
    [IO.File]::WriteAllText($destination, $xml, [Text.Encoding]::Unicode)
    Write-Host '已生成任务配置预览；未注册、未启用任务。'
    return
}
if ($LogonType -eq 'Password') {
    $credential = Get-Credential -UserName $identity -Message '仅供 Windows 任务计划程序保存；不要输入校园账户密码。'
    if (-not $credential -or $credential.UserName -ne $identity) { throw '必须使用当前 Windows 账户。' }
    Register-ScheduledTask -TaskName $taskName -Xml $xml -User $identity -Password $credential.GetNetworkCredential().Password -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
}
$task = Get-ScheduledTask -TaskName $taskName
if (-not $task) { throw '未能验证任务已注册。' }
Write-Host "已安装 $taskName。每天北京时间 05:00 更新，错过后补执行。"
Write-Host '十分钟检查一次本地状态；今天成功后不再访问预约网站。'
