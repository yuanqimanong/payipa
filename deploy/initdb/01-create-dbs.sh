#!/usr/bin/env sh
# PostgreSQL 官方镜像只会在空数据卷的首次初始化时执行本目录。
# 使用 psql 变量 + format('%I') 生成标识符，允许部署时自定义三个数据库名称。
# 镜像会 source 非可执行 .sh；子 shell 防止 set -u 泄漏到镜像入口脚本。
(
  set -eu

  create_database() {
    database_name=$1
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --set=ON_ERROR_STOP=1 --set="database_name=$database_name" <<'SQL'
SELECT format('CREATE DATABASE %I', :'database_name')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name') \gexec
SQL
  }

  if [ "$POSTGRES_DB" = "$PG_DB_DATA_CENTER" ] || [ "$POSTGRES_DB" = "$PG_DB_BUSINESS" ] || [ "$PG_DB_DATA_CENTER" = "$PG_DB_BUSINESS" ]; then
    echo "PG_DB_PYP、PG_DB_DATA_CENTER、PG_DB_BUSINESS 必须互不相同" >&2
    exit 1
  fi

  create_database "$PG_DB_DATA_CENTER"
  create_database "$PG_DB_BUSINESS"
)
