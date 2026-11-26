"""Optional callback transaction fence; ordinary requests have no transport scope."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

_factory = ContextVar("callback_transaction_fence", default=None)


@contextmanager
def bind_callback_scope(factory):
    token = _factory.set(factory)
    try:
        yield
    finally:
        _factory.reset(token)


def callback_boundary():
    factory = _factory.get()
    return factory() if factory is not None else nullcontext()


def require_callback_current():
    with callback_boundary():
        pass
