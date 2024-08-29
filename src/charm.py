#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""
import logging
import yaml
from typing import Optional, Dict

from cosl.coordinated_workers.worker import CONFIG_FILE, Worker
from ops import CollectStatusEvent
from ops.charm import CharmBase
from ops.main import main
from ops.model import BlockedStatus, ActiveStatus
from ops.pebble import Layer
from cosl.juju_topology import JujuTopology

from charms.tempo_k8s.v1.charm_tracing import trace_charm


# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

CA_PATH = "/usr/local/share/ca-certificates/ca.crt"


class RolesConfigurationError(Exception):
    """Raised when the worker has an invalid role(s) set in its config."""


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
    extra_types=[Worker],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    def __init__(self, *args):
        super().__init__(*args)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096, 14250)
        self._topology = JujuTopology.from_charm(self)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.worker = Worker(
            charm=self,
            name="tempo",
            pebble_layer=self.generate_worker_layer,
            endpoints={"cluster": "tempo-cluster"},  # type: ignore
            readiness_check_endpoint=self.readiness_check_endpoint,
            resources_requests=self.get_resources_requests,
            container_name="tempo",
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

    @staticmethod
    def readiness_check_endpoint(worker: Worker) -> str:
        """Endpoint for worker readiness checks."""
        scheme = "https" if worker.cluster.get_tls_data() else "http"
        return f"{scheme}://localhost:3200/ready"

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
        if role == "all":
            role = "scalable-single-binary"

        if role == "scalable-single-binary" or role == "metrics-generator":
            self._add_juju_topolgy()
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
            }
        )

    def _add_juju_topolgy(self):
        """Modify the config file to add juju topology for
        `metrics-generator` metrics before running the pebble service.
        """
        tempo_container = self.unit.get_container("tempo")
        if not tempo_container.exists(CONFIG_FILE):
            logger.debug("Skipping adding juju topology. Config file does not exist yet.")
            return

        raw_data = tempo_container.pull(CONFIG_FILE).read()
        data = yaml.safe_load(raw_data)
        # if `metrics_generator` doesn't exist in config,
        # then it is not enabled, so no point of adding juju topology.
        if "metrics_generator" in data:
            if "registry" not in data["metrics_generator"]:
                data["metrics_generator"]["registry"] = {}
            data["metrics_generator"]["registry"]["external_labels"] = dict(
                self._topology.as_dict()
            )

            # write new data
            tempo_container.push(CONFIG_FILE, yaml.safe_dump(data))

    def get_resources_requests(self, _) -> Dict[str, str]:
        """Returns a dictionary for the "requests" portion of the resources requirements."""
        return {"cpu": "50m", "memory": "200Mi"}
            

    def _on_collect_status(self, e: CollectStatusEvent):
        # add Tempo worker-specific statuses
        if (
            self.worker.cluster.relation
            and self.worker.roles
            and not self.worker.cluster.get_prometheus_endpoints()
        ):
            if self.worker.roles[0] == "all":
                e.add_status(
                    ActiveStatus(
                        "`metrics-generator` disabled. No prometheus-remote-write relation configured"
                    )
                )
            elif self.worker.roles[0] == "metrics-generator":
                e.add_status(BlockedStatus("No prometheus-remote-write relation configured"))


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)
