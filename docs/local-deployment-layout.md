# 本地目录

开发与正式各用一套家目录，三端并列。macOS、Linux、Windows 用同一相对路径，不再按 Application Support、APPDATA 或 XDG 分叉。

| | 开发 | 正式 |
| --- | --- | --- |
| Scout | `~/.mino-dev/scout` | `~/.mino/scout` |
| Nexus | `~/.mino-dev/nexus` | `~/.mino/nexus` |
| Workbench | `~/.mino-dev/workbench` | `~/.mino/workbench` |

Nexus 的 `mino.db` 在 nexus 目录根上。Scout 的配置、`bin`、日志在 scout 目录内。Workbench 开发态由 Vite 提供，正式静态文件随 Nexus 包发布；本机安装状态放 workbench 目录。组件之间不共用配置、数据库或进程锁。开发进程不读正式环境变量。

Nexus 和 Workbench 都是本地后台 Web 服务，Scout 是执行进程；不会生成 `.app`。Workbench 在浏览器访问。GitHub Actions 构建发布包，本地安装器负责部署，两者不是相互替代的路线。

旧目录不自动搬迁，也不因为旁边还留着旧文件就改用旧位置。要沿用旧位置，把 `MINO_SCOUT_HOME` 或 `MINO_NEXUS_DATA_DIR` 显式指过去。

既有安装迁移必须停写、备份、复制到不存在的新目录、同步服务定义/命令入口/配置内的绝对路径，再启动并验收；原目录保留供回滚。严禁直接移动目录或对运行中的 SQLite 主文件做普通复制。
