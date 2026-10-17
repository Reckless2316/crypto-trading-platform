param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$ScriptDirectory = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PSScriptRoot)
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDirectory '..'))
$ComposeFile = Join-Path $ScriptDirectory 'docker-compose.yml'

function Test-LocalPortAvailable {
    param([int]$Port)
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $Listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        $Listener.Stop()
    }
}

if ([string]::IsNullOrWhiteSpace($env:SURGE_HOST_PORT)) {
    $DashboardPort = $null
    for ($Port = 8000; $Port -le 8099; $Port++) {
        if (Test-LocalPortAvailable -Port $Port) {
            $DashboardPort = $Port
            break
        }
    }
    if ($null -eq $DashboardPort) {
        throw 'No available dashboard port found between 8000 and 8099. Set SURGE_HOST_PORT to an available port and try again.'
    }
    $env:SURGE_HOST_PORT = [string]$DashboardPort
} else {
    $DashboardPort = 0
    if (-not [int]::TryParse($env:SURGE_HOST_PORT, [ref]$DashboardPort) -or $DashboardPort -lt 1 -or $DashboardPort -gt 65535) {
        throw 'SURGE_HOST_PORT must be a TCP port number between 1 and 65535.'
    }
}
$DashboardUrl = "http://127.0.0.1:$DashboardPort"
Write-Host "Using dashboard port $DashboardPort."

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
