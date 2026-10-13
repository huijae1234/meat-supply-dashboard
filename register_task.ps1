# 매일 오전 9시에 collect.py 를 실행하는 Windows 예약 작업을 등록한다.
#   등록:  powershell -ExecutionPolicy Bypass -File register_task.ps1
#   해제:  Unregister-ScheduledTask -TaskName "SupplyDashboardCollect" -Confirm:$false
# PC가 꺼져 있어 놓친 실행은 켜졌을 때 바로 실행되고(StartWhenAvailable), 빠진 날짜는 다음 수집에서 채워진다.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $py -Argument "collect.py" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "SupplyDashboardCollect" -Action $action -Trigger $trigger -Settings $settings -Description "축산물 수급 대시보드 일일 수집" -Force | Out-Null
Get-ScheduledTask -TaskName "SupplyDashboardCollect" | Select-Object TaskName, State
