# systemone-bridge 启动脚本（PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File start.ps1
# 首次运行会自动创建 .venv 并安装依赖（uv sync）。

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "[systemone-bridge] 首次运行：uv sync 安装依赖..."
    uv sync
}

# 无 config.toml 时提示按模板创建（仍可用内置默认值启动）
if (-not (Test-Path "config.toml")) {
    Write-Host "[systemone-bridge] 未发现 config.toml，使用 config.toml.example 默认值；建议复制模板后填写 api_key"
}

uv run systemone-bridge @args
