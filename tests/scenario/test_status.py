from contextlib import contextmanager
from functools import partial
from unittest.mock import patch

import pytest
from cosl.coordinated_workers.interface import ClusterProviderAppData
from cosl.coordinated_workers.worker import CONFIG_FILE
from ops import ActiveStatus, WaitingStatus
from ops import BlockedStatus
from scenario import Context, State, Container, Relation, Mount

from tests.scenario.conftest import _urlopen_patch
import json
from unittest.mock import MagicMock

from cosl.coordinated_workers.interface import ClusterRequirer
from scenario import ExecOutput

from tests.scenario.conftest import TEMPO_VERSION_EXEC_OUTPUT
from tests.scenario.helpers import set_role


@pytest.fixture
def ctx(worker_charm):
    return Context(worker_charm)


@contextmanager
def endpoint_starting(tls: bool = False):
    with patch(
        "urllib.request.urlopen",
        new=partial(_urlopen_patch, tls=tls, resp="foo\nStarting: 10\n bar"),
    ):
        yield


@contextmanager
def endpoint_ready(tls: bool = False):
    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, tls=tls, resp="ready")):
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


def test_status_check_starting(ctx, tmp_path):
    # GIVEN getting the status returns "Starting: X"
    db = {}
    ClusterProviderAppData(worker_config="some: yaml").dump(db)

    with endpoint_starting(), config_on_disk():
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[Container("tempo", can_connect=True)],
        )
        # WHEN we run any event
        state_out = ctx.run("update_status", state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == WaitingStatus("Starting...")


def test_status_check_tls(ctx, tmp_path):
    # GIVEN getting the status returns "Starting: X" and we have TLS enabled
    db = {}
    ClusterProviderAppData(
        worker_config="some: yaml",
        # simulate tls active
        ca_cert="cacert",
        server_cert="servercert",
        privkey_secret_id="privkey",
    ).dump(db)

    with endpoint_starting(tls=True), config_on_disk():
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[Container("tempo", can_connect=True)],
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

    with endpoint_ready(), config_on_disk():
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


@endpoint_ready
@patch.object(ClusterRequirer, "get_worker_config", MagicMock(return_value={"config": "config"}))
@patch(
    "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch.get_status",
    MagicMock(return_value=BlockedStatus("`juju trust` this application")),
)
def test_patch_k8s_failed(ctx):

    tempo_container = Container(
        "tempo",
        can_connect=True,
        exec_mock={
            ("/bin/tempo", "-version"): TEMPO_VERSION_EXEC_OUTPUT,
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        "config_changed",
        state=set_role(
            State(
                containers=[tempo_container],
                relations=[
                    Relation(
                        "tempo-cluster",
                        remote_app_data={
                            "tempo_config": json.dumps({"alive": "beef"}),
                        },
                    )
                ],
            ),
            "all",
        ),
    )

    assert state_out.unit_status == BlockedStatus("`juju trust` this application")


@endpoint_ready
@patch.object(ClusterRequirer, "get_worker_config", MagicMock(return_value={"config": "config"}))
@patch(
    "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch.get_status",
    MagicMock(return_value=WaitingStatus("")),
)
def test_patch_k8s_waiting(ctx):

    tempo_container = Container(
        "tempo",
        can_connect=True,
        exec_mock={
            ("/bin/tempo", "-version"): TEMPO_VERSION_EXEC_OUTPUT,
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        "config_changed",
        state=set_role(
            State(
                containers=[tempo_container],
                relations=[
                    Relation(
                        "tempo-cluster",
                        remote_app_data={
                            "tempo_config": json.dumps({"alive": "beef"}),
                        },
                    )
                ],
            ),
            "all",
        ),
    )

    assert state_out.unit_status == WaitingStatus("")
