"""proxy —— 受控出口/代理中转（11，多供应商）。

- `adapters`：三类 provider 适配器（tunnel/longlived/iplist）统一 `acquire_egress` + Egress 描述符 + passthrough。
- `store`：provider（api_config KEK 加密）+「代理配置」下拉项（出口组 mix / 单一 single / 不用代理）CRUD。
- `pool`：出口组选路（按传输健康）+ 溯源记录 + 聚合统计（per-IP / per-(出口×域)）→ 喂 07 调频与 monitor。

出口数据面 = 中转网关（方案 B）：agent 连一个中转地址、中转据 config_id 选路 → 真实代理 → 目标；
中转作为可独立部署的网络转发服务延后。**访问边界（11 §定案-8）**：中转只做受控路由/审计/传输故障切换，
应用层认证失败/授权拒绝/交互式验证必须暂停数据源、不自动切换账号/会话/出口。
"""

from __future__ import annotations

from payipa.proxy.adapters import (
    PASSTHROUGH,
    Egress,
    IpListAdapter,
    LonglivedAdapter,
    NoEgressAvailable,
    ProviderAdapter,
    TunnelAdapter,
    build_adapter,
)
from payipa.proxy.pool import egress_stats, proxy_stat, record_usage, select_egress
from payipa.proxy.store import (
    create_config,
    get_config,
    list_configs,
    list_providers,
    register_provider,
    resolve_provider,
)

__all__ = [
    "PASSTHROUGH",
    "Egress",
    "IpListAdapter",
    "LonglivedAdapter",
    "NoEgressAvailable",
    "ProviderAdapter",
    "TunnelAdapter",
    "build_adapter",
    "create_config",
    "egress_stats",
    "get_config",
    "list_configs",
    "list_providers",
    "proxy_stat",
    "record_usage",
    "register_provider",
    "resolve_provider",
    "select_egress",
]
