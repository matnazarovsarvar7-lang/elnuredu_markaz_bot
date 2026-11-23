# ELNUR EDU - printer agent (Windows 10/11, Windows PowerShell 5.1)
# Serverdan chop etish topshiriqlarini oladi va SumatraPDF orqali printerga yuboradi.
# Windows chop etish jurnalini (Event 307) serverga yuboradi - botsiz chop etilganlar ham ko'rinadi.
$ErrorActionPreference = 'Continue'
$Ver = 1
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cfg = Get-Content (Join-Path $Dir 'config.json') -Raw | ConvertFrom-Json
$Url = $Cfg.url.TrimEnd('/')
$H = @{ Authorization = "Bearer $($Cfg.key)" }
$StateFile = Join-Path $Dir 'state.json'
$LogFile = Join-Path $Dir 'agent.log'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$created = $false
$Mtx = New-Object System.Threading.Mutex($true, 'Local\ElnurPrintAgent', [ref]$created)
if (-not $created) { exit }

function Write-Log($m) {
    try {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) { Remove-Item $LogFile }
        Add-Content -Path $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m)
    } catch {}
}

function Find-Sumatra {
    $c = @("$env:LOCALAPPDATA\SumatraPDF\SumatraPDF.exe", "$env:ProgramFiles\SumatraPDF\SumatraPDF.exe",
           "${env:ProgramFiles(x86)}\SumatraPDF\SumatraPDF.exe", (Join-Path $Dir 'SumatraPDF.exe'))
    foreach ($p in $c) { if ($p -and (Test-Path $p)) { return $p } }
    $g = Get-Command SumatraPDF.exe -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    return $null
}

function Post-Json($path, $obj) {
    $json = $obj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Uri "$Url$path" -Method Post -Headers $H -Body $bytes `
        -ContentType 'application/json; charset=utf-8' -TimeoutSec 60
}

function Get-State {
    if (Test-Path $StateFile) { try { return (Get-Content $StateFile -Raw | ConvertFrom-Json) } catch {} }
    return $null
}

function Send-Logs {
    $st = Get-State
    $ev = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-PrintService/Operational'; Id = 307 } `
        -MaxEvents 300 -ErrorAction SilentlyContinue
    if (-not $ev) { return }
    $maxRid = ($ev | Measure-Object -Property RecordId -Maximum).Maximum
    if (-not $st) {
        # birinchi ishga tushish: eski tarixni yubormaymiz
        @{ rid = $maxRid } | ConvertTo-Json | Set-Content $StateFile
        return
    }
    $new = $ev | Where-Object { $_.RecordId -gt [int64]$st.rid } | Sort-Object RecordId
    if (-not $new) { return }
    $items = @()
    foreach ($e in $new) {
        $p = $e.Properties
        $items += [pscustomobject]@{
            rid = $e.RecordId; time = $e.TimeCreated.ToUniversalTime().ToString('o')
            doc = [string]$p[1].Value; user = [string]$p[2].Value; printer = [string]$p[4].Value
            pages = [int]$p[7].Value
        }
    }
    Post-Json '/agent/log' @{ items = $items } | Out-Null
    @{ rid = $maxRid } | ConvertTo-Json | Set-Content $StateFile
}

function Do-Task($t, $sumatra) {
    $f = Join-Path $env:TEMP $t.name
    $ok = $false; $err = ''
    try {
        Invoke-WebRequest -Uri "$Url/agent/file/$($t.id)" -Headers $H -OutFile $f -TimeoutSec 180 -UseBasicParsing
        if (-not $sumatra) { throw 'SumatraPDF topilmadi' }
        $pr = Get-Printer -Name $t.printer -ErrorAction SilentlyContinue
        if (-not $pr) { throw "Printer topilmadi: $($t.printer)" }
        if ($pr.PrinterStatus -match 'Offline|Error|PaperOut|PaperJam') { throw "Printer holati: $($pr.PrinterStatus)" }
        $a = @('-print-to', "`"$($t.printer)`"", '-print-settings', 'fit', '-silent', "`"$f`"")
        $p = Start-Process -FilePath $sumatra -ArgumentList $a -Wait -PassThru -WindowStyle Hidden
        if ($p.ExitCode -ne 0) { throw "SumatraPDF xato kodi $($p.ExitCode)" }
        $ok = $true
    } catch { $err = $_.Exception.Message; Write-Log "task $($t.id): $err" }
    try { Post-Json '/agent/done' @{ id = $t.id; ok = $ok; err = $err } | Out-Null } catch { Write-Log "done: $_" }
    Start-Sleep -Seconds 2
    Remove-Item $f -ErrorAction SilentlyContinue
}

Write-Log "start v$Ver"
$lastLog = [datetime]::MinValue
while ($true) {
    try {
        $sumatra = Find-Sumatra
        $names = @(Get-Printer -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch 'PDF|XPS|OneNote|Fax' } |
            ForEach-Object { $_.Name })
        $r = Post-Json '/agent/poll' @{ printers = $names; ver = $Ver; sumatra = [bool]$sumatra }
        foreach ($t in @($r.tasks)) { if ($t) { Do-Task $t $sumatra } }
    } catch { Write-Log "poll: $($_.Exception.Message)"; Start-Sleep -Seconds 20 }
    if (((Get-Date) - $lastLog).TotalSeconds -ge 60) {
        try { Send-Logs } catch { Write-Log "log: $($_.Exception.Message)" }
        $lastLog = Get-Date
    }
    Start-Sleep -Seconds 5
}
