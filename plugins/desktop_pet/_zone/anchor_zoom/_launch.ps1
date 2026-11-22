$ErrorActionPreference = 'Continue'
$exe = "F:\PythonProjects\TraeProjects\智能体集群\frontend\node_modules\electron\dist\electron.exe"
$wd = "e:\appdata\AgentCluster\plugins\desktop_pet\_zone\anchor_zoom"
$ud = "C:\Users\yangq\AppData\Roaming\anchor-zoom-demo"

# kill old demo processes only (not AgentCluster at PID 12504)
$demoProcs = Get-CimInstance Win32_Process -Filter "Name='electron.exe'" |
  Where-Object { $_.CommandLine -like "*$($wd.Replace('\','\\'))*" } |
  Where-Object { $_.CommandLine -notlike "*--type=*" }
if ($demoProcs) {
  $demoProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 800
}

$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $exe
$pinfo.Arguments = ". --user-data-dir=$ud"
$pinfo.WorkingDirectory = $wd
$pinfo.UseShellExecute = $false
$pinfo.RedirectStandardOutput = $true
$pinfo.RedirectStandardError = $true
$proc = [System.Diagnostics.Process]::Start($pinfo)

Start-Sleep -Seconds 4
if ($proc.HasExited) {
  Write-Output ("[DEMO] failed exit=" + $proc.ExitCode)
  Write-Output ("stdout=" + $proc.StandardOutput.ReadToEnd())
  Write-Output ("stderr=" + $proc.StandardError.ReadToEnd())
} else {
  Write-Output ("[DEMO] running PID=" + $proc.Id)
}
