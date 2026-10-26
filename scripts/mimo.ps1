# Installed as the laptop's `mimo` command. No credentials are displayed.
[CmdletBinding()]
param(
    [ValidateSet('start', 'status', 'logs')]
    [string]$Mode = 'start'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $repo '.venv\Scripts\agent-dispatcher.exe'
if ($Mode -eq 'logs') {
    Get-Content -LiteralPath (Join-Path $repo 'runs\dispatcher.log') -Tail 30 -Wait
    return
}
if ($Mode -eq 'status') {
    Push-Location $repo
    try { & $exe status } finally { Pop-Location }
    return
}

$task = Get-ScheduledTask -TaskName 'agent-dispatcher'
if ($task.State -eq 'Running') {
    throw 'A run is already active. Use mimo status; try mimo after it finishes.'
}
if ($task.State -eq 'Disabled') {
    throw 'The schedule is paused. Enable agent-dispatcher before starting it.'
}

# Prevent a scheduled start while changing the next run's provider settings.
Disable-ScheduledTask -TaskName 'agent-dispatcher' | Out-Null
try {
    if ((Get-ScheduledTask -TaskName 'agent-dispatcher').State -eq 'Running') {
        throw 'A run started concurrently; settings were not changed.'
    }
    $path = Join-Path $repo '.env'
    $content = [IO.File]::ReadAllText($path)
    if ($content -notmatch '(?m)^DISPATCHER_MIMO_API_KEY\s*=\s*\S+') {
        throw 'MiMo credentials are not configured in the existing local .env.'
    }
    $settings = [ordered]@{
        DISPATCHER_DISABLED_PROVIDERS = 'claude,codex,nvidia'
        DISPATCHER_ALLOW_WORKER_THINKING = 'true'
        DISPATCHER_MIMO_HARNESS = 'claude-code'
    }
    foreach ($key in $settings.Keys) {
        $pattern = '(?m)^' + [regex]::Escape($key) + '=.*$'
        $line = $key + '=' + $settings[$key]
        if ([regex]::IsMatch($content, $pattern)) {
            $content = [regex]::Replace($content, $pattern, $line)
        } else {
            $content = $content.TrimEnd() + "`n" + $line + "`n"
        }
    }
    # Replace atomically; preserve the existing file's access control.
    $temporary = $path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, $content, [Text.UTF8Encoding]::new($false))
        [IO.File]::Replace($temporary, $path, [NullString]::Value)
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary
        }
    }
} finally {
    Enable-ScheduledTask -TaskName 'agent-dispatcher' | Out-Null
}
Start-ScheduledTask -TaskName 'agent-dispatcher'
Write-Output 'MiMo-only mode saved. Requested one scheduled cycle; overlapping runs are ignored.'
Write-Output 'It takes the next agent:ready issue. Draft PRs and CI stay on GitHub.'
Write-Output 'Progress: mimo status | Live log: mimo logs (Ctrl+C stops watching only).'
