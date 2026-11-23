"""执行器集合：本地执行器与子能力展开表，catalog 与资源侧共用。"""
from __future__ import annotations

LOCAL_EXECUTORS = {
    'assert_visual': 'vlm', 'persona_subtask': 'ai_persona',
    'human_input_text': 'hitl',
    # Flow-block program hook: observes the login form locally in prep/do.
    # It is not the check-only assert_visual capability (on any platform).
    'confirm_login_state': 'internal',
    'note': 'internal',
    'sample': 'internal',
}


# Catalog-dispatched children must have a real catalog row and phase contract.
CATALOG_CHILDREN = {
    'input_text': ('tap_element',),
    'confirm_login_state': (),
    'get_otp': ('plugin.gmail.fetch_otp',),
    'fsm_navigate': ('tap_element', 'press_key'),
    'operation.check_box': ('tap_element',),
    'accept_legal_consent': ('tap_element', 'press_key'), 'dismiss_ime': ('press_key',),
    'request_sms_code': ('tap_element', 'press_key'),
    'wait_screen_ready': ('probe_device_state', 'get_foreground_app'),
}

# Framework commands share the EXECUTE / RESULT envelope but are dispatched by
# RouterProxy directly. Their availability is a protocol concern; requiring
# catalog rows or phases here creates false runtime-contract gaps.
FRAMEWORK_CHILDREN = {
    'confirm_login_state': ('read_web_auth', 'hierarchy', 'screenshot'),
    'operation.check_box': ('hierarchy', 'screenshot'),
}

# Compatibility view for callers that need every possible child. Health
# diagnostics must use CATALOG_CHILDREN so framework commands stay outside
# catalog lifecycle checks.
CHILDREN = {
    parent: tuple(dict.fromkeys((*CATALOG_CHILDREN.get(parent, ()), *children)))
    for parent, children in FRAMEWORK_CHILDREN.items()
}
for _parent, _children in CATALOG_CHILDREN.items():
    CHILDREN[_parent] = tuple(dict.fromkeys((*_children, *FRAMEWORK_CHILDREN.get(_parent, ()))))
