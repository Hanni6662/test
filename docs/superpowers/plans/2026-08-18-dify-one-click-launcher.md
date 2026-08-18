# Dify One-Click Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a silent Windows desktop shortcut named `Dify一键启动` that starts Docker Desktop when needed, starts the local Dify Compose stack, waits for readiness, and opens the Dify UI.

**Architecture:** A focused PowerShell launcher owns Docker readiness, the equivalent of `docker compose up -d`, HTTP readiness, browser launch, and error reporting. A Windows `.lnk` invokes that launcher with a hidden PowerShell window. A standalone PowerShell verification script checks syntax, shortcut metadata, container state, and HTTP availability.

**Tech Stack:** Windows PowerShell 5.1, Docker Desktop CLI, Docker Compose, WScript.Shell COM automation, Windows PresentationFramework message box.

---

### Task 1: Add the launcher verification script

**Files:**
- Create: `C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1`
- Test: `C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1`

- [ ] **Step 1: Write the failing verification script**

Create the test file with these checks:

```powershell
$ErrorActionPreference = 'Stop'
$launcher = 'C:\Users\admin\dify\Dify-OneClick-Launcher.ps1'
$shortcut = 'C:\Users\admin\Desktop\Dify一键启动.lnk'
$docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$composeDir = 'C:\Users\admin\dify\docker'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

Assert-True (Test-Path -LiteralPath $launcher) 'Launcher file is missing.'
Assert-True (Test-Path -LiteralPath $shortcut) 'Desktop shortcut is missing.'

$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
Assert-True ($errors.Count -eq 0) ('Launcher parse errors: ' + ($errors.Message -join '; '))

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
Assert-True ($link.TargetPath -eq "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe") 'Shortcut target is incorrect.'
Assert-True ($link.Arguments -match 'Dify-OneClick-Launcher\.ps1') 'Shortcut arguments do not reference the launcher.'
Assert-True ($link.WorkingDirectory -eq 'C:\Users\admin\dify') 'Shortcut working directory is incorrect.'
Assert-True ($link.WindowStyle -eq 7) 'Shortcut is not configured for a minimized/hidden launch.'

$rows = & $docker compose --project-directory $composeDir ps -a --format json | ConvertFrom-Json
$bad = @($rows | Where-Object { $_.Service -ne 'init_permissions' -and $_.State -ne 'running' })
$init = $rows | Where-Object Service -eq 'init_permissions'
Assert-True ($bad.Count -eq 0) 'One or more persistent Dify services are not running.'
Assert-True ($init.State -eq 'exited' -and $init.ExitCode -eq 0) 'init_permissions did not exit successfully.'

$response = Invoke-WebRequest -Uri 'http://localhost/' -UseBasicParsing -TimeoutSec 15
Assert-True ($response.StatusCode -eq 200) 'Dify UI did not return HTTP 200.'
Write-Output 'PASS: launcher syntax, shortcut metadata, containers, and Dify UI verified.'
```

- [ ] **Step 2: Run the test and verify the expected RED state**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1
```

Expected: exit code `1` with `Launcher file is missing.` because production files do not exist yet.

### Task 2: Implement the silent launcher

**Files:**
- Create: `C:\Users\admin\dify\Dify-OneClick-Launcher.ps1`
- Test: `C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1`

- [ ] **Step 1: Create the minimal launcher implementation**

```powershell
$ErrorActionPreference = 'Stop'
$docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$composeDir = 'C:\Users\admin\dify\docker'
$difyUrl = 'http://localhost/'

