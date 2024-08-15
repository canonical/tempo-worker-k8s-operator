#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import logging
import urllib.request
from enum import Enum
from typing import Optional

import ops
from cosl.coordinated_workers.worker import CONFIG_FILE, Worker
from ops import CollectStatusEvent, WaitingStatus
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus
from ops.pebble import Layer

from charms.tempo_k8s.v1.charm_tracing import trace_charm

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CA_PATH = "/usr/local/share/ca-certificates/ca.crt"


class WorkerStatus(Enum):
    starting = "starting"
    up = "up"
    down = "down"


class RolesConfigurationError(Exception):
    """Raised when the worker has an invalid role(s) set in its config."""


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
    extra_types=[Worker],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    _valid_roles = [
        "all",
        "querier",
        "query-frontend",
        "ingester",
        "distributor",
        "compactor",
        "metrics-generator",
    ]

    def __init__(self, *args):
        super().__init__(*args)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096, 14250)

        self.worker = Worker(
            charm=self,
            name="tempo",
            pebble_layer=self.generate_worker_layer,
            endpoints={"cluster": "tempo-cluster"},  # type: ignore
        )
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(
            self.on["tempo"].pebble_check_failed, self._on_tempo_pebble_check_failed
        )
        self.framework.observe(
            self.on["tempo"].pebble_check_recovered, self._on_tempo_pebble_check_recovered
        )

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        if endpoints := self.worker.cluster.get_tracing_receivers():
            return endpoints.get("otlp_http")

    @property
    def ca_cert_path(self) -> Optional[str]:
        """CA certificate path for tls tracing."""
        return CA_PATH

    def generate_worker_layer(self, worker: Worker) -> Layer:
        """Return the Pebble layer for the Worker.

        This method assumes that worker.roles is valid.
        """
        roles = worker.roles
        if not len(roles) == 1:
            raise RolesConfigurationError(
                f"Worker can only have 1 role configured. {len(roles)} found."
            )
        role = roles[0]
        if role not in self._valid_roles:
            raise RolesConfigurationError(
                f"Invalid role {role}. Should be one of {self._valid_roles}"
            )
        if role == "all":
            role = "scalable-single-binary"
        return Layer(
            {
                "summary": "tempo worker layer",
                "description": "pebble config layer for tempo worker",
                "services": {
                    "tempo": {
                        "override": "replace",
                        "summary": "tempo worker process",
                        "command": f"/bin/tempo -config.file={CONFIG_FILE} -target {role}",
                        "startup": "enabled",
                    }
                },
                "checks": {
                    "ready": {
                        "override": "replace",
                        "http": {"url": "http://localhost:3200/ready"},
                    }
                },
            }
        )

    def _on_tempo_pebble_check_failed(self, event: ops.PebbleCheckFailedEvent):
        if event.info.name == "ready":
            logger.warning(
                "Pebble `ready` check on localhost:3200/ready started to fail: worker node is down."
            )
            # collect-status will detect that we're not ready and set waiting status.

    def _on_tempo_pebble_check_recovered(self, event: ops.PebbleCheckFailedEvent):
        if event.info.name == "ready":
            logger.warning(
                "Pebble `ready` check on localhost:3200/ready is passing: worker node is up."
            )
            # collect-status will detect that we're ready and set active status.

    @property
    def status(self) -> WorkerStatus:
        try:
            with urllib.request.urlopen("http://localhost:3200/ready") as response:
                html: bytes = response.read()
                # response looks like
            # Some services are not Running:
            # Starting: 1
            # Running: 16
            raw_out = html.decode("utf-8").strip()
            if "Starting: " in raw_out:
                return WorkerStatus.starting
            elif raw_out == "ready":
                return WorkerStatus.up
            return WorkerStatus.down
        except Exception:
            logger.exception("Error while getting worker status.")
            return WorkerStatus.down

    def _on_collect_status(self, e: CollectStatusEvent):
        status = self.status
        if status == WorkerStatus.starting:
            e.add_status(WaitingStatus("Starting..."))
        elif status == WorkerStatus.down:
            e.add_status(BlockedStatus("node down (see logs)"))
        # the happy path is handled by Worker, who'll set "(<role>) ready."

        # add Tempo worker custom blocking conditions
        if self.worker.roles and self.worker.roles[0] not in self._valid_roles:
            e.add_status(
                BlockedStatus(
                    f"Invalid `role` config value: {self.config.get('role')!r}. Should be one of {self._valid_roles}"
                )
            )


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)
