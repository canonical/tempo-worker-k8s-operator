#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.
"""

import logging

from ops import CollectStatusEvent
from ops.charm import CharmBase
from ops.model import BlockedStatus, ActiveStatus

from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from tempo import TempoWorker

logger = logging.getLogger(__name__)


@trace_charm(
    tracing_endpoint="_charm_tracing_endpoint",
    server_cert="_charm_tracing_cert",
    extra_types=[TempoWorker],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    def __init__(self, *args):
        super().__init__(*args)
        # FIXME take ports from tempo instead of using hardcoded ports set
        #  https://github.com/canonical/tempo-coordinator-k8s-operator/issues/106
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096, 14250)

        self.worker = TempoWorker(self)

        # event handling
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self._charm_tracing_endpoint, self._charm_tracing_cert = (
            self.worker.charm_tracing_config()
        )

    def _on_collect_status(self, e: CollectStatusEvent):
        # add Tempo worker-specific statuses
        roles = self.worker.roles
        if roles and len(roles) > 1:
            e.add_status(
                BlockedStatus(f"cannot have more than 1 enabled role: {roles}")
            )

        if (
            roles
            and self.worker.cluster.relation
            and not self.worker.cluster.get_remote_write_endpoints()
        ):
            if "all" in roles:
                e.add_status(
                    ActiveStatus(
                        "metrics-generator disabled. No prometheus remote-write relation configured on the coordinator"
                    )
                )
            elif "metrics-generator" in roles:
                e.add_status(
                    BlockedStatus(
                        "No prometheus remote-write relation configured on the coordinator"
                    )
                )

        # the worker will set its status after we've set ours,
        # so in case of a conflict ours will prevail
        self.worker.set_status(e)


if __name__ == "__main__":  # pragma: nocover
    import ops

    ops.main(TempoWorkerK8SOperatorCharm)
