import pytest
from charm import TempoWorkerK8SOperatorCharm
from scenario import Context, ExecOutput


@pytest.fixture(autouse=True)
def patch_all():
    # with patch("charm.TempoWorkerK8SOperatorCharm._current_tempo_config", PropertyMock(return_value={})):
    # with patch("charm.TempoWorkerK8SOperatorCharm._set_alerts", Mock(return_value=True)):
    yield


@pytest.fixture
def ctx():
    return Context(TempoWorkerK8SOperatorCharm)


TEMPO_VERSION_EXEC_OUTPUT = ExecOutput(stdout="1.31")
