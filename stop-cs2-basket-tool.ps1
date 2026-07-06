$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root ".runtime"
$PidFile = Join-Path $RuntimeDir "streamlit.pid"
$Stopped = $false
$HadPidFile = Test-Path -LiteralPath $PidFile

function Stop-LocalPortListener {
    param([int]$Port)

    $Lines = netstat -ano -p tcp | Select-String -Pattern ":$Port\s+.*LISTENING\s+(\d+)"
    foreach ($Line in $Lines) {
        $Text = $Line.Line.Trim()
        $Parts = $Text -split "\s+"
        $OwnerPid = [int]$Parts[-1]
        if ($OwnerPid -gt 0) {
            Stop-Process -Id $OwnerPid -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped CS2 basket tool listener process $OwnerPid."
            $script:Stopped = $true
        }
    }
}

Stop-LocalPortListener -Port 8501

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
    if ($ExistingPid) {
        $Process = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($Process) {
            Stop-Process -Id $Process.Id -Force
            Write-Host "Stopped CS2 basket tool process $($Process.Id)."
            $Stopped = $true
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if (-not $Stopped) {
    try {
        $Processes = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.CommandLine -and
                $_.CommandLine -match "streamlit" -and
                $_.CommandLine -match [regex]::Escape($Root)
            }

        foreach ($Process in $Processes) {
            Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped CS2 basket tool process $($Process.ProcessId)."
            $Stopped = $true
        }
    } catch {
        # Some Windows accounts cannot query Win32_Process. The listener/PID paths above are enough.
    }
}

if (-not $Stopped) {
    $Remaining = netstat -ano -p tcp | Select-String -Pattern ":8501\s+.*LISTENING\s+(\d+)"
    if ($HadPidFile -and -not $Remaining) {
        Write-Host "CS2 basket tool stopped."
    } else {
        Write-Host "CS2 basket tool was not running."
    }
}
