# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import json
from unittest.mock import patch

import pytest
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from scenario import Container, ExecOutput, Relation, State
from tempo_cluster import TempoClusterRequirerAppData, TempoRole

from tests.scenario.conftest import TEMPO_VERSION_EXEC_OUTPUT


@pytest.mark.parametrize("evt", ["update-status", "config-changed"])
def test_status_cannot_connect_no_relation(ctx, evt):
    state_out = ctx.run(evt, state=State(containers=[Container("tempo", can_connect=False)]))
    assert state_out.unit_status == BlockedStatus(
        "Missing tempo-cluster relation to a tempo-coordinator charm"
    )


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
            config={"role": "ingester"},
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
    assert state_out.unit_status == WaitingStatus(
        "Waiting for coordinator to publish a tempo config"
    )


def test_status_bad_config(ctx):
    state_out = ctx.run(
        "config-changed",
        state=State(
            config={"role": "beeef"},
            containers=[Container("tempo", can_connect=True)],
            relations=[Relation("tempo-cluster")],
        ),
    )
    assert state_out.unit_status == BlockedStatus("Invalid `role` config value: 'beeef'.")


@pytest.mark.parametrize(
    "role",
    list(TempoRole),
)
@patch("tempo.Tempo.is_configured", new=lambda _: False)
def test_pebble_ready_plan(ctx, role):
    expected_plan = {
        "services": {
            "tempo": {
                "override": "replace",
                "summary": "tempo worker process",
                "command": f"/bin/tempo -config.file=/etc/tempo/tempo.yaml -target {role.value}",
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
        state=State(
            config={"role": role},
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
    )

    tempo_container_out = state_out.get_container(tempo_container)
    assert tempo_container_out.services.get("tempo").is_running() is True
    assert tempo_container_out.plan.to_dict() == expected_plan

    assert state_out.unit_status == ActiveStatus(f"{role.value} ready.")


@pytest.mark.parametrize(
    "role_str, expected",
    (
        ("all", TempoRole.all),
        ("querier", TempoRole.querier),
        ("query-frontend", TempoRole.query_frontend),
        ("ingester", TempoRole.ingester),
        ("distributor", TempoRole.distributor),
        ("compactor", TempoRole.compactor),
        ("metrics-generator", TempoRole.metrics_generator),
    ),
)
def test_role(ctx, role_str, expected):
    out = ctx.run(
        "config-changed",
        state=State(
            leader=True,
            config={"role": role_str},
            containers=[Container("tempo", can_connect=True)],
            relations=[Relation("tempo-cluster")],
        ),
    )
    if expected:
        data = TempoClusterRequirerAppData.load(
            out.get_relations("tempo-cluster")[0].local_app_data
        )
        assert data.role == expected
    else:
        assert not out.get_relations("tempo-cluster")[0].local_app_data
