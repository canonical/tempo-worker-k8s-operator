# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import json
from functools import partial
from unittest.mock import patch, MagicMock
import yaml
import pytest
from cosl.coordinated_workers.interface import ClusterRequirerAppData, ClusterRequirer
from ops.model import ActiveStatus, BlockedStatus
from scenario import Container, ExecOutput, Relation, State, Mount

from tests.scenario.conftest import TEMPO_VERSION_EXEC_OUTPUT, _urlopen_patch
from tests.scenario.helpers import set_role
from cosl.coordinated_workers.worker import CONFIG_FILE
from cosl.juju_topology import JujuTopology


@pytest.fixture(autouse=True)
def patch_urllib_request():
    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, resp="ready")):
        yield


tempo_container = Container(
    "tempo", can_connect=True, exec_mock={("update-ca-certificates", "--fresh"): ExecOutput()}
)


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_cannot_connect_no_relation(ctx, evt):
    state_out = ctx.run(evt, state=State(containers=[Container("tempo", can_connect=False)]))
    assert state_out.unit_status == BlockedStatus("Missing relation to a coordinator charm")


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_cannot_connect(ctx, evt):
    container = Container(
        "tempo",
        can_connect=False,
        exec_mock={
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        evt,
        state=State(
            config={"role-ingester": True, "role-all": False},
            containers=[container],
            relations=[Relation("tempo-cluster")],
        ),
    )
    assert state_out.unit_status == BlockedStatus("node down (see logs)")


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_no_config(ctx, evt):
    state_out = ctx.run(
        evt,
        state=State(
            containers=[tempo_container],
            relations=[Relation("tempo-cluster")],
        ),
    )
    assert state_out.unit_status == BlockedStatus("node down (see logs)")


@patch.object(ClusterRequirer, "get_worker_config", MagicMock(return_value={"config": "config"}))
def test_status_bad_config(ctx):
    with pytest.raises(Exception):
        ctx.run(
            "config-changed",
            state=State(
                config={"role": "beeef"},
                containers=[tempo_container],
                relations=[Relation("tempo-cluster")],
            ),
        )


@pytest.mark.parametrize(
    "role",
    [
        "all",
        "querier",
        "query-frontend",
        "ingester",
        "distributor",
        "compactor",
        "metrics-generator",
    ],
)
@patch.object(ClusterRequirer, "get_worker_config", MagicMock(return_value={"config": "config"}))
def test_pebble_ready_plan(ctx, role):
    expected_plan = {
        "services": {
            "tempo": {
                "override": "replace",
                "summary": "tempo worker process",
                "command": f"/bin/tempo -config.file=/etc/worker/config.yaml -target {role if role != 'all' else 'scalable-single-binary'}",
                "startup": "enabled",
            }
        },
    }

    tempo_container = Container(
        "tempo",
        can_connect=True,
        exec_mock={
            ("/bin/tempo", "-version"): TEMPO_VERSION_EXEC_OUTPUT,
            ("update-ca-certificates", "--fresh"): ExecOutput(),
        },
    )
    state_out = ctx.run(
        tempo_container.pebble_ready_event,
        state=set_role(
            State(
                containers=[tempo_container],
                relations=[
                    Relation(
                        "tempo-cluster",
                        remote_app_data={
                            "worker_config": json.dumps("beef"),
                            "remote_write_endpoints": json.dumps([{"url": "http://test:3000"}]),
                        },
                    )
                ],
            ),
            role,
        ),
    )

    tempo_container_out = state_out.get_container(tempo_container)
    assert tempo_container_out.services.get("tempo").is_running() is True
    assert tempo_container_out.plan.to_dict() == expected_plan

    if role == "all":
        assert state_out.unit_status == ActiveStatus("(all roles) ready.")
    else:
        assert state_out.unit_status == ActiveStatus(f"{role} ready.")


@pytest.mark.parametrize(
    "role_str, expected",
    (
        ("all", "all"),
        ("querier", "querier"),
        ("query-frontend", "query-frontend"),
        ("ingester", "ingester"),
        ("distributor", "distributor"),
        ("compactor", "compactor"),
        ("metrics-generator", "metrics-generator"),
    ),
)
def test_role(ctx, role_str, expected):
    out = ctx.run(
        "config-changed",
        state=set_role(
            State(
                leader=True,
                containers=[tempo_container],
                relations=[Relation("tempo-cluster")],
            ),
            role_str,
        ),
    )
    if expected:
        data = ClusterRequirerAppData.load(out.get_relations("tempo-cluster")[0].local_app_data)
        assert data.role == expected
    else:
        assert not out.get_relations("tempo-cluster")[0].local_app_data


@pytest.mark.parametrize(
    "role_str",
    (
        ("all"),
        ("metrics-generator"),
    ),
)
@patch.object(JujuTopology, "from_charm")
def test_config_juju_topology(topology_mock, role_str, ctx, tmp_path):

    charm_topo = JujuTopology(
        model="test",
        model_uuid="00000000-0000-4000-8000-000000000000",
        application="worker",
        unit="worker/0",
        charm_name="tempo",
    )
    topology_mock.return_value = charm_topo
    cfg_file = tmp_path / "fake.config"
    data = yaml.safe_dump({"metrics_generator": {}})
    cfg_file.write_text("some: yaml")

    cluster_relation = Relation(
        "tempo-cluster",
        remote_app_data={
            "worker_config": json.dumps(data),
            "remote_write_endpoints": json.dumps([{"url": "http://prometheus:3000/push"}]),
        },
    )

    state = State(
        leader=True,
        containers=[
            Container(
                "tempo",
                can_connect=True,
                mounts={"cfg": Mount(CONFIG_FILE, cfg_file)},
                exec_mock={
                    ("update-ca-certificates", "--fresh"): ExecOutput(),
                },
            )
        ],
        relations=[cluster_relation],
    )

    ctx.run(cluster_relation.changed_event, state=set_role(state, role_str))

    # assert that topology is added to config
    updated_config = yaml.safe_load(cfg_file.read_text())
    assert updated_config == {
        "metrics_generator": {"registry": {"external_labels": dict(charm_topo.as_dict())}}
    }
