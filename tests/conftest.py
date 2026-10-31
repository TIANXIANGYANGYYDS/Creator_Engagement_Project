"""Keep intentional test failures out of the production API log."""
import logging
from logging.handlers import RotatingFileHandler

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_application_log(tmp_path_factory):
    from app.core import logging as app_logging

    root = tmp_path_factory.mktemp("application-logs")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app_logging, "PROJECT_ROOT", root)
        yield
    logger = logging.getLogger("app")
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename.startswith(str(root) + "/"):
            logger.removeHandler(handler)
            handler.close()
