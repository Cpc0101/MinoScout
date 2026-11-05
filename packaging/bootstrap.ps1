# Mino Scout 远程安装（Windows）。Workbench 复制的 PowerShell 命令。
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$NexusUrl = "http://mino.local:50104",
  [string]$StudioId = "",
  [string]$ReleaseVersion = "",
  [string]$ManifestUrl = ""
)

$ErrorActionPreference = "Stop"
$Repo = if ($env:MINO_SCOUT_GITHUB_REPO) { $env:MINO_SCOUT_GITHUB_REPO } else { "pengchengluoyi/MinoScout" }

if (-not $ManifestUrl) {
  if ($ReleaseVersion) {
    $ver = $ReleaseVersion.TrimStart('v')
    $ManifestUrl = "https://github.com/$Repo/releases/download/v$ver/manifest.json"
  } else {
    $ManifestUrl = "https://github.com/$Repo/releases/latest/download/manifest.json"
  }
}

if ($env:OS -ne "Windows_NT") { throw "此安装命令仅支持 Windows" }
$Prefix = if ($env:MINO_SCOUT_HOME) { $env:MINO_SCOUT_HOME } else { Join-Path $env:USERPROFILE ".mino\scout" }
$env:MINO_SCOUT_HOME = $Prefix
$ScoutOs = "win32"
$machineArch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$ScoutArch = switch ($machineArch.ToUpper()) {
  "ARM64" { "arm64" }
  "AMD64" { "x64" }
  default { throw "暂不支持 CPU 架构：$machineArch" }
}

Write-Host "→ manifest $ManifestUrl"
$manifestJson = Invoke-RestMethod -Uri $ManifestUrl -UseBasicParsing
$row = $null
if ($manifestJson.items) {
  foreach ($it in $manifestJson.items) {
    $osName = if ($it.os -in @("windows", "win")) { "win32" } else { $it.os }
    $archName = if ($it.arch -in @("amd64", "x86_64")) { "x64" } elseif ($it.arch -eq "aarch64") { "arm64" } else { $it.arch }
    if ($osName -eq $ScoutOs -and $archName -eq $ScoutArch) {
      $row = $it; break
    }
  }
} else {
  if ($manifestJson.os -in @("win32", "windows", "win") -and $manifestJson.arch -eq $ScoutArch) { $row = $manifestJson }
}
if (-not $row -or -not $row.url) { throw "manifest 里没有 $ScoutOs-$ScoutArch 安装包" }

$work = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("mino-scout-bootstrap-" + [guid]::NewGuid()))
$zip = Join-Path $work.FullName "package.zip"
Invoke-WebRequest -Uri $row.url -OutFile $zip -UseBasicParsing
if ($row.sha256) {
  $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
  if ($hash -ne $row.sha256.ToLower()) { throw "sha256 mismatch" }
}
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $work.FullName "extract") -Force
$installSh = Get-ChildItem -Path (Join-Path $work.FullName "extract") -Recurse -Filter "install.ps1" | Select-Object -First 1
if (-not $installSh) { throw "zip 内找不到 install.ps1" }
$configPath = Join-Path $Prefix "config.json"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
$appVer = if ($row.version) { [string]$row.version } elseif ($manifestJson.version) { [string]$manifestJson.version } else { "" }
$studioClean = if ($StudioId) { ($StudioId.ToLower() -replace '[^a-z0-9]', '') } else { "" }
$obj = [ordered]@{}
if (Test-Path -LiteralPath $configPath) {
  try {
    $existing = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not ($existing -is [System.Management.Automation.PSCustomObject])) { throw "配置必须是对象" }
    foreach ($property in $existing.PSObject.Properties) { $obj[$property.Name] = $property.Value }
  } catch { throw "现有 Scout 配置无法解析；原配置已保留：$configPath" }
}
$obj.pending_nexus_url = $NexusUrl.TrimEnd('/')
$obj.pending_install_token = $Token
$obj.version = $appVer
if ($studioClean) { $obj.studio_id = $studioClean }
$obj | ConvertTo-Json -Depth 50 | Set-Content -LiteralPath "$configPath.tmp" -Encoding UTF8
Move-Item -LiteralPath "$configPath.tmp" -Destination $configPath -Force
Write-Host "→ 配置已写入 $configPath"
Push-Location $installSh.DirectoryName
try { & $installSh.FullName } finally { Pop-Location }
Write-Host "Mino Scout 安装完成。账号绑定结果请在 Workbench 查看；拒绝原因见 Scout 日志。Chromium 将在首次接入后自动下载。"
