# Install Mino Scout next to config.json and register a startup task.
#
# 分层安装：zip 里带哪几层就装哪几层，没带的层原地不动。与 install.sh 一一对应。
#
#   layers.txt + runtime\         → 替换 bin\mino-scout.exe 与 bin\_internal
#   layers.txt + app\             → 替换 bin\app
#   layers.txt + browser\         → 替换 bin\ms-playwright
#   mino_scout\ + pyproject.toml  → 源码包（dev）：venv + pip，需要 Python 3.12+
#
# **刻意不再整体删掉 bin**：浏览器层实测 781 MB、占安装体积 78%，而它几乎从不变。
# 层与目录的映射见 scripts/layers.py 的 PAYLOAD_DIRS —— 改那里要同步改这里和 install.sh。
# Keep UTF-8 BOM: Windows PowerShell 5.1 otherwise reads this file as ANSI.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:OS -ne "Windows_NT") { throw "此脚本仅支持 Windows" }
$InstallingIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($InstallingIdentity.IsSystem) { throw "PC 操作需要登录用户，请在用户桌面内安装 Scout" }
if ($env:MINO_SCOUT_HOME) {
  $Prefix = $env:MINO_SCOUT_HOME
} else {
  $Prefix = Join-Path $env:USERPROFILE ".mino\scout"

}

$env:MINO_SCOUT_HOME = $Prefix

$Logs = Join-Path $Prefix "logs"
$Dest = Join-Path $Prefix "bin"
$Bin = $null
$LayerNames = @("runtime", "app", "browser")

Write-Host "Mino Scout → $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix, $Logs | Out-Null

# ---------------- 层清单读取 ----------------

# `<层> <指纹> [k=v ...]`，# 开头是注释。刻意不解 JSON：与 install.sh 保持同一份格式。
function Read-LayerRows($path) {
  if (-not (Test-Path -LiteralPath $path)) { return @() }
  $rows = @()
  foreach ($line in Get-Content -LiteralPath $path) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#")) { continue }
    $rows += , ($t -split '\s+')
  }
  return $rows
}

function Get-ZipLayerKey($layer) {
  foreach ($row in Read-LayerRows (Join-Path $Root "layers.txt")) {
    if ($row[0] -eq $layer) { return $row[1] }
  }
  return $null
}

function Get-ZipLayerField($layer, $name) {
  foreach ($row in Read-LayerRows (Join-Path $Root "layers.txt")) {
    if ($row[0] -ne $layer) { continue }
    for ($i = 2; $i -lt $row.Count; $i++) {
      $kv = $row[$i] -split '=', 2
      if ($kv.Count -eq 2 -and $kv[0] -eq $name) { return $kv[1] }
    }
  }
  return $null
}

function Get-InstalledLayerKey($layer) {
  foreach ($row in Read-LayerRows (Join-Path $Dest "layers.txt")) {
    if ($row[0] -eq $layer) { return $row[1] }
  }
  return $null
}

function Set-InstalledLayer($layer, $key) {
  $f = Join-Path $Dest "layers.txt"
  $kept = @()
  foreach ($row in Read-LayerRows $f) {
    if ($row[0] -ne $layer) { $kept += "$($row[0]) $($row[1])" }
  }
  $kept += "$layer $key"
  Set-Content -LiteralPath $f -Value ($kept | Sort-Object) -Encoding ASCII
}

function Get-LayerTarget($layer) {
  switch ($layer) {
    "runtime" { return "" }
    "app"     { return "app" }
    "browser" { return "ms-playwright" }
    default   { throw "unknown layer: $layer" }
  }
}

# ---------------- 落地 ----------------

