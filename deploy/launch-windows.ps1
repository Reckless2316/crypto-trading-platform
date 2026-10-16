param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ComposeFile = Join-Path $PSScriptRoot 'docker-compose.yml'
$DashboardUrl = 'http://127.0.0.1:8000'

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose -f $ComposeFile --project-directory $RepoRoot @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed with exit code $LASTEXITCODE."
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'Docker was not found. Install and start Docker Desktop, then run this launcher again.'
    exit 1
}

switch ($Action) {
    'start' {
        & docker info *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'Docker Desktop is not running. Start Docker Desktop and try again.'
            exit 1
        }
        Invoke-Compose @('up', '-d', '--build')
        Write-Host 'Waiting for SURGE to become healthy...'
        $Healthy = $false
        for ($Attempt = 0; $Attempt -lt 45; $Attempt++) {
            try {
                $Health = Invoke-RestMethod -Uri "$DashboardUrl/health" -TimeoutSec 2
                if ($Health.status -eq 'ok') {
                    $Healthy = $true
                    break
                }
            } catch {
                Start-Sleep -Seconds 2
            }
        }
        if ($Healthy) {
            Write-Host "SURGE is ready at $DashboardUrl"
            Start-Process $DashboardUrl
        } else {
            Write-Warning 'SURGE did not become healthy in time. Showing recent logs.'
            Invoke-Compose @('logs', '--tail=80')
            exit 1
        }
    }
    'stop' { Invoke-Compose @('down') }
    'restart' { Invoke-Compose @('up', '-d', '--build', '--force-recreate') }
    'status' { Invoke-Compose @('ps') }
    'logs' { Invoke-Compose @('logs', '--tail=100', '-f') }
}
