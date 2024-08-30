#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import unittest
from unittest.mock import patch
from uuid import uuid4

import ops
from charm import TempoWorkerK8SOperatorCharm
from ops.testing import Harness

ops.testing.SIMULATE_CAN_CONNECT = True

k8s_resource_multipatch = patch.multiple(
    "cosl.coordinated_workers.worker.KubernetesComputeResourcesPatch",
    _namespace="test-namespace",
    _patch=lambda _: None,
)
lightkube_client_patch = patch("lightkube.core.client.GenericSyncClient")


@patch("cosl.coordinated_workers.worker.Worker.running_version", lambda *_: "1.2.3")
@patch("cosl.coordinated_workers.worker.Worker.restart", lambda *_: True)
@k8s_resource_multipatch
@lightkube_client_patch
class TestCharm(unittest.TestCase):
    def setUp(self, *unused):
        self.harness = Harness(TempoWorkerK8SOperatorCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.handle_exec("tempo", ["update-ca-certificates", "--fresh"], result=0)
        self.harness.set_leader(True)

    def test_initial_hooks(self, *_):
        self.harness.set_model_info("foo", str(uuid4()))
        self.harness.begin_with_initial_hooks()
