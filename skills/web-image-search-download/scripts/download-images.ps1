# 批量下载候选图并校验（配合 web-image-search-download skill 使用）
# 用法：
#   1) 把上游从 get_html(element) 解析出的 src 填进 $imgs（域名只认 images.unsplash.com）
#   2) 改 $OutDir / $Width / $Quality
#   3) powershell -ExecutionPolicy Bypass -File .\download-images.ps1

param(
    [string]$OutDir  = ".\assets\images",
    [int]   $Width   = 2400,     # 目标最长边像素
    [int]   $Quality = 85,       # JPEG 质量
    [string]$UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
)

$ErrorActionPreference = "Continue"

# ============ 候选图：文件名 => 图片直链（去掉 &amp;、去掉多余尺寸参数） ============
$imgs = [ordered]@{
    # "示例-主题特写.jpg" = "https://images.unsplash.com/photo-1676286145991-d305692b3a8b?fm=jpg&q=85&w=2400&fit=max"
}

if ($imgs.Count -eq 0) {
    Write-Host "[WARN] 候选列表为空，请先把解析到的 src 填进脚本的 `$imgs 字典。" -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$ok = 0; $fail = 0
foreach ($k in $imgs.Keys) {
    $url = $imgs[$k]

    # 1) 反爬 / 付费源过滤
    if ($url -match 'plus\.unsplash\.com') {
        Write-Host "[SKIP] $k  <- Unsplash+ 付费图，不下载" -ForegroundColor Yellow
        continue
    }
    # 2) 还原 HTML 转义
    $url = $url -replace '&amp;', '&'
    # 3) 改写尺寸参数
    $url = [regex]::Replace($url, '([?&])w=\d+', "`${1}w=$Width")
    $url = [regex]::Replace($url, '([?&])q=\d+', "`${1}q=$Quality")

    $dst = Join-Path $OutDir $k
    try {
        Invoke-WebRequest -Uri $url -OutFile $dst -TimeoutSec 60 -UserAgent $UserAgent
        $size = (Get-Item $dst).Length
        if ($size -lt 20KB) {
            Write-Host "[FAIL] $k  体积仅 $([math]::Round($size/1KB,1))KB，疑似 403/错误页" -ForegroundColor Red
            $fail++
        } else {
            Write-Host ("[OK]   {0,-34} {1,8:N0} KB" -f $k, ($size/1KB)) -ForegroundColor Green
            $ok++
        }
    } catch {
        Write-Host "[FAIL] $k : $($_.Exception.Message)" -ForegroundColor Red
        $fail++
    }
}

Write-Host "`n=== 下载完成：成功 $ok / 失败 $fail ===" -ForegroundColor Cyan
Get-ChildItem $OutDir | Select-Object Name, Length | Format-Table -AutoSize

# ============ 收尾：隐藏浏览器（SW_HIDE），避免打断用户桌面 ============
try {
    Add-Type -Namespace Win -Name Api -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
    $names = @('msedge','chrome','firefox','brave','opera','360se','360ChromeX','QQBrowser','SogouExplorer')
    $hit = @()
    foreach ($n in $names) {
        Get-Process -Name $n -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            ForEach-Object {
                [void][Win.Api]::ShowWindow($_.MainWindowHandle, 0)
                $hit += "$($_.ProcessName)(pid=$($_.Id))"
            }
    }
    if ($hit.Count -gt 0) { Write-Host "[HIDE] 已隐藏浏览器窗口：$($hit -join ', ')" -ForegroundColor Cyan }
    else { Write-Host "[HIDE] 无可视浏览器窗口（已在后台运行）" -ForegroundColor DarkGray }
} catch {
    Write-Host "[WARN] 隐藏浏览器失败：$($_.Exception.Message)" -ForegroundColor Yellow
}