# 先拷到 .new 再改名，把"目标已删、新的还没到位"的窗口压到一次改名。
$script:Replacements = @()
$script:BackupRoot = Join-Path $Prefix ("update-backups\" + [guid]::NewGuid().ToString())
function Replace-Path($src, $dst) {
  $fresh = "$dst.new-" + [guid]::NewGuid().ToString()
  $saved = Join-Path $script:BackupRoot ([string]$script:Replacements.Count)
  Copy-Item -Recurse -LiteralPath $src -Destination $fresh
  $existed = Test-Path -LiteralPath $dst
  New-Item -ItemType Directory -Force -Path $script:BackupRoot | Out-Null
  if ($existed) { Move-Item -LiteralPath $dst -Destination $saved }
  $script:Replacements += @{path=$dst; saved=$saved; fresh=$fresh; existed=$existed}
  Move-Item -LiteralPath $fresh -Destination $dst
}
function Restore-Replacements {
  for ($i = $script:Replacements.Count - 1; $i -ge 0; $i--) {
    $entry = $script:Replacements[$i]
    if (Test-Path -LiteralPath $entry.path) { Move-Item -LiteralPath $entry.path -Destination ($entry.saved + '.failed') }
    if ($entry.existed) { Move-Item -LiteralPath $entry.saved -Destination $entry.path }
  }
}

function Install-Layer($layer) {
  $src = Join-Path $Root $layer
  $rel = Get-LayerTarget $layer
  if (-not $rel) {
    # runtime：逐个顶层条目替换，将来多出别的顶层文件也不用改脚本。
    foreach ($entry in Get-ChildItem -LiteralPath $src) {
      Replace-Path $entry.FullName (Join-Path $Dest $entry.Name)
    }
  } else {
    Replace-Path $src (Join-Path $Dest $rel)
  }
  Write-Host "  层 $layer → $(if ($rel) { $rel } else { 'bin\' })  ($(Get-ZipLayerKey $layer))"
}

# app 层单独更新时，必须确认本机 runtime 与它是同一套依赖 —— 否则新代码引用了
# 本机 runtime 里没有的依赖，换完就起不来，症状只是启动时一句 ImportError。
function Assert-AppGate {
  $need = Get-ZipLayerField "app" "requires_runtime"
  if (-not $need) { return }
  $have = Get-InstalledLayerKey "runtime"
  if (-not $have) {
    throw "app layer needs runtime $need but nothing is installed here. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) first."
  }
  if ($have -ne $need) {
    throw "app layer needs runtime $need but $have is installed. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) instead."
  }
}

function Install-Layers {
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null

  # app-only 增量：先过闸门，再动任何文件。
  if ((Test-Path -LiteralPath (Join-Path $Root "app")) -and
      -not (Test-Path -LiteralPath (Join-Path $Root "runtime"))) {
    Assert-AppGate
  }

  $present = 0
  foreach ($layer in $LayerNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $layer))) { continue }
    Install-Layer $layer
    Set-InstalledLayer $layer (Get-ZipLayerKey $layer)
    $present++
  }
  if ($present -eq 0) {
    throw "layers.txt found but no payload directory (runtime\ app\ browser\)"
  }

  $exe = Join-Path $Dest "mino-scout.exe"
  if (-not (Test-Path -LiteralPath $exe)) {
    throw "runtime layer missing: $exe not present after install. Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip)."
  }
  $cli = Join-Path $Dest "app\mino_scout\cli.py"
  if (-not (Test-Path -LiteralPath $cli)) {
    throw "app layer missing: $cli not present after install"
  }
  $script:Bin = $exe
}

function Install-Source {
  $App = Join-Path $Prefix "app"
  $Venv = Join-Path $Prefix "venv"
  $Py = $null
  foreach ($candidate in @("py", "python", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
  }
  if (-not $Py) {
    throw "This zip is a source payload and needs Python 3.12+. Prefer the frozen GitHub Release."
  }
  $pyArgs = @()
  if ((Split-Path $Py -Leaf) -eq "py.exe" -or (Split-Path $Py -Leaf) -eq "py") {
    $pyArgs = @("-3")
  }
  & $Py @pyArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
  if ($LASTEXITCODE -ne 0) {
    throw "This zip is a source payload and needs Python 3.12+"
  }
  New-Item -ItemType Directory -Force -Path $App | Out-Null
  $dstPkg = Join-Path $App "mino_scout"
  Replace-Path (Join-Path $Root "mino_scout") $dstPkg
  Replace-Path (Join-Path $Root "pyproject.toml") (Join-Path $App "pyproject.toml")
  & $Py @pyArgs -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw "Failed to prepare Scout Python environment" }
  $VenvPy = Join-Path $Venv "Scripts\python.exe"
  & $VenvPy -m pip install -U pip
  if ($LASTEXITCODE -ne 0) { throw "Failed to prepare pip" }
  & $VenvPy -m pip install "$App[desktop]"
  if ($LASTEXITCODE -ne 0) { throw "Failed to install Scout with Desktop SDK" }
  $script:Bin = Join-Path $Venv "Scripts\mino-scout.exe"
  if (-not (Test-Path -LiteralPath $script:Bin)) {
    throw "install failed: $($script:Bin) not created"
  }
}

# Validate app compatibility before stopping the existing installation.
if ((Test-Path -LiteralPath (Join-Path $Root 'app')) -and -not (Test-Path -LiteralPath (Join-Path $Root 'runtime'))) { Assert-AppGate }
$hash = [Security.Cryptography.SHA256]::Create()
try { $taskSuffix = -join ($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Prefix.ToLowerInvariant())) | ForEach-Object { $_.ToString('x2') }) }
finally { $hash.Dispose() }
$taskName = "Mino Scout " + $taskSuffix.Substring(0, 12)

