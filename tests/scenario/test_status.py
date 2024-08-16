from contextlib import contextmanager
from functools import partial
from unittest.mock import patch

import pytest
from cosl.coordinated_workers.interface import ClusterProviderAppData
from ops import ActiveStatus, WaitingStatus, BlockedStatus
from scenario import Context, State, Container, Relation

from charm import TempoWorkerK8SOperatorCharm
from tests.scenario.conftest import _urlopen_patch


@pytest.fixture
def ctx():
    return Context(TempoWorkerK8SOperatorCharm)

@contextmanager
def endpoint_starting():
    with patch(
            "urllib.request.urlopen", new=partial(_urlopen_patch, resp="foo\nStarting: 10\n bar")
    ):
        yield


@contextmanager
def endpoint_ready():
    with patch(
            "urllib.request.urlopen", new=partial(_urlopen_patch, resp="ready")
    ):
        yield


@contextmanager
def config_on_disk():
    with patch(
        "cosl.coordinated_workers.worker.Worker._running_worker_config", new=lambda _: True
    ):
        yield


def test_status_check_no_pebble(ctx, caplog):
    # GIVEN the container cannot connect
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)

    state = State(
        relations=[Relation("tempo-cluster", remote_app_data=db)],
        containers=[Container("tempo")],
    )
    # WHEN we run any event
    state_out = ctx.run("update_status", state)

    # THEN the charm sets blocked
    assert state_out.unit_status == BlockedStatus("node down (see logs)")
    # AND THEN the charm logs that the container isn't ready.
    assert "Container cannot connect. Skipping status check." in caplog.messages


def test_status_check_no_config(ctx, caplog):
    # GIVEN there is no config file on disk
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)

    state = State(
        relations=[Relation("tempo-cluster", remote_app_data=db)],
        containers=[Container("tempo", can_connect=True)],
    )
    # WHEN we run any event
    state_out = ctx.run("update_status", state)

    # THEN the charm sets blocked
    assert state_out.unit_status == BlockedStatus("node down (see logs)")
    # AND THEN the charm logs that the config isn't on disk
    assert "Config file not on disk. Skipping status check." in caplog.messages


def test_status_check_starting(ctx):
    # GIVEN getting the status returns "Starting: X"
    db = {}
    ClusterProviderAppData(worker_config="foo:12").dump(db)

    with (endpoint_starting(), config_on_disk()):
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

    with (endpoint_ready(), config_on_disk()):
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[Container("tempo", can_connect=True)],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == ActiveStatus("(all roles) ready.")
