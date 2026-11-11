# Linux 部署与维护

本项目已在 Ubuntu 24.04、2 核 2 GiB 的单用户云服务器运行。下面使用通用配置，不含作者的服务器地址、组织或隧道标识。

## 本地与云端分别做什么

本地演示直接调用 Python 业务服务。远程实际使用由官方安全隧道承接 ChatGPT 请求，再启动 stdio MCP 子进程。MCP 程序自身不监听公网端口。

## 准备环境

```bash
sudo apt-get update
sudo apt-get install -y python3-venv unzip ca-certificates
mkdir -p /home/admin/personal-assistant
chmod 700 /home/admin/personal-assistant
# 将本项目代码放到上述目录后执行
cd /home/admin/personal-assistant
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python portfolio_demo.py
```

执行 README 中的完整测试命令。使用其他账号或目录时，需要同步修改 deploy 下所有服务文件中的路径及 User/Group。

## 安全隧道与凭据

通过官方 tunnel-client 发布渠道获取适合系统的版本，核对官方 SHA256 后解压到 `tools/tunnel-client/`。本次实际使用 v0.0.20；未来安装应以所选版本对应的官方文件为准。

复制 `deploy/tunnel.example.yaml` 为 `deploy/tunnel.yaml`，填写自己已有的隧道 ID。依照 OpenAI 安全隧道的访问控制流程完成组织、工作区和 ChatGPT 应用关联。

运行 API 凭据需要对应的 Tunnels Read + Use 权限。通过隐藏输入与受控的凭据管理方式，将 `CONTROL_PLANE_API_KEY` 配置给服务。示例 systemd 期望读取 `/etc/personal-assistant/tunnel.env`，该目录应为 root 所有、700 权限，文件为 root 所有、600 权限。不要把凭据提交到仓库或直接放入命令参数。

## 初始化与服务管理

新安装创建空数据库：

```bash
.venv/bin/python database.py --db data/personal.sqlite3
```

已有数据应先停止旧服务，使用 maintenance.py 一致性备份后迁移，避免双端同时写入。不要把演示库冒充真实业务数据。

检查客户端可执行权限、路径、数据库与凭据均准备好后：

```bash
sudo install -m 644 deploy/personal-assistant.service deploy/personal-assistant-backup.service deploy/personal-assistant-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now personal-assistant.service personal-assistant-backup.timer
sudo systemctl status personal-assistant --no-pager
sudo journalctl -u personal-assistant -n 30 --no-pager
```

服务异常退出后等待 10 秒重启。NoNewPrivileges、PrivateTmp 和只读系统目录减少进程影响范围，项目目录允许写入。它们不构成完整安全隔离或独立服务账号。

`deploy/health.url` 保存仅监听回环地址的健康检查地址，可据此访问 `/healthz` 与 `/readyz`。应用查询成功仍应通过 MCP 客户端进一步验证，不能只以进程存在判断业务正常。

## 备份与恢复

```bash
sudo systemctl start personal-assistant-backup
systemctl show personal-assistant-backup -p Result
systemctl list-timers personal-assistant-backup.timer
```

定时器按服务器时区每日 03:00 触发，随机延迟不超过 15 分钟，停机错过后补跑。备份在项目 `backups/`，数据库和 `.manifest.json` 必须成对保留。该配置没有自动异地传输或清理策略。

恢复时先停止主服务，使用下面模板；将 BACKUP.sqlite3 替换为已验证的备份路径：

```bash
sudo systemctl stop personal-assistant
.venv/bin/python maintenance.py verify backups/BACKUP.sqlite3
.venv/bin/python maintenance.py restore backups/BACKUP.sqlite3 --db data/personal.sqlite3 --confirm-replace
# 仅在验证和恢复成功后启动
sudo systemctl start personal-assistant
```

模式校验拒绝不匹配的数据库，不会自动执行未来版本的结构迁移。升级前应先准备兼容的迁移与回滚方案。
