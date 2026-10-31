# Installed as the laptop's `mimo` command. No credentials are displayed.
[CmdletBinding()]
param(
    [ValidateSet('chat', 'task', 'start', 'status', 'logs')]
    [string]$Mode = 'chat'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $repo '.venv\Scripts\agent-dispatcher.exe'
if ($Mode -eq 'chat') {
    Push-Location $repo
    try {
        & (Join-Path $repo '.venv\Scripts\python.exe') -m agent_dispatcher.native_ui
    } finally { Pop-Location }
    return
}
Write-Output 'MiMo orchestrator: task -> plan -> code -> review -> gated draft PR.'
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
    if ($Mode -eq 'start') {
        Write-Output 'A cycle is already active; provider settings were not changed.'
        Write-Output 'No duplicate started. Use mimo status; queued work waits for a later cycle.'
        return
    }
    throw 'A run is already active. Use mimo status; try mimo after it finishes.'
}
if ($task.State -eq 'Disabled') {
    throw 'The schedule is paused. Enable agent-dispatcher before starting it.'
}

if ($Mode -eq 'task') {
    $target = (Read-Host 'Public repo name (for example shortlink)').Trim()
    if ($target -notmatch '^[A-Za-z0-9_.-]+$' -or $target -eq 'agent-dispatcher') {
        throw 'Use an owned public application repo name; the controller is excluded.'
    }
    $target = 'Abdel-Abdelaal/' + $target
    $details = & gh repo view $target --json nameWithOwner,isPrivate,isArchived,isFork
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the repository; nothing queued.' }
    $details = $details | ConvertFrom-Json
    if ($details.nameWithOwner -ine $target -or $details.isPrivate -or
        $details.isArchived -or $details.isFork) {
        throw 'Only owned public, active, non-fork repositories can be queued.'
    }
    $login = & gh api user --jq '.login'
    if ($LASTEXITCODE -ne 0 -or $login.Trim() -ine 'Abdel-Abdelaal') {
        throw 'Sign in to GitHub CLI as the owner before authorizing a task.'
    }
    Write-Output "This task will be a public GitHub issue in $target. Do not enter secrets."
    $goal = (Read-Host 'What should it do?').Trim()
    $acceptance = (Read-Host 'How will we know it is done?').Trim()
    if (!$goal -or !$acceptance) { throw 'Goal and acceptance criteria are required.' }
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
if ($Mode -eq 'task') {
    $bodyPath = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N') + '.md')
    try {
        $body = "## Goal`n$goal`n`n## Done when`n$acceptance`n`n## Constraints`nReuse existing code; add relevant regression tests. Preserve required checks and protected paths. Publish a draft PR; do not merge.`n"
        [IO.File]::WriteAllText($bodyPath, $body, [Text.UTF8Encoding]::new($false))
        $title = $goal.Substring(0, [Math]::Min(120, $goal.Length))
        $issue = & gh issue create --repo $target --title $title --body-file $bodyPath --label 'agent:ready'
        if ($LASTEXITCODE -ne 0) { throw 'Issue creation failed; no cycle was requested. Check GitHub before retrying.' }
        Write-Output "Task queued: $issue"
        # GitHub's label listing and events can lag a new issue by a minute or
        # two; a cycle started before both show it would miss the task.
        $number = ($issue.Trim() -split '/')[-1]
        $listed = $false
        $deadline = (Get-Date).AddMinutes(2)
        while (!$listed -and (Get-Date) -lt $deadline) {
            $ready = & gh api "repos/$target/issues?labels=agent:ready&state=open&per_page=50" --jq '.[].number'
            if ($LASTEXITCODE -ne 0) { throw 'GitHub API failed while checking the new task; no cycle was requested.' }
            $events = & gh api "repos/$target/issues/$number/events"
            if ($LASTEXITCODE -ne 0) { throw 'GitHub API failed while checking the new task; no cycle was requested.' }
            # Filtered here: Windows PowerShell drops quotes inside a jq argument.
            $labelled = @($events | ConvertFrom-Json |
                Where-Object { $_.event -eq 'labeled' -and $_.label.name -eq 'agent:ready' })
            $listed = (@($ready) -contains $number) -and ($labelled.Count -gt 0)
            if (!$listed -and (Get-Date).AddSeconds(5) -lt $deadline) { Start-Sleep -Seconds 5 }
            elseif (!$listed) { break }
        }
        if (!$listed) {
            # A cycle now would miss the task; the next scheduled cycle takes it.
            Write-Output 'GitHub does not list the task as ready yet. No cycle was started now;'
            Write-Output 'the next scheduled cycle (within 30 minutes) will take it. Or run: mimo start'
            return
        }
    } finally {
        if (Test-Path -LiteralPath $bodyPath) { Remove-Item -LiteralPath $bodyPath }
    }
}
Start-ScheduledTask -TaskName 'agent-dispatcher'
Write-Output 'MiMo-only mode saved. Requested one scheduled cycle; overlapping runs are ignored.'
Write-Output 'It takes the next agent:ready issue. Draft PRs and CI stay on GitHub.'
Write-Output 'Provider quota still applies. Exhausted providers wait; no paid capacity is enabled.'
Write-Output 'Progress: mimo status | Live log: mimo logs (Ctrl+C stops watching only).'