function Test-DockerReady {
    & $docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-ForCondition([scriptblock]$Condition, [int]$TimeoutSeconds) {
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    while ($stopwatch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (& $Condition) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Test-DifyReady {
    try {
        $response = Invoke-WebRequest -Uri $difyUrl -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    if (-not (Test-Path -LiteralPath $docker)) { throw 'Docker CLI was not found.' }
    if (-not (Test-Path -LiteralPath $dockerDesktop)) { throw 'Docker Desktop was not found.' }
    if (-not (Test-Path -LiteralPath (Join-Path $composeDir 'docker-compose.yaml'))) { throw 'Dify Compose configuration was not found.' }

    if (-not (Test-DockerReady)) {
        Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
        if (-not (Wait-ForCondition { Test-DockerReady } 300)) {
            throw 'Docker Desktop did not become ready within 5 minutes.'
        }
    }

    & $docker compose --project-directory $composeDir up -d *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start Dify.' }

    if (-not (Wait-ForCondition { Test-DifyReady } 300)) {
        throw 'Dify did not become ready within 5 minutes.'
    }

    Start-Process $difyUrl
    exit 0
} catch {
    Add-Type -AssemblyName PresentationFramework
    [void][Windows.MessageBox]::Show($_.Exception.Message, 'Dify startup failed', 'OK', 'Error')
    exit 1
}
```

- [ ] **Step 2: Verify launcher syntax independently**

Run:

```powershell
$tokens=$null; $errors=$null
[void][System.Management.Automation.Language.Parser]::ParseFile('C:\Users\admin\dify\Dify-OneClick-Launcher.ps1',[ref]$tokens,[ref]$errors)
if($errors.Count){$errors; exit 1}
```

Expected: exit code `0` and no parse errors.

### Task 3: Create the desktop shortcut

**Files:**
- Create: `C:\Users\admin\Desktop\Dify一键启动.lnk`
- Test: `C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1`

- [ ] **Step 1: Create the `.lnk` with exact metadata**

Run:

```powershell
$shortcutPath='C:\Users\admin\Desktop\Dify一键启动.lnk'
$powershell="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($shortcutPath)
$link.TargetPath=$powershell
$link.Arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Users\admin\dify\Dify-OneClick-Launcher.ps1"'
$link.WorkingDirectory='C:\Users\admin\dify'
$link.IconLocation='C:\Program Files\Docker\Docker\Docker Desktop.exe,0'
$link.Description='Start Docker Desktop and Dify, then open the Dify UI'
$link.WindowStyle=7
$link.Save()
```

- [ ] **Step 2: Run the verification script and confirm GREEN**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1
```

Expected: exit code `0` and `PASS: launcher syntax, shortcut metadata, containers, and Dify UI verified.`

### Task 4: Exercise the real shortcut and re-verify

**Files:**
- Verify: `C:\Users\admin\Desktop\Dify一键启动.lnk`
- Verify: `C:\Users\admin\dify\Dify-OneClick-Launcher.ps1`

- [ ] **Step 1: Launch the actual shortcut**

Run:

```powershell
Start-Process -FilePath 'C:\Users\admin\Desktop\Dify一键启动.lnk'
Start-Sleep -Seconds 15
```

Expected: the default browser opens `http://localhost/`; no persistent PowerShell window remains.

- [ ] **Step 2: Run fresh final verification**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\admin\dify\tests\Test-DifyOneClickLauncher.ps1
```

Expected: exit code `0` and the PASS line.

- [ ] **Step 3: Inspect the launcher process state**

Run:

```powershell
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'Dify-OneClick-Launcher\.ps1' }
if($running){$running | Select-Object ProcessId,CommandLine; exit 1}
Write-Output 'PASS: no launcher PowerShell process remains.'
```

Expected: exit code `0` and `PASS: no launcher PowerShell process remains.`

### Task 5: Record implementation state

**Files:**
- Modify: `docs/superpowers/plans/2026-08-18-dify-one-click-launcher.md`

- [ ] **Step 1: Mark completed checkboxes after all verification passes**

Change each completed `- [ ]` entry above to `- [x]`. Do not mark a step complete before its corresponding command succeeds.

- [ ] **Step 2: Commit only the implementation plan update**

Run:

```powershell
git add -- docs/superpowers/plans/2026-08-18-dify-one-click-launcher.md
git commit -m "docs: record Dify launcher implementation"
```

Expected: commit succeeds without staging unrelated workspace files.
