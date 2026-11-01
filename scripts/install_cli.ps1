<#
.SYNOPSIS
    安装 file-tool 全局 CLI（幂等，可重复执行）。

.DESCRIPTION
    在 Windows 上装一个 `file-tool` 命令，让它指向本仓库的 .venv。

    launcher 固定指向仓库内的解释器 + PYTHONPATH，仓库改动后无需重装 ——
    和 scripts/install_cli.sh 是同一套语义，只是换了平台的壳。

.PARAMETER BinDir
    launcher 放哪个目录。默认 %LOCALAPPDATA%\file-tool\bin。

.PARAMETER AddToPath
    把 launcher 目录写进当前用户的 PATH（不动系统 PATH）。
    已经在了就跳过；改动在**新开的终端**里才生效。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_cli.ps1 -AddToPath
#>
[CmdletBinding()]
param(
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA "file-tool\bin"),
    [switch]$AddToPath
)

$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Write-Host "仓库：$Repo"

# ---- 准备虚拟环境（没有就建） ----
$VenvDir = Join-Path $Repo '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host "创建虚拟环境：$VenvDir"
    & python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败（需要 Python 3.11+ 且 python 在 PATH 上）"
    }
} else {
    Write-Host "复用已有虚拟环境：$VenvDir"
}

# ---- 装依赖（可编辑模式，改代码立即生效） ----
Write-Host "安装依赖……"
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -e "$Repo" --quiet
if ($LASTEXITCODE -ne 0) {
    throw "安装依赖失败"
}

# ---- 生成 launcher ----
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Launcher = Join-Path $BinDir 'file-tool.cmd'

$cmd = @"
@echo off
REM file-tool CLI launcher（由 scripts\install_cli.ps1 生成，勿手改）
setlocal
set "REPO=$Repo"
set "PY=$VenvPython"
if not exist "%PY%" set "PY=python"
if defined PYTHONPATH (
    set "PYTHONPATH=%REPO%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%REPO%"
)
"%PY%" -m file_tool.cli %*
"@

# 写成 UTF-8 无 BOM —— .cmd 里带 BOM 会在第一行多出乱码字符
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($Launcher, $cmd, $utf8NoBom)

Write-Host "file-tool CLI 已安装：$Launcher"

# ---- 可选：写进用户 PATH ----
if ($AddToPath) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $already = $false
    if ($userPath) {
        $already = ($userPath -split ';') -contains $BinDir
    }
    if ($already) {
        Write-Host "PATH 里已经有 $BinDir，跳过。"
    } else {
        $newPath = if ($userPath) { "$userPath;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host "已把 $BinDir 加入当前用户的 PATH。"
        Write-Host "提示：新开一个终端后 `file-tool` 才能直接用。"
    }
} else {
    Write-Host "提示：把 $BinDir 加入 PATH 后就能直接敲 file-tool。"
}
