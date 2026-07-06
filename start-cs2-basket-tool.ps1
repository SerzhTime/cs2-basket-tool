$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root ".runtime"
$PidFile = Join-Path $RuntimeDir "streamlit.pid"
$OutLog = Join-Path $Root "streamlit.out.log"
$ErrLog = Join-Path $Root "streamlit.err.log"
$Streamlit = Join-Path $Root ".venv\Scripts\streamlit.exe"
$Url = "http://127.0.0.1:8501"

function Open-ToolUrl {
    param([string]$Address)
    try {
        Start-Process -FilePath "explorer.exe" -ArgumentList $Address
    } catch {
        Write-Host "Open this URL manually: $Address"
    }
}

Write-Host "Starting CS2 basket tool..."
Write-Host "Project: $Root"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

if (-not (Test-Path -LiteralPath $Streamlit)) {
    Write-Host "Streamlit executable not found: $Streamlit"
    Write-Host "Run setup from README.md first."
    exit 1
}

try {
    Write-Host "Checking existing local server..."
    $ExistingResponse = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3
    if ($ExistingResponse.StatusCode -ge 200 -and $ExistingResponse.StatusCode -lt 500) {
        Write-Host "CS2 basket tool is already running at $Url"
        Open-ToolUrl $Url
        Start-Sleep -Seconds 1
        exit 0
    }
} catch {
    # No server is responding on the expected local port.
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
    if ($ExistingPid) {
        $ExistingProcess = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Found non-responsive CS2 basket tool process $ExistingPid. Restarting it..."
            Stop-Process -Id $ExistingProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Launching Streamlit..."
$LaunchCommand = @(
    "& '$Streamlit'",
    "run app.py",
    "--server.address 127.0.0.1",
    "--server.port 8501",
    "--server.headless true",
    "1> '$OutLog'",
    "2> '$ErrLog'"
) -join " "

$Process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $LaunchCommand) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $PidFile -Value $Process.Id -Encoding ASCII

$Started = $false
for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
    try {
        Write-Host "Waiting for app response ($Attempt/20)..."
        $Response = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 2
        if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 500) {
            $Started = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}

if ($Started) {
    Write-Host "CS2 basket tool started. HTTP $($Response.StatusCode)"
    Write-Host "URL: $Url"
    Open-ToolUrl $Url
    Start-Sleep -Seconds 1
    exit 0
}

Write-Host "Started process $($Process.Id), but the app did not respond yet."
Write-Host "Check log: $ErrLog"
exit 1
