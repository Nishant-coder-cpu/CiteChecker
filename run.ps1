param(
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Command = "start"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Logs = Join-Path $ProjectRoot "logs"
$EnvFile = Join-Path $ProjectRoot ".env"

function Get-ConfiguredPort([string]$Name, [int]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if (-not $value -and (Test-Path $EnvFile)) {
        $pattern = "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$"
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match $pattern) {
                $value = $Matches[1].Trim("'`"")
            }
        }
    }
    if (-not $value) {
        return $Default
    }

    $port = 0
    if (-not [int]::TryParse($value, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "$Name must be a port number between 1 and 65535."
    }
    return $port
}

$AgentPort = Get-ConfiguredPort "AGENT_PORT" 8001
$VerifierPort = Get-ConfiguredPort "VERIFIER_PORT" 8002
if ($AgentPort -eq $VerifierPort) {
    throw "AGENT_PORT and VERIFIER_PORT must be different."
}

function Get-PortProcessId([int]$Port) {
    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) {
        return [int]$connection.OwningProcess
    }
    return $null
}

function Get-ProjectAgentProcess([string]$Name, [int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    $scriptName = "$Name`_agent.py"
    if ($Name -eq "citecheck") {
        $scriptName = "agent.py"
    }
    $scriptPath = Join-Path $ProjectRoot $scriptName
    if (-not $process -or -not $process.CommandLine) {
        return $null
    }
    if ($process.CommandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $process
    }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" `
        -ErrorAction SilentlyContinue
    $expectedParentCommand = '"{0}" {1}' -f $Python, $scriptName
    if ($parent -and $parent.CommandLine -and
        $parent.CommandLine.Equals($expectedParentCommand, [StringComparison]::OrdinalIgnoreCase)) {
        return $process
    }
    return $null
}

function Get-OwnedProcess([string]$Name, [int]$ProcessId) {
    $pidFile = Join-Path $Logs "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        return $null
    }
    $recordedId = 0
    if (-not [int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$recordedId) -or
        $recordedId -ne $ProcessId) {
        return $null
    }
    return Get-ProjectAgentProcess $Name $ProcessId
}

function Show-AgentStatus([string]$Name, [int]$Port) {
    $processId = Get-PortProcessId $Port
    if (-not $processId) {
        Write-Host "$Name`: stopped (port $Port)"
        return
    }
    if (Get-ProjectAgentProcess $Name $processId) {
        Write-Host "$Name`: running (pid $processId, port $Port)"
    }
    else {
        Write-Host "$Name`: port $Port is in use by another process (pid $processId)"
    }
}

