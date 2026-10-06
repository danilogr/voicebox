"""What every plugin's entry points build the same way."""

import logging
import sys

import pytest

from speak import runtime


@pytest.fixture
def restore_loggers():
    names = ("walkie", runtime.CORE_LOGGER)
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def added(name: str, before: list[logging.Handler]) -> list[logging.Handler]:
    return [h for h in logging.getLogger(name).handlers if h not in before]


def test_the_plugin_and_the_core_both_log_to_stderr_under_the_plugins_name(restore_loggers):
    before = {n: list(logging.getLogger(n).handlers) for n in ("walkie", runtime.CORE_LOGGER)}
    runtime.configure_logging("walkie")
    for name in ("walkie", runtime.CORE_LOGGER):
        (handler,) = added(name, before[name])
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
        assert handler.formatter is not None
        assert handler.formatter._fmt == "[walkie] %(message)s"
        assert logging.getLogger(name).level == logging.INFO
        assert logging.getLogger(name).propagate is False


def test_a_plugin_named_like_the_core_package_gets_one_handler(restore_loggers):
    before = list(logging.getLogger(runtime.CORE_LOGGER).handlers)
    runtime.configure_logging(runtime.CORE_LOGGER)
    assert len(added(runtime.CORE_LOGGER, before)) == 1


def test_an_info_line_from_a_core_module_reaches_stderr(restore_loggers, capsys):
    """Without a handler of its own, a core logger falls through to Python's
    last-resort handler, which drops everything below WARNING."""
    runtime.configure_logging("walkie")
    logging.getLogger(f"{runtime.CORE_LOGGER}.supervisor").info("hello from core")
    assert "[walkie] hello from core" in capsys.readouterr().err
