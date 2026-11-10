"""Versioned execution adapters shared by binding and dispatch.

No resolver, device access or model calls. Compile-time plans no longer exist:
用例正文由 Scout 直接执行，这里只保留事件合同的适配。
"""
from copy import deepcopy
from mino_nexus.contract.executor_sets import LOCAL_EXECUTORS  # noqa: F401
import hashlib
import json

PHASES = ('prep', 'do', 'check', 'cleanup')
# Navigation policy names and compile-time markers, not catalog_entries ids.
NON_CATALOG_EVENT_IDS = frozenset({'nav_target'})
POLICY_NAV_CAPS = ('tap_element', 'press_key')
# 旧 Scout 兼容：老版本上报的是抽象通道名（ui_native_input/system_shell…）。
# 新 Scout 直接上报具体事件 id，绑定走 cid in provides 这条主路径，不依赖本表。
ABSTRACT = {
    'tap_element': 'ui_native_input', 'multi_tap': 'ui_native_input',
    'long_press_element': 'ui_native_input', 'swipe_direction': 'ui_native_input',
    'swipe_element_to_element': 'ui_native_input', 'input_text': 'ui_input_text',
    'screenshot': 'ui_screenshot', 'hierarchy': 'ui_native_input',
    'probe_device_state': 'read_system_data', 'read_device_data': 'read_system_data',
    'get_app_version': 'read_system_data', 'get_foreground_app': 'read_system_data',
    'launch_app': 'app_launch_native', 'open_app': 'app_launch_native',
    # pm clear 通过 shell 执行；Scout 的 adb 执行器提供 system_shell。
    'clear_app_cache': 'system_shell', 'kill_app': 'app_force_stop',
    'close_app': 'app_force_stop', 'press_key': 'key_event', 'set_clipboard': 'clipboard_set',
    'wake_screen': 'key_event', 'dismiss_keyguard': 'system_shell',
    'open_url': 'ui_native_input', 'read_web_auth': 'ui_native_input',
    'reload_page': 'ui_native_input',
}
# Local orchestration still dispatches every child via RouterProxy. These are
# declared potential children; unavailable optional branches cannot execute.


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def adapt_event(value, *, platform, framework=False):
    """Versioned legacy assembly bridge; preserve explicit disabling and contracts."""
    from mino_nexus.catalog.exec_classes import LOCAL_EXECUTOR_IDS
    value = deepcopy(value)
    cid = value['id']
    if cid in {'persona_subtask', 'human_input_text'}:
        value['implementations'] = []  # Existing implementations explicitly report not implemented.
        return value
    if cid in LOCAL_EXECUTOR_IDS or cid.startswith(('recover_', 'signal_')) or cid in LOCAL_EXECUTORS:
        executor = LOCAL_EXECUTORS.get(cid, 'internal')
        value['implementations'] = [{'id': 'nexus.local.v1:' + cid, 'executor': executor, 'runs_on': 'nexus',
            'low_level': {}, 'params_contract': {'version': 1}, 'result_contract': {'version': 1},
            'device_hint_template': {}}]
        return value
    for impl in value.get('implementations') or []:
        impl.setdefault('runs_on', 'scout')
        impl.setdefault('params_contract', {'version': 1})
        impl.setdefault('result_contract', {'version': 1})
        impl.setdefault('device_hint_template', {'execution_mode': 'agent'})
        required = ABSTRACT.get(cid)
        if impl['executor'] == 'playwright' and cid in {'launch_app', 'open_app', 'get_foreground_app', 'close_app', 'press_key', 'hierarchy'}:
            required = 'ui_native_input'
        if required:
            impl.setdefault('requires_caps', [required])
    return value
