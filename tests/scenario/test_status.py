from functools import partial
from unittest.mock import patch

import pytest
from cosl.coordinated_workers.interface import ClusterProviderAppData
from ops import ActiveStatus, WaitingStatus
from scenario import Context, State, Container, Relation

from charm import TempoWorkerK8SOperatorCharm
from tests.scenario.conftest import _urlopen_patch


@pytest.fixture
def ctx():
    return Context(TempoWorkerK8SOperatorCharm)


def test_status_check_starting(ctx):
    # GIVEN getting the status returns "Starting: X"
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)

    with patch(
            "urllib.request.urlopen", new=partial(_urlopen_patch, resp="foo\nStarting: 10\n bar")
    ):
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[Container("tempo", can_connect=True)],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == WaitingStatus("Starting...")


def test_status_check_ready(ctx):
    # GIVEN getting the status returns "ready"
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)

    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, resp="ready")):
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[Container("tempo", can_connect=True)],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == ActiveStatus("(all roles) ready.")
