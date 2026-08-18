# Dify 一键启动快捷方式设计

## 目标

在 Windows 桌面创建名为“Dify一键启动”的快捷方式。用户双击后，无需看到命令窗口，即可启动 Docker Desktop、启动本地 Dify Compose 服务，并在服务可访问后打开 Dify Web 界面。

## 采用方案

桌面快捷方式调用一个存放在 `C:\Users\admin\dify` 下的 PowerShell 启动脚本。快捷方式使用隐藏窗口启动 PowerShell，避免命令窗口闪现。

未采用批处理文件，因为它更容易显示命令窗口，且等待与错误处理能力较弱。未采用 Windows 计划任务，因为该需求不需要额外的系统级配置或管理员权限。

## 启动流程

1. 检查 Docker CLI 与 Dify Compose 配置文件是否存在。
2. 调用 `docker info` 检查 Docker Engine 是否已就绪。
3. 若未就绪，则启动 Docker Desktop，并在最多 5 分钟内轮询 Docker Engine。
4. 在 `C:\Users\admin\dify\docker` 执行 `docker compose up -d`。
5. 在最多 5 分钟内轮询 `http://localhost/`。
6. 页面可访问后，使用系统默认浏览器打开 `http://localhost/`。

如果任一步骤失败或超时，脚本通过 Windows 消息框显示简洁错误原因，然后退出；不显示常驻命令窗口。

## 文件与边界

- 启动脚本：`C:\Users\admin\dify\Dify-OneClick-Launcher.ps1`
- 桌面快捷方式：`C:\Users\admin\Desktop\Dify一键启动.lnk`
- Dify Compose 目录：`C:\Users\admin\dify\docker`
- 访问地址：`http://localhost/`

脚本只负责启动，不停止、删除或重建 Dify 数据卷，不修改 Dify 的 `.env` 配置。

## 验证标准

- 启动脚本可被 PowerShell 正常解析。
- 快捷方式名称、目标、参数、工作目录和隐藏窗口设置正确。
- 运行脚本后 `docker compose ps` 显示全部常驻服务运行。
- `http://localhost/` 返回 HTTP 200。
- 快捷方式实际启动一次后，Dify 页面可访问。
