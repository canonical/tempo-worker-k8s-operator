from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from scenario import Context, ExecOutput
from ops import ActiveStatus
from unittest.mock import patch

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
def worker_charm():
    with patch.multiple(
        "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch",
        _namespace="test-namespace",
        _patch=lambda _: None,
        get_status=lambda _: ActiveStatus(""),
    ):
        yield TempoWorkerK8SOperatorCharm


@pytest.fixture
def ctx(worker_charm):
    return Context(charm_type=worker_charm)


TEMPO_VERSION_EXEC_OUTPUT = ExecOutput(stdout="1.31")
