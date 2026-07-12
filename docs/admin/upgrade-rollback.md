# 升级与回滚

## 短停机升级

```bash
git pull --ff-only
uv sync --package pyp-server --locked
uv run pypctl upgrade --build
uv run pyp-admin schema-status
```

`upgrade` 固定执行：停止主控、冷备三库与对象卷、构建并启动当前镜像、运行 one-shot 迁移、等待 readiness、执行 smoke。升级前应先等待重要批次结束并暂停外部触发。

## 回滚原则

不要默认执行 Alembic downgrade。优先回滚到上一应用镜像；只有该版本明确声明迁移可逆且已做恢复演练时，才考虑 schema downgrade。若新 schema 与旧应用不兼容，使用升级前备份恢复到隔离或原环境：

```bash
uv run pypctl restore <升级前备份目录> --confirm RESTORE
uv run pypctl smoke
uv run pyp-admin schema-status
```

升级迁移会把缺少正式动态表台账的旧数据源送入 reconciliation。若 `schema-status` 报短码非法（例如旧短码含连字符），系统不会自动改名；请先确认历史表和外部引用，再创建符合“小写字母开头，仅含小写字母、数字、下划线”的新短码并做受控数据迁移。

恢复后重新签发需要重新入网的 Agent 凭证，并观察调度、Outbox、磁盘水位和错误分布。