# Stop only this installation's process; never kill all nodes by executable name.
if (-not $env:MINO_SCOUT_UPDATING -and -not $env:MINO_SCOUT_REGISTER_ONLY) {
  $pidFile = Join-Path $Prefix 'scout.pid'
  if (Test-Path -LiteralPath $pidFile) {
    $scoutPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$scoutPid)) {
      $process = Get-Process -Id $scoutPid -ErrorAction SilentlyContinue
      $allowed = @((Join-Path $Dest 'mino-scout.exe'), (Join-Path $Prefix 'venv\Scripts\mino-scout.exe'), (Join-Path $Prefix 'venv\Scripts\python.exe'))
      if ($process -and $process.Path -in $allowed) {
        $createdAt = ([DateTimeOffset]$process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
        @{pid=$scoutPid; created_at=$createdAt; requested_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()} |
          ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Prefix 'scout_stop.json') -Encoding UTF8
        if (-not $process.WaitForExit(15000)) {
          $current = Get-Process -Id $scoutPid -ErrorAction SilentlyContinue
          if ($current -and $current.StartTime -eq $process.StartTime) { Stop-Process -Id $scoutPid -Force }
        }
      } elseif ($process) { throw 'Scout PID does not belong to this installation; files were not changed.' }
    }
  }
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}

if ($env:MINO_SCOUT_REGISTER_ONLY) {
  $Bin = Join-Path $Dest 'mino-scout.exe'
  if (-not (Test-Path -LiteralPath $Bin)) { throw 'Installed Scout binary missing.' }
} else {
  $layerFile = Join-Path $Dest 'layers.txt'
  $layerBefore = if (Test-Path -LiteralPath $layerFile) { [IO.File]::ReadAllBytes($layerFile) } else { $null }
  try {
    if (Test-Path -LiteralPath (Join-Path $Root "layers.txt")) { Install-Layers }
    elseif ((Test-Path -LiteralPath (Join-Path $Root "mino_scout")) -and (Test-Path -LiteralPath (Join-Path $Root "pyproject.toml"))) { Install-Source }
    else { throw 'Installer payload missing.' }
  } catch {
    Restore-Replacements
    if ($null -ne $layerBefore) { [IO.File]::WriteAllBytes($layerFile, $layerBefore) }
    throw
  }
}

# CI 与本地自测；进程内热更不重注册计划任务。
if ($env:MINO_SCOUT_UPDATING) {
  Write-Host "In-place update finished (scheduled task registration skipped)."
} elseif ($env:MINO_SCOUT_SKIP_SERVICE) {
  Write-Host "MINO_SCOUT_SKIP_SERVICE set - not registering a scheduled task."
} else {
  $launcher = Join-Path $Prefix "run-scout.ps1"
  $homeLiteral = $Prefix.Replace("'", "''")
  $binLiteral = $Bin.Replace("'", "''")
  "`$env:MINO_SCOUT_HOME = '$homeLiteral'`r`nStart-Process -FilePath '$binLiteral' -ArgumentList 'run' -WorkingDirectory '$homeLiteral' -WindowStyle Hidden -Wait" | Set-Content -LiteralPath $launcher -Encoding UTF8
  $registered = $false
  try {
    $legacy = Get-ScheduledTask -TaskName 'Mino Scout' -ErrorAction SilentlyContinue
    if ($legacy -and ($legacy.Actions | Where-Object { $_.Execute -eq $Bin -or ($_.Arguments -and $_.Arguments.Contains($Prefix)) })) {
      Export-ScheduledTask -TaskName 'Mino Scout' | Set-Content -LiteralPath (Join-Path $Prefix 'legacy-task.xml') -Encoding UTF8
      Stop-ScheduledTask -TaskName 'Mino Scout' -ErrorAction SilentlyContinue
      Disable-ScheduledTask -TaskName 'Mino Scout' | Out-Null
    }
    $identity = $InstallingIdentity
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`"" -WorkingDirectory $Prefix
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $registered = $true
    Write-Host "Registered scheduled task: $taskName"
  } catch {
    Write-Host "Scheduled task failed ($($_.Exception.Message)); using Startup folder."
  }

  if (-not $registered) {
    $startup = [Environment]::GetFolderPath("Startup")
    $cmdPath = Join-Path $startup ("MinoScout-" + $taskSuffix.Substring(0, 12) + ".cmd")
    $startupHome = $Prefix.Replace('%', '%%')
    $startupLauncher = $launcher.Replace('%', '%%')
    $startupContent = "@echo off`r`nchcp 65001 >nul`r`nset `"MINO_SCOUT_HOME=$startupHome`"`r`nstart `"`" /b powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startupLauncher`""
    [IO.File]::WriteAllText($cmdPath, $startupContent, (New-Object Text.UTF8Encoding($false)))
    $env:MINO_SCOUT_HOME = $Prefix
    Start-Process -FilePath $Bin -ArgumentList "run" -WorkingDirectory $Prefix -WindowStyle Hidden
    Write-Host "Wrote $cmdPath"
  }
}

$installed = Join-Path $Dest "layers.txt"
if (Test-Path -LiteralPath $installed) {
  Write-Host "Installed layers:"
  foreach ($line in Get-Content -LiteralPath $installed) { Write-Host "  $line" }
}
Write-Host "Config file (existing identity and credentials preserved): $(Join-Path $Prefix 'config.json')"
Write-Host "Binary: $Bin"
Write-Host "Control: $Bin status | $Bin stop"
