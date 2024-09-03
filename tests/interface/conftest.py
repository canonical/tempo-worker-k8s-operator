# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
from unittest.mock import patch

import pytest
from charms.tempo_k8s.v1.charm_tracing import charm_tracing_disabled
from interface_tester import InterfaceTester
from ops import ActiveStatus
from ops.pebble import Layer
from scenario.state import Container, State

from charm import TempoWorkerK8SOperatorCharm


# Interface tests are centrally hosted at https://github.com/canonical/charm-relation-interfaces.
# this fixture is used by the test runner of charm-relation-interfaces to test tempo's compliance
# with the interface specifications.
# DO NOT MOVE OR RENAME THIS FIXTURE! If you need to, you'll need to open a PR on
# https://github.com/canonical/charm-relation-interfaces and change tempo's test configuration
# to include the new identifier/location.
@pytest.fixture
def interface_tester(interface_tester: InterfaceTester):
    with patch.multiple(
        "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch",
        _namespace="test-namespace",
        _patch=lambda _: None,
        get_status=lambda _: ActiveStatus(""),
    ):
        with patch("lightkube.core.client.GenericSyncClient"):
            with charm_tracing_disabled():
                interface_tester.configure(
                    charm_type=TempoWorkerK8SOperatorCharm,
                    state_template=State(
                        leader=True,
                        containers=[
                            Container(
                                name="tempo",
                                can_connect=True,
                            )
                        ],
                        config={
                            "role": "all",
                        }
                    ),
                )
                yield interface_tester
