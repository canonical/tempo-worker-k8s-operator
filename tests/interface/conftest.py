# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from charms.tempo_k8s.v1.charm_tracing import charm_tracing_disabled
from cosl import JujuTopology
from interface_tester import InterfaceTester
from ops import ActiveStatus
from ops.pebble import Layer
from scenario.state import Container, Mount, State
from unittest.mock import MagicMock
from cosl.coordinated_workers.worker import CONFIG_FILE

from charm import TempoWorkerK8SOperatorCharm


ready_mock = MagicMock()
ready_mock.read.return_value = b"ready"

topology_mock = MagicMock()
topology_mock.return_value = JujuTopology(
    model="testmodel",
    model_uuid="00000000-0000-0000-0000-000000000001",
    application="worker",
    unit="worker/0",
    charm_name="worker",
)

# Interface tests are centrally hosted at https://github.com/canonical/charm-relation-interfaces.
# this fixture is used by the test runner of charm-relation-interfaces to test tempo's compliance
# with the interface specifications.
# DO NOT MOVE OR RENAME THIS FIXTURE! If you need to, you'll need to open a PR on
# https://github.com/canonical/charm-relation-interfaces and change tempo's test configuration
# to include the new identifier/location.
@pytest.fixture
def interface_tester(interface_tester: InterfaceTester):
    td = tempfile.TemporaryDirectory()
    filename = f"config.yaml"
    conf_file = Path(td.name).joinpath(filename)
    conf_file.write_text("foo: bar")

    with patch.multiple(
        "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch",
        _namespace="test-namespace",
        _patch=lambda _: None,
        get_status=lambda _: ActiveStatus(""),
    ):
        with patch("urllib.request.urlopen", ready_mock):
            with patch("cosl.JujuTopology.from_charm", topology_mock):
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
                                        mounts={
                                            "worker-config": Mount(
                                                CONFIG_FILE,
                                                conf_file
                                            )
                                        }
                                    )
                                ],
                            ),
                        )
                        yield interface_tester
