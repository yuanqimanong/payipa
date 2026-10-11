# 备份与恢复

## 备份范围

`pypctl backup` 同时保存：

- `pyp_sys`、`data_center`、`business` 三个 PostgreSQL custom-format dump；
- 本地 `pyp_data` 对象卷；
- `deploy/.env.compose` 的副本；
- 版本信息、文件大小和 SHA-256 清单。

备份目录包含凭证密钥与业务数据，安全等级不低于生产数据库。应加密、限制访问并复制到独立故障域。建议内部版 RPO 24 小时、RTO 4 小时；业务要求更高时缩短周期。

```bash
uv run pypctl backup
uv run pypctl backup --output D:/payipa-backups/pre-upgrade
```

若主控正在运行，命令会短暂停止主控以获得三库和对象卷的静态备份，完成后恢复服务。Agent 会出站重连，不需要重新入网。

若无法查询服务状态或主控停止失败，命令会返回非零并终止备份。若备份已完成，但主控重启失败，命令仍返回非零，完整备份及其 `manifest.json` 会保留在输出目录；先检查 Compose 日志并恢复服务，不要删除这份备份。没有完整清单的目录属于未完成备份，不能用于恢复。

## 恢复

恢复会覆盖当前三库和对象卷。先保存当前现场，再执行：

```bash
uv run pypctl restore backups/20260712T120000Z --confirm RESTORE
uv run pypctl smoke
```

恢复流程在停服前校验清单：必须包含三个互不相同的数据库、三份 dump、对象卷和部署配置；文件大小与 SHA-256 必须匹配。三库名称须与当前及备份配置对应，当前 `CRED_KEK` 须与备份一致。既有 `pypctl backup` 生成的完整清单可继续使用，无需重新生成。

校验通过后停止主控和本机 Agent，再执行 `pg_restore --clean`、恢复对象卷、把 schema 迁移到当前 head，最后启动主控。校验失败不会停止服务；停服失败不会开始恢复数据。数据恢复或迁移失败时返回非零并保持主控停止，禁止在未知状态继续接收任务。

## 恢复演练

每个发布版本至少在隔离环境完成一次：恢复备份、检查三库 revision、运行 `pypctl smoke`、接入测试 Agent、执行内置示例采集、核对数据与工件。只生成备份但从不恢复，不算备份闭环。
