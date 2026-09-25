[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$SmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($CheckOnly -and $SmokeTest) {
    throw "Use either -CheckOnly or -SmokeTest, not both."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$webRoot = Join-Path $repoRoot "web"
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $repoRoot "configs\api.json"
$webPackagePath = Join-Path $webRoot "package.json"
$nodeModulesPath = Join-Path $webRoot "node_modules"
$apiPort = 8000
$webPort = 5173
$apiUrl = "http://127.0.0.1:$apiPort"
$webUrl = "http://127.0.0.1:$webPort"

function Assert-File {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing $Label`: $Path"
    }
}

function Test-PortAvailable {
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        $Port
    )
    try {
        $listener.Start()
        return $true
    }
    catch [System.Net.Sockets.SocketException] {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Wait-ForEndpoint {
    param(
        [Parameter(Mandatory)]
        [string]$Uri,
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)]
        [string]$ServiceName
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "$ServiceName exited before becoming ready (exit code $($Process.ExitCode))."
        }

        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }

    throw "$ServiceName did not become ready at $Uri within 30 seconds."
}

function Stop-ProcessTree {
    param(
        [AllowNull()]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)]
        [string]$ServiceName
    )

    if ($null -eq $Process) {
        return
    }

    $Process.Refresh()
    if ($Process.HasExited) {
        return
    }

    Write-Host "Stopping $ServiceName..."
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    & $taskkill /PID $Process.Id /T /F 2>$null | Out-Null
}

Assert-File -Path $pythonPath -Label "Python virtual environment executable"
Assert-File -Path $configPath -Label "API config"
Assert-File -Path $webPackagePath -Label "dashboard package manifest"

$npmCommand = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command "npm" -ErrorAction SilentlyContinue
}
if ($null -eq $npmCommand) {
    throw "npm is unavailable. Install Node.js 22+ and reopen PowerShell."
}
if (-not (Test-Path -LiteralPath $nodeModulesPath -PathType Container)) {
    throw "Dashboard dependencies are missing. Run: cd web; npm install"
}

$apiConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$configDirectory = Split-Path -Parent $configPath
foreach ($propertyName in @("analysis_report_path", "predictions_path", "zone_lookup_path")) {
    $property = $apiConfig.PSObject.Properties[$propertyName]
    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "API config is missing $propertyName."
    }
    $artifactPath = [System.IO.Path]::GetFullPath(
        (Join-Path $configDirectory ([string]$property.Value))
    )
    Assert-File -Path $artifactPath -Label "API artifact '$propertyName'"
}

foreach ($port in @($apiPort, $webPort)) {
    if (-not (Test-PortAvailable -Port $port)) {
        throw "Port $port is already in use. Stop the existing service before launching the demo."
    }
}

if ($CheckOnly) {
    $validationCode = @"
from pathlib import Path
from urbanflow.api import PredictionStore, load_api_config
store = PredictionStore(load_api_config(Path('configs/api.json')))
try:
    print(f'API artifacts valid: model={store.model_version} rows={store.prediction_rows} zones={store.zone_count}')
finally:
    store.close()
"@
    Push-Location $repoRoot
    try {
        & $pythonPath -c $validationCode
        if ($LASTEXITCODE -ne 0) {
            throw "API artifact validation failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
    Write-Host "Demo preflight passed: Python, npm, dashboard dependencies, artifacts, and ports are ready."
    exit 0
}

$apiProcess = $null
$webProcess = $null
try {
    Write-Host "Starting UrbanFlow API at $apiUrl..."
    $apiProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @(
            "-m", "uvicorn", "urbanflow.api:app",
            "--host", "127.0.0.1",
            "--port", [string]$apiPort
        ) `
        -WorkingDirectory $repoRoot `
        -NoNewWindow `
        -PassThru
    Wait-ForEndpoint `
        -Uri "$apiUrl/health" `
        -Process $apiProcess `
        -ServiceName "UrbanFlow API"

    Write-Host "Starting UrbanFlow dashboard at $webUrl..."
    $webProcess = Start-Process `
        -FilePath $npmCommand.Source `
        -ArgumentList @(
            "run", "dev", "--",
            "--host", "127.0.0.1",
            "--port", [string]$webPort,
            "--strictPort"
        ) `
        -WorkingDirectory $webRoot `
        -NoNewWindow `
        -PassThru
    Wait-ForEndpoint `
        -Uri $webUrl `
        -Process $webProcess `
        -ServiceName "UrbanFlow dashboard"

    if ($SmokeTest) {
        $apiHealth = Invoke-RestMethod -Uri "$apiUrl/health" -TimeoutSec 5
        $proxyHealth = Invoke-RestMethod -Uri "$webUrl/api/health" -TimeoutSec 5
        $dashboardPage = Invoke-WebRequest -Uri $webUrl -UseBasicParsing -TimeoutSec 5
        if ($apiHealth.model_version -ne $proxyHealth.model_version) {
            throw "Vite proxy health response does not match the API model version."
        }
        if ($dashboardPage.Content -notmatch "UrbanFlow AI") {
            throw "Dashboard HTML does not contain the expected UrbanFlow title."
        }
        Write-Host (
            "Demo smoke test passed: model={0} rows={1} zones={2}; API and Vite proxy returned HTTP 200." -f
            $apiHealth.model_version,
            $apiHealth.prediction_rows,
            $apiHealth.zone_count
        )
        exit 0
    }

    Write-Host ""
    Write-Host "UrbanFlow demo is ready: $webUrl"
    Write-Host "Historical backtest only; this is not a live operational forecast."
    Write-Host "Press Ctrl+C to stop both services."
    if (-not $NoBrowser) {
        Start-Process $webUrl | Out-Null
    }

    while ($true) {
        Start-Sleep -Seconds 1
        $apiProcess.Refresh()
        $webProcess.Refresh()
        if ($apiProcess.HasExited) {
            throw "UrbanFlow API exited unexpectedly with code $($apiProcess.ExitCode)."
        }
        if ($webProcess.HasExited) {
            throw "UrbanFlow dashboard exited unexpectedly with code $($webProcess.ExitCode)."
        }
    }
}
finally {
    Stop-ProcessTree -Process $webProcess -ServiceName "UrbanFlow dashboard"
    Stop-ProcessTree -Process $apiProcess -ServiceName "UrbanFlow API"
}
