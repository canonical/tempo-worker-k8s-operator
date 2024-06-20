#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Tempo worker charm.
This charm deploys a Tempo worker application on k8s Juju models.

Integrate it with a `tempo-k8s` coordinator unit to start.
"""

import logging
import socket
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from charms.loki_k8s.v1.loki_push_api import _PebbleLogClient
from charms.tempo_k8s.v1.charm_tracing import trace_charm
from cosl import JujuTopology
from ops.charm import CharmBase, CollectStatusEvent
from ops.framework import BoundEvent, Object
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from tempo import Tempo
from tempo_cluster import (
    ConfigReceivedEvent,
    TempoClusterRequirer,
    TempoRole,
)

CA_CERT_PATH = Path("/usr/local/share/ca-certificates/ca.crt")

TEMPO_DIR = "/tempo"

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class ManualLogForwarder(Object):
    """Forward the standard outputs of all workloads to explictly-provided Loki endpoints."""

    def __init__(
        self,
        charm: CharmBase,
        *,
        loki_endpoints: Optional[Dict[str, str]],
        refresh_events: Optional[List[BoundEvent]] = None,
    ):
        _PebbleLogClient.check_juju_version()
        super().__init__(charm, "tempo-cluster")
        self._charm = charm
        self._loki_endpoints = loki_endpoints
        self._topology = JujuTopology.from_charm(charm)

        if not refresh_events:
            return

        for event in refresh_events:
            self.framework.observe(event, self.update_logging)

    def update_logging(self, _=None):
        """Update the log forwarding to match the active Loki endpoints."""
        loki_endpoints = self._loki_endpoints

        if not loki_endpoints:
            logger.warning("No Loki endpoints available")
            loki_endpoints = {}

        for container in self._charm.unit.containers.values():
            _PebbleLogClient.disable_inactive_endpoints(
                container=container,
                active_endpoints=loki_endpoints,
                topology=self._topology,
            )
            _PebbleLogClient.enable_endpoints(
                container=container, active_endpoints=loki_endpoints, topology=self._topology
            )

    def disable_logging(self, _=None):
        """Disable all log forwarding."""
        # This is currently necessary because, after a relation broken, the charm can still see
        # the Loki endpoints in the relation data.
        for container in self._charm.unit.containers.values():
            _PebbleLogClient.disable_inactive_endpoints(
                container=container, active_endpoints={}, topology=self._topology
            )


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="ca_cert_path",
    extra_types=[
        TempoClusterRequirer,
        ManualLogForwarder,
    ],
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    _name = "tempo"
    _instance_addr = "127.0.0.1"

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container(self._name)

        self.tempo = Tempo(self._container, self.tempo_role, self.model)

        # TODO take ports from tempo instead of using hardcoded ports set
        self.unit.set_ports(3200, 4317, 4318, 9411, 14268, 7946, 9096)

        self.topology = JujuTopology.from_charm(self)
        self.tempo_cluster = TempoClusterRequirer(self)
        self.log_forwarder = ManualLogForwarder(
            charm=self,
            loki_endpoints=self.tempo_cluster.get_loki_endpoints(),
            refresh_events=[
                self.on["tempo-cluster"].relation_joined,
                self.on["tempo-cluster"].relation_changed,
            ],
        )

        self.framework.observe(
            self.on.tempo_pebble_ready, self._on_pebble_ready  # pyright: ignore
        )
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        # Tempo Cluster
        self.framework.observe(
            self.tempo_cluster.on.config_received, self._on_tempo_config_received
        )
        self.framework.observe(self.tempo_cluster.on.created, self._on_tempo_cluster_created)
        self.framework.observe(
            self.on.tempo_cluster_relation_departed, self.log_forwarder.disable_logging
        )
        self.framework.observe(
            self.on.tempo_cluster_relation_broken, self.log_forwarder.disable_logging
        )

    def _on_tempo_cluster_created(self, _):
        self._update_tempo_cluster()

    def _update_tempo_cluster(self):
        """Share via tempo-cluster all information we need to publish."""
        self.tempo_cluster.publish_unit_address(socket.getfqdn())

        if self.unit.is_leader() and (role := self.tempo_role):
            logger.info(f"publishing role: {role}")
            self.tempo_cluster.publish_app_role(role)

    def _on_tempo_config_received(self, _e: ConfigReceivedEvent):
        self._update_config()

    def _on_upgrade_charm(self, _):
        self._update_tempo_cluster()

    def _on_config_changed(self, _):
        # if the user has changed the roles, we might need to let the coordinator know
        self._update_tempo_cluster()
        self._update_config()

    def _update_config(self):
        """Update the tempo config and restart the workload if necessary."""
        if failed_checks := self.pre_update_checklist():
            failures = "\n\t".join(failed_checks)
            logger.warning(f"cannot update tempo config:\n\t{failures}")
            return False

        # determine if a workload restart is necessary
        restart = any(
            [
                self._update_tls_certificates(),
                self.tempo.update_config(self.tempo_cluster.get_tempo_config()),
                self.tempo.plan(),
            ]
        )

        if restart:
            self.tempo.restart()

    def _on_pebble_ready(self, _):
        self.unit.set_workload_version(self.tempo.running_version() or "")
        self._update_config()

    @property
    def _tempo_role(self) -> TempoRole:
        """Return the role that this Tempo worker should take on."""
        return TempoRole(self.config["role"])

    @property
    def tempo_role(self) -> Optional[TempoRole]:
        """The role of this tempo worker, if a valid one is assigned."""
        try:
            return self._tempo_role
        except ValueError:
            return None

    def _update_tls_certificates(self) -> bool:
        # caller is responsible for guarding for container connectivity
        if self.tempo_cluster.cert_secrets_ready():
            ca_cert, server_cert = self.tempo_cluster.get_ca_and_server_certs()
            self.tempo.update_certs(self.tempo_cluster.get_privkey(), ca_cert, server_cert)
            if ca_cert:
                # update cacert in charm container too, for self-instrumentation
                CA_CERT_PATH.write_text(ca_cert)
                subprocess.run(["update-ca-certificates", "--fresh"])
            return True

        else:
            self.tempo.clear_certs()

            # clear from charm container too
            CA_CERT_PATH.unlink(missing_ok=True)
            subprocess.run(["update-ca-certificates", "--fresh"])
            return True

    def pre_update_checklist(
        self,
    ) -> List[str]:
        """Check if the charm as a whole is ready, and if not, return a list of failed checks."""
        failures = []
        failures.extend(self.tempo.pre_update_checklist())

        if not self.tempo_cluster.get_tempo_config():
            failures.append("tempo_cluster config not ready")

        return failures

    def _on_collect_status(self, e: CollectStatusEvent):
        if not self._container.can_connect():
            e.add_status(WaitingStatus("Waiting for `tempo` container"))
        if not self.model.get_relation("tempo-cluster"):
            e.add_status(
                BlockedStatus("Missing tempo-cluster relation to a tempo-coordinator charm")
            )
        elif not self.tempo_cluster.relation:
            e.add_status(WaitingStatus("Tempo-Cluster relation not ready"))
        if not self.tempo_cluster.get_tempo_config():
            e.add_status(WaitingStatus("Waiting for coordinator to publish a tempo config"))

        if role := self.tempo_role:
            e.add_status(ActiveStatus(f"{role.value} ready."))
        else:
            logger.error(
                f"`role` config value {self.config.get('role')!r} invalid: should "
                f"be one of {[r.value for r in TempoRole]}."
            )
            e.add_status(
                BlockedStatus(f"Invalid `role` config value: {self.config.get('role')!r}.")
            )

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        return self.tempo_cluster.get_tracing_endpoint("otlp_http")

    @property
    def ca_cert_path(self) -> Optional[str]:
        """CA certificate path for tls tracing."""
        return str(CA_CERT_PATH) if CA_CERT_PATH.exists() else None


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)
