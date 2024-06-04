# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import json

import pytest
from tempo_cluster import TempoClusterRequirerAppData, TempoRole
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from scenario import Container, ExecOutput, Relation, State

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
            config={"role-ruler": True},
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
    assert state_out.unit_status == BlockedStatus("No roles assigned: please configure some roles")


@pytest.mark.parametrize(
    "roles",
    (
        ["alertmanager", "compactor"],
        ["alertmanager", "distributor"],
        ["alertmanager"],
        ["alertmanager", "query-frontend"],
        ["alertmanager", "ruler", "store-gateway"],
        ["alertmanager", "overrides-exporter", "ruler", "store-gateway"],  # order matters
    ),
)
def test_pebble_ready_plan(ctx, roles):
    expected_plan = {
        "services": {
            "tempo": {
                "override": "replace",
                "summary": "tempo worker daemon",
                "command": f"/bin/tempo --config.file=/etc/tempo/tempo-config.yaml -target {','.join(sorted(roles))} -auth.multitenancy-enabled=false",
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
            config={f"role-{role}": True for role in roles},
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

    assert state_out.unit_status == ActiveStatus("")


@pytest.mark.parametrize(
    "roles_config, expected",
    (
        ("querier", (TempoRole.querier,)),
        ("querier,ingester", (TempoRole.querier, TempoRole.ingester)),
        ("read,ingester", (TempoRole.query_frontend, TempoRole.querier, TempoRole.ingester)),
        ("read", (TempoRole.query_frontend, TempoRole.querier)),
        ("write", (TempoRole.distributor, TempoRole.ingester)),
        (
            "backend",
            (
                TempoRole.store_gateway,
                TempoRole.compactor,
                TempoRole.ruler,
                TempoRole.alertmanager,
                TempoRole.query_scheduler,
                TempoRole.overrides_exporter,
            ),
        ),
        ("all", tuple(TempoRole)),
    ),
)
def test_roles(ctx, roles_config, expected):
    out = ctx.run(
        "config-changed",
        state=State(
            leader=True,
            config={f"role-{x}": True for x in roles_config.split(",")},
            containers=[Container("tempo", can_connect=True)],
            relations=[Relation("tempo-cluster")],
        ),
    )
    if expected:
        data = TempoClusterRequirerAppData.load(
            out.get_relations("tempo-cluster")[0].local_app_data
        )
        assert set(data.roles) == set(expected)
    else:
        assert not out.get_relations("tempo-cluster")[0].local_app_data
