from functools import partial
from unittest.mock import patch

import pytest
from cosl.coordinated_workers.interface import ClusterProviderAppData
from cosl.coordinated_workers.worker import CONFIG_FILE
from ops import ActiveStatus, WaitingStatus
from scenario import Context, State, Container, Relation, Mount

from charm import TempoWorkerK8SOperatorCharm
from tests.scenario.conftest import _urlopen_patch


@pytest.fixture
def ctx():
    return Context(TempoWorkerK8SOperatorCharm)


def test_status_check_starting(ctx, tmp_path):
    # GIVEN getting the status returns "Starting: X"
    db = {}
    ClusterProviderAppData(worker_config="some: yaml").dump(db)

    with patch(
        "urllib.request.urlopen", new=partial(_urlopen_patch, resp="foo\nStarting: 10\n bar")
    ):
        cfg_file = tmp_path / "fake.config"
        cfg_file.write_text("some: yaml")

        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[
                Container("tempo", can_connect=True, mounts={"cfg": Mount(CONFIG_FILE, cfg_file)})
            ],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == WaitingStatus("Starting...")


def test_status_check_ready(ctx, tmp_path):
    # GIVEN getting the status returns "ready"
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)
    cfg_file = tmp_path / "fake.config"
    cfg_file.write_text("some: yaml")

    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, resp="ready")):
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[
                Container("tempo", can_connect=True, mounts={"cfg": Mount(CONFIG_FILE, cfg_file)})
            ],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == ActiveStatus("(all roles) ready.")
