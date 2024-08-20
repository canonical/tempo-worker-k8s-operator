from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from scenario import Context, ExecOutput

from charm import TempoWorkerK8SOperatorCharm


@contextmanager
def _urlopen_patch(url: str, resp, tls: bool = False):
    if url == f"{'https' if tls else 'http'}://localhost:3200/ready":
        mm = MagicMock()
        mm.read = MagicMock(return_value=resp.encode("utf-8"))
        yield mm
    else:
        raise RuntimeError("unknown path")


@pytest.fixture
def ctx():
    return Context(TempoWorkerK8SOperatorCharm)


TEMPO_VERSION_EXEC_OUTPUT = ExecOutput(stdout="1.31")
