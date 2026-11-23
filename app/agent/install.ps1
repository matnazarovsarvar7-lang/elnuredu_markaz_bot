# ELNUR EDU - printer dasturini o'rnatish (bir marta). Botdagi havola orqali ishga tushiriladi.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Url = '__URL__'
$Key = '__KEY__'
$Dir = Join-Path $env:LOCALAPPDATA 'ElnurPrint'
Write-Host ''
Write-Host '=== ELNUR EDU printer dasturi ===' -ForegroundColor Cyan

# 1) eski nusxani to'xtatish
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*ElnurPrint*agent.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2) fayllar
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
@{ url = $Url; key = $Key } | ConvertTo-Json | Set-Content -Path (Join-Path $Dir 'config.json') -Encoding UTF8
Invoke-WebRequest -Uri "$Url/agent/script" -Headers @{ Authorization = "Bearer $Key" } `
    -OutFile (Join-Path $Dir 'agent.ps1') -UseBasicParsing
Write-Host '[1/4] Dastur fayllari yuklandi' -ForegroundColor Green

# 3) SumatraPDF (bepul PDF dasturi - chop etish uchun)
$have = @("$env:LOCALAPPDATA\SumatraPDF\SumatraPDF.exe", "$env:ProgramFiles\SumatraPDF\SumatraPDF.exe") |
    Where-Object { Test-Path $_ }
if (-not $have) {
    Write-Host '[2/4] SumatraPDF o''rnatilmoqda...' -ForegroundColor Yellow
    winget install -e --id SumatraPDF.SumatraPDF --scope user --silent --accept-source-agreements --accept-package-agreements
} else { Write-Host '[2/4] SumatraPDF bor' -ForegroundColor Green }

# 4) Windows chop etish jurnalini yoqish (administrator ruxsati so'raladi - "Ha" ni bosing)
try {
    Start-Process -FilePath 'wevtutil.exe' -ArgumentList 'sl', 'Microsoft-Windows-PrintService/Operational', '/e:true' `
        -Verb RunAs -Wait -WindowStyle Hidden
    Write-Host '[3/4] Chop etish jurnali yoqildi' -ForegroundColor Green
} catch { Write-Host '[3/4] Jurnal yoqilmadi (ruxsat berilmadi) - botsiz chop etish nazorati ishlamaydi' -ForegroundColor Red }

# 5) kompyuter yoqilganda avtomatik ishga tushish (yashirin oynada)
$startup = [Environment]::GetFolderPath('Startup')
$vbs = Join-Path $startup 'ElnurPrint.vbs'
$agent = Join-Path $Dir 'agent.ps1'
Set-Content -Path $vbs -Encoding ASCII -Value ("CreateObject(""WScript.Shell"").Run ""powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$agent"""""", 0, False")
Start-Process -FilePath 'wscript.exe' -ArgumentList "`"$vbs`""
Write-Host '[4/4] Dastur ishga tushdi va har safar kompyuter yoqilganda o''zi ishlaydi' -ForegroundColor Green
Write-Host ''
Write-Host 'TAYYOR. Botga qayting - u printerlarni ko''rsatadi.' -ForegroundColor Cyan
