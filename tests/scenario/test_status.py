from contextlib import contextmanager
from functools import partial
from unittest.mock import patch

import pytest
from cosl.coordinated_workers.interface import ClusterProviderAppData
from cosl.coordinated_workers.worker import CONFIG_FILE
from ops import ActiveStatus, WaitingStatus
from ops import BlockedStatus
from scenario import State, Container, Relation, Mount, Secret

from tests.scenario.conftest import _urlopen_patch
import json
from unittest.mock import MagicMock

from cosl.coordinated_workers.interface import ClusterRequirer

from tests.scenario.conftest import TEMPO_VERSION_EXEC_OUTPUT, UPDATE_CA_CERTS_EXEC_OUTPUT
from tests.scenario.helpers import set_role

tempo_container = Container("tempo", can_connect=True, execs={UPDATE_CA_CERTS_EXEC_OUTPUT})


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
    state_out = ctx.run(ctx.on.update_status(), state)

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
        containers=[tempo_container],
    )
    # WHEN we run any event
    state_out = ctx.run(ctx.on.update_status(), state)

    # THEN the charm sets blocked
    assert state_out.unit_status == BlockedStatus("node down (see logs)")


def test_status_check_starting(ctx, tmp_path):
    # GIVEN getting the status returns "Starting: X"
    db = {}
    ClusterProviderAppData(worker_config="some: yaml").dump(db)

    with endpoint_starting(), config_on_disk():
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[tempo_container],
        )
        # WHEN we run any event
        state_out = ctx.run(ctx.on.update_status(), state)
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
            secrets=[Secret(id="secret:privkey", tracked_content={})],
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[tempo_container],
        )
        # WHEN we run any event
        state_out = ctx.run(ctx.on.update_status(), state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == WaitingStatus("Starting...")


def test_status_check_ready(ctx, tmp_path):
    # GIVEN getting the status returns "ready"
    db = {}
    ClusterProviderAppData(worker_config="foo:12", remote_write_endpoints=[{"url": "test"}]).dump(
        db
    )
    cfg_file = tmp_path / "fake.config"
    cfg_file.write_text("some: yaml")

    with endpoint_ready(), config_on_disk():
        state = State(
            relations=[Relation("tempo-cluster", remote_app_data=db)],
            containers=[
                Container(
                    "tempo",
                    can_connect=True,
                    execs={
                        TEMPO_VERSION_EXEC_OUTPUT,
                        UPDATE_CA_CERTS_EXEC_OUTPUT,
                    },
                    mounts={"cfg": Mount(location=CONFIG_FILE, source=cfg_file)},
                )
            ],
        )
        # WHEN we run any event
        state_out = ctx.run(ctx.on.update_status(), state)
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
            TEMPO_VERSION_EXEC_OUTPUT,
            UPDATE_CA_CERTS_EXEC_OUTPUT,
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
            TEMPO_VERSION_EXEC_OUTPUT,
            UPDATE_CA_CERTS_EXEC_OUTPUT,
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


@pytest.mark.parametrize(
    "role_str, expected",
    (
        (
            "all",
            ActiveStatus(
                "metrics-generator disabled. No prometheus remote-write relation configured on the coordinator"
            ),
        ),
        ("querier", ActiveStatus("querier ready.")),
        ("query-frontend", ActiveStatus("query-frontend ready.")),
        ("ingester", ActiveStatus("ingester ready.")),
        ("distributor", ActiveStatus("distributor ready.")),
        ("compactor", ActiveStatus("compactor ready.")),
        (
            "metrics-generator",
            BlockedStatus("No prometheus remote-write relation configured on the coordinator"),
        ),
    ),
)
def test_status_remote_write_endpoints(role_str, expected, ctx):

    cluster_relation = Relation(
        "tempo-cluster",
        remote_app_data={
            "worker_config": json.dumps("some: yaml"),
        },
    )

    state = State(
        leader=True,
        containers=[tempo_container],
        relations=[cluster_relation],
    )

    with endpoint_ready(), config_on_disk():
        state_out = ctx.run(ctx.on.collect_unit_status(), set_role(state, role_str))
        assert state_out.unit_status == expected