function Start-Agent([string]$Name, [string]$Script, [int]$Port) {
    $existingProcessId = Get-PortProcessId $Port
    if ($existingProcessId) {
        if (Get-ProjectAgentProcess $Name $existingProcessId) {
            Write-Host "$Name is already running (pid $existingProcessId, port $Port)."
            return
        }
        throw "Port $Port is already in use (pid $existingProcessId). Run .\run.ps1 status to inspect it."
    }
    $outputLog = Join-Path $Logs "$Name.log"
    $errorLog = Join-Path $Logs "$Name.error.log"
    $scriptPath = Join-Path $ProjectRoot $Script
    $process = Start-Process -FilePath $Python `
        -ArgumentList ('"{0}"' -f $scriptPath) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $outputLog `
        -RedirectStandardError $errorLog `
        -PassThru
    Set-Content -Path (Join-Path $Logs "$Name.pid") -Value $process.Id
    Write-Host "Started $Name (pid $($process.Id)); logs: $outputLog and $errorLog"
}

function Stop-Agent([string]$Name, [int]$Port) {
    $pidFile = Join-Path $Logs "$Name.pid"
    $recordedId = 0
    if ((Test-Path $pidFile) -and
        [int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$recordedId)) {
        $process = Get-OwnedProcess $Name $recordedId
        if ($process) {
            Stop-Process -Id $recordedId
            Write-Host "Stopped $Name (pid $recordedId)"
        }
        Remove-Item $pidFile -Force
    }
    $processId = Get-PortProcessId $Port
    if ($processId) {
        if (Get-ProjectAgentProcess $Name $processId) {
            Stop-Process -Id $processId
            Write-Host "Stopped $Name (pid $processId)"
        }
        else {
            Write-Host "$Name is not managed by this script; left process $processId on port $Port untouched."
        }
    }
}

if ($Command -in @("start", "restart")) {
    if (-not (Test-Path $Python)) {
        throw "Project Python environment not found at $Python. Create it and install requirements.txt first."
    }
    if (-not (Test-Path $EnvFile)) {
        throw "Missing .env. Copy .env.example to .env and configure ASI_ONE_API_KEY and AGENT_SEED."
    }
}

New-Item -ItemType Directory -Path $Logs -Force | Out-Null

switch ($Command) {
    "start" {
        Start-Agent "verifier" "verifier_agent.py" $VerifierPort
        Start-Sleep -Seconds 3
        Start-Agent "citecheck" "agent.py" $AgentPort
        Start-Sleep -Seconds 3
        Show-AgentStatus "verifier" $VerifierPort
        Show-AgentStatus "citecheck" $AgentPort
    }
    "stop" {
        Stop-Agent "citecheck" $AgentPort
        Stop-Agent "verifier" $VerifierPort
    }
    "restart" {
        Stop-Agent "citecheck" $AgentPort
        Stop-Agent "verifier" $VerifierPort
        Start-Sleep -Seconds 2
        Start-Agent "verifier" "verifier_agent.py" $VerifierPort
        Start-Sleep -Seconds 3
        Start-Agent "citecheck" "agent.py" $AgentPort
        Start-Sleep -Seconds 3
        Show-AgentStatus "verifier" $VerifierPort
        Show-AgentStatus "citecheck" $AgentPort
    }
    "status" {
        Show-AgentStatus "verifier" $VerifierPort
        Show-AgentStatus "citecheck" $AgentPort
    }
    "logs" {
        $logFiles = @(
            (Join-Path $Logs "verifier.log"),
            (Join-Path $Logs "verifier.error.log"),
            (Join-Path $Logs "citecheck.log"),
            (Join-Path $Logs "citecheck.error.log")
        ) | Where-Object { Test-Path $_ }
        if ($logFiles.Count -eq 0) {
            $runningAgents = @()
            foreach ($entry in @(
                @{ Name = "verifier"; Port = $VerifierPort },
                @{ Name = "citecheck"; Port = $AgentPort }
            )) {
                $processId = Get-PortProcessId $entry.Port
                if ($processId -and (Get-ProjectAgentProcess $entry.Name $processId)) {
                    $runningAgents += $entry.Name
                }
            }
            if ($runningAgents.Count -gt 0) {
                Write-Host "No captured logs for the running $($runningAgents -join ' and ') agent(s)."
                Write-Host "Processes started from a terminal do not have output saved here."
                Write-Host "Run .\run.ps1 restart to restart them with logs captured in .\logs\."
            }
            else {
                Write-Host "No logs yet. Start the agents with .\run.ps1 start."
            }
        }
        else {
            foreach ($name in @("verifier", "citecheck")) {
                $hasLog = (Test-Path (Join-Path $Logs "$name.log")) -or
                    (Test-Path (Join-Path $Logs "$name.error.log"))
                $port = if ($name -eq "verifier") { $VerifierPort } else { $AgentPort }
                $processId = Get-PortProcessId $port
                if (-not $hasLog -and $processId -and (Get-ProjectAgentProcess $name $processId)) {
                    Write-Host "No captured logs for the running $name agent; use .\run.ps1 restart to capture its output."
                }
            }
            Get-Content -Path $logFiles -Tail 80 -Wait
        }
    }
}
