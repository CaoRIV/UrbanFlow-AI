[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [ValidateRange(0, 7)]
    [int]$StartStage = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$reliabilityConfig = "configs/reliability.json"
$runReportPath = Join-Path $repoRoot "artifacts\reliability\pipeline-run.json"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Missing Python environment: ' + $pythonPath + '. Create .venv and install -e ".[dev]".'
}

function Invoke-PythonCheck {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,
        [Parameter(Mandatory)]
        [string]$Label
    )

    & $pythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Invoke-PipelineStage {
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [Parameter(Mandatory)]
        [string]$Module,
        [Parameter(Mandatory)]
        [string]$Config,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[object]]$Results
    )

    Write-Host ""
    Write-Host "==> $Name"
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $slug = (($Name -replace '[^a-zA-Z0-9]+', '-').Trim('-')).ToLowerInvariant()
    $logPath = Join-Path $repoRoot "artifacts\reliability\logs\$slug.log"
    $errorLogPath = "$logPath.stderr"
    New-Item -ItemType Directory -Path (Split-Path -Parent $logPath) -Force | Out-Null
    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", $Module, "--config", $Config) `
        -NoNewWindow `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError $errorLogPath
    $exitCode = $process.ExitCode
    $timer.Stop()
    if ($exitCode -ne 0) {
        Get-Content -LiteralPath $logPath -Tail 20 | Write-Host
        Get-Content -LiteralPath $errorLogPath -Tail 20 | Write-Host
        throw "$Name failed with exit code $exitCode. Logs: $logPath, $errorLogPath"
    }
    Write-Host "Completed in $([Math]::Round($timer.Elapsed.TotalSeconds, 3)) seconds."
    $Results.Add([ordered]@{
        name = $Name
        module = $Module
        config = $Config
        elapsed_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
        log_path = $logPath.Replace($repoRoot + '\', '').Replace('\', '/')
    })
}

Push-Location $repoRoot
try {
    Write-Host "==> Verifying exact dependency pins and imports"
    Invoke-PythonCheck `
        -Arguments @("-m", "urbanflow.reliability", "--config", $reliabilityConfig, "--environment-only") `
        -Label "Environment verification"

    Write-Host "==> Checking installed dependency constraints"
    Invoke-PythonCheck -Arguments @("-m", "pip", "check") -Label "pip check"

    if ($VerifyOnly) {
        Write-Host "==> Verifying existing report chain, artifact footprint, and API store"
        Invoke-PythonCheck `
            -Arguments @("-m", "urbanflow.reliability", "--config", $reliabilityConfig) `
            -Label "Reliability verification"
        exit 0
    }

    $stages = [System.Collections.Generic.List[object]]::new()
    $pipelineTimer = [System.Diagnostics.Stopwatch]::StartNew()
    $stageDefinitions = @(
        @("Download or verify raw files", "urbanflow.download_data", "configs/data_sources.json"),
        @("Inspect configured raw month", "urbanflow.inspect_data", "configs/eda.json"),
        @("Aggregate hourly pickups", "urbanflow.aggregate_hourly", "configs/aggregate.json"),
        @("Build full hourly grid", "urbanflow.build_hourly_grid", "configs/grid.json"),
        @("Evaluate seasonal baseline", "urbanflow.evaluate_baseline", "configs/baseline.json"),
        @("Build leakage-safe features", "urbanflow.build_features", "configs/features.json"),
        @("Train locked CPU model", "urbanflow.train_model", "configs/model.json"),
        @("Analyze locked test errors", "urbanflow.analyze_model", "configs/model_analysis.json")
    )
    for ($stageIndex = $StartStage; $stageIndex -lt $stageDefinitions.Count; $stageIndex++) {
        $stage = $stageDefinitions[$stageIndex]
        Invoke-PipelineStage `
            -Name $stage[0] `
            -Module $stage[1] `
            -Config $stage[2] `
            -Results $stages
    }
    $pipelineTimer.Stop()

    $runReportDirectory = Split-Path -Parent $runReportPath
    New-Item -ItemType Directory -Path $runReportDirectory -Force | Out-Null
    $runReport = [ordered]@{
        report_version = 1
        status = "pass"
        generated_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        python = (& $pythonPath --version 2>&1).Trim()
        dependency_check = "exact pyproject pins imported; pip check passed"
        start_stage = $StartStage
        elapsed_seconds = [Math]::Round($pipelineTimer.Elapsed.TotalSeconds, 3)
        stages = $stages
    }
    $temporaryPath = "$runReportPath.tmp"
    $json = $runReport | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryPath -Destination $runReportPath -Force

    Write-Host ""
    Write-Host "==> Verifying report chain, artifact footprint, and API store"
    Invoke-PythonCheck `
        -Arguments @("-m", "urbanflow.reliability", "--config", $reliabilityConfig) `
        -Label "Reliability verification"
    Write-Host "Pipeline completed in $([Math]::Round($pipelineTimer.Elapsed.TotalSeconds, 3)) seconds."
}
finally {
    Pop-Location
}
