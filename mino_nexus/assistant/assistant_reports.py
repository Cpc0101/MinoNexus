"""IM 报告链接仅指向 Workbench 的登录保护详情页。"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from mino_nexus.core.database import session_scope
from mino_nexus.models.assistant import AssistantProfile


def normalize_base(value: str) -> str:
    value = str(value or '').strip().rstrip('/')
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname or ''
        address = ipaddress.ip_address(host) if ':' in host or host.replace('.', '').isdigit() else None
    except ValueError as exc:
        raise ValueError('请填写有效的 Workbench HTTP/HTTPS 入口') from exc
    if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Workbench 入口不能包含账号、查询参数或片段')
    if host.lower() == 'localhost' or any(c.isspace() for c in host) or (address and (address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local)):
        raise ValueError('Workbench 入口不能使用回环、通配或链路本地地址')
    host = f'[{address}]' if address and address.version == 6 else host.encode('idna').decode('ascii')
    netloc = host + (f':{port}' if port else '')
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip('/'), '', ''))


def agent_board_url(task_id: str, *, base_url: str) -> str:
    base = normalize_base(base_url)
    return f'{base}/testing/agents?{urlencode({"agentTask": task_id})}' if base and task_id else ''


def _lan_base() -> str:
    """同机部署默认入口；分机/反代部署通过显式 Workbench 地址覆盖。"""
    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))  # 只选择出站接口，不发送数据。
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    port = os.environ.get('MINO_WORKBENCH_PORT', '60104')
    for address in dict.fromkeys(candidates):
        try:
            return normalize_base(f'http://{address}:{port}')
        except ValueError:
            continue
    return ''


def configuration(uid: str) -> dict:
    with session_scope() as db:
        row = db.get(AssistantProfile, uid)
        saved = str((row.extra or {}).get('workbench_url') or '') if row else ''
    value = saved or os.environ.get('MINO_WORKBENCH_PUBLIC_URL', '')
    try:
        base = normalize_base(value) if value else _lan_base()
    except ValueError:
        base = ''
    return {'workbench_url': saved, 'effective_url': base,
            'source': 'configured' if saved else 'environment' if value else 'lan_default',
            'available': bool(base)}


def configure(uid: str, value: str) -> dict:
    base = normalize_base(value)
    with session_scope() as db:
        row = db.get(AssistantProfile, uid)
        if row is None:
            row = AssistantProfile(user_id=uid, extra={})
            db.add(row)
        row.extra = {**(row.extra or {}), 'workbench_url': base}
    return configuration(uid)


def report_url(doc: dict, task_id: str, *, base_url: str, case_id: str = '') -> str:
    base = normalize_base(base_url)
    app_id = str(doc.get('app_id') or '')
    if not base or not app_id or not task_id:
        return ''
    query = {'tab': 'tasks', 'run': str(task_id)}
    if case_id:
        query['tcase'] = case_id
    return f'{base}/testing/{quote(app_id, safe="")}?{urlencode(query)}'


def case_edit_url(app_id: str, case_id: str, *, base_url: str) -> str:
    base = normalize_base(base_url)
    if not base or not app_id or not case_id:
        return ''
    return f'{base}/testing/{quote(app_id, safe="")}?{urlencode({"tab": "cases", "case": case_id})}'
