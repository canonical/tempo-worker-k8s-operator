# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import json
from functools import partial
from unittest.mock import patch, MagicMock

import pytest
from cosl.coordinated_workers.interface import ClusterRequirerAppData, ClusterRequirer
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from scenario import Container, ExecOutput, Relation, State

from tests.scenario.conftest import TEMPO_VERSION_EXEC_OUTPUT, _urlopen_patch
from tests.scenario.helpers import set_role


@pytest.fixture(autouse=True)
def patch_all():
    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, resp="ready")):
        yield


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
    assert state_out.unit_status == WaitingStatus("Waiting for `tempo` container")


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_no_config(ctx, evt):
    state_out = ctx.run(
        evt,
        state=State(
            containers=[Container("tempo", can_connect=True)],
            relations=[Relation("tempo-cluster")],
        ),
    )
    assert state_out.unit_status == WaitingStatus("Waiting for coordinator to publish a config")


@patch.object(ClusterRequirer, "get_worker_config", MagicMock(return_value={"config": "config"}))
def test_status_bad_config(ctx):
    with pytest.raises(Exception):
        ctx.run(
            "config-changed",
            state=State(
                config={"role": "beeef"},
                containers=[Container("tempo", can_connect=True)],
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
                            "tempo_config": json.dumps({"alive": "beef"}),
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
                containers=[Container("tempo", can_connect=True)],
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
