# Codex SessionStart on native Windows. Match session_start.sh's adoption
# check, but use the session cwd even when Claude's project env is inherited.
# A read failure is context for the agent, never a failed session.
$ErrorActionPreference = 'Continue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
try {
    $root = (Get-Location).Path
    $main = $root
    if (Get-Command git -CommandType Application -ErrorAction SilentlyContinue) {
        $records = @(& git -C $root worktree list --porcelain 2>$null)
        if ($LASTEXITCODE -eq 0 -and $records.Count -gt 0 -and $records[0].StartsWith('worktree ')) {
            $main = $records[0].Substring(9)
        }
    }
    $pointer = Join-Path $main '.symbion'
    if (-not (Test-Path -LiteralPath $pointer)) {
        if (Get-Command git -CommandType Application -ErrorAction SilentlyContinue) {
            $top = & git -C $root rev-parse --show-toplevel 2>$null
            if ($LASTEXITCODE -eq 0 -and $top) { $pointer = Join-Path $top '.symbion' }
        }
    }
    $store = $env:SYMBION_DIR
    if (-not $store -and (Test-Path -LiteralPath $pointer)) {
        try {
            $lines = [System.IO.File]::ReadAllLines($pointer, $utf8)
            foreach ($line in $lines) {
                $line = $line.Trim()
                if (-not $line) { continue }
                if ($line.StartsWith('~/') -or $line.StartsWith('~\')) {
                    $store = Join-Path $HOME $line.Substring(2)
                } elseif ([System.IO.Path]::IsPathRooted($line)) {
                    $store = $line
                } else {
                    $store = Join-Path $main $line
                }
                break
            }
        } catch {
            # Let symbion name an unreadable pointer instead of going silent.
            $store = $pointer
        }
    }
    if (-not $store) {
        $main = [System.IO.Path]::GetFullPath($main)
        $store = Join-Path (Split-Path $main -Parent) ((Split-Path $main -Leaf) + '-notes')
        if (-not (Test-Path -LiteralPath (Join-Path $store 'notes.jsonl'))) { exit 0 }
    }
    if (-not (Get-Command symbion -CommandType Application -ErrorAction SilentlyContinue)) {
        Write-Output "symbion: cannot read store at $store because 'symbion' is not on PATH -- see Install at https://github.com/phreakocious/symbion"
        exit 0
    }
    if (-not $env:SYMBION_AUTHOR) { $env:SYMBION_AUTHOR = 'codex' }
    # Native stderr must become plain stdout, which Codex passes to the agent.
    & symbion summary 2>&1 | ForEach-Object { Write-Output "$_" }
} catch {
    Write-Output "error: symbion session start: $($_.Exception.Message)"
}
exit 0
