#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

https://discourse.charmhub.io/t/4208
"""

import json
import logging
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from charms.loki_k8s.v1.loki_push_api import _PebbleLogClient
from charms.tempo_k8s.v1.charm_tracing import trace_charm
from charms.tempo_k8s.v1.tracing import TracingEndpointRequirer
from cosl import JujuTopology
from tempo_cluster import (
    TEMPO_CERT_FILE,
    TEMPO_CLIENT_CA_FILE,
    TEMPO_CONFIG_FILE,
    TEMPO_KEY_FILE,
    ConfigReceivedEvent,
    TempoClusterRequirer,
    TempoRole,
)
from ops import pebble
from ops.charm import CharmBase, CollectStatusEvent
from ops.framework import BoundEvent, Object
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import PathError, ProtocolError

TEMPO_DIR = "/tempo"

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


@trace_charm(
    tracing_endpoint="tempo_endpoint",
    server_cert="server_cert_path",
    extra_types=[
        TempoClusterRequirer,
    ],
    # TODO add certificate file once TLS support is merged
)
class TempoWorkerK8SOperatorCharm(CharmBase):
    """A Juju Charmed Operator for Tempo."""

    _name = "tempo"
    _instance_addr = "127.0.0.1"

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container(self._name)
        self.unit.set_ports(8080)

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
        self.tracing = TracingEndpointRequirer(self)

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
        if self.unit.is_leader() and self._tempo_roles:
            logger.info(f"publishing roles: {self._tempo_roles}")
            self.tempo_cluster.publish_app_roles(self._tempo_roles)

    def _on_tempo_config_received(self, _e: ConfigReceivedEvent):
        self._update_config()

    def _on_upgrade_charm(self, _):
        self._update_tempo_cluster()

    def _on_config_changed(self, _):
        # if the user has changed the roles, we might need to let the coordinator know
        self._update_tempo_cluster()

        # if we have a config, we can start tempo
        if self.tempo_cluster.get_tempo_config():
            # determine if a workload restart is necessary
            self._update_config()

    def _update_config(self):
        """Update the tempo config and restart the workload if necessary."""
        restart = any(
            [
                self._update_tls_certificates(),
                self._update_tempo_config(),
                self._set_pebble_layer(),
            ]
        )

        if restart:
            self.restart()

    def _on_pebble_ready(self, _):
        self.unit.set_workload_version(self._tempo_version or "")
        self._update_config()

    def _set_pebble_layer(self) -> bool:
        """Set Pebble layer.

        Returns: True if Pebble layer was added, otherwise False.
        """
        if not self._container.can_connect():
            return False
        if not self._tempo_roles:
            return False

        current_layer = self._container.get_plan()
        new_layer = self._pebble_layer

        if (
            "services" not in current_layer.to_dict()
            or current_layer.services != new_layer["services"]
        ):
            self._container.add_layer(self._name, new_layer, combine=True)  # pyright: ignore
            return True

        return False

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        targets = ",".join(sorted(self._tempo_roles))

        return {
            "summary": "tempo worker layer",
            "description": "pebble config layer for tempo worker",
            "services": {
                "tempo": {
                    "override": "replace",
                    "summary": "tempo worker daemon",
                    "command": f"/bin/tempo --config.file={TEMPO_CONFIG_FILE} -target {targets} -auth.multitenancy-enabled=false",
                    "startup": "enabled",
                }
            },
        }

    @property
    def _tempo_roles(self) -> List[TempoRole]:
        """Return a set of the roles this Tempo worker should take on."""
        roles: List[TempoRole] = [
            role for role in TempoRole if self.config[f"role-{role.value}"] is True
        ]
        return roles

    @property
    def _tempo_version(self) -> Optional[str]:
        if not self._container.can_connect():
            return None

        version_output, _ = self._container.exec(["/bin/tempo", "-version"]).wait_output()
        # Output looks like this:
        # Tempo, version 2.4.0 (branch: HEAD, revision 32137ee)
        if result := re.search(r"[Vv]ersion:?\s*(\S+)", version_output):
            return result.group(1)
        return None

    def _update_tls_certificates(self) -> bool:
        if not self._container.can_connect():
            return False

        ca_cert_path = Path("/usr/local/share/ca-certificates/ca.crt")

        if cert_secrets := self.tempo_cluster.get_cert_secret_ids():
            cert_secrets = json.loads(cert_secrets)

            private_key_secret = self.model.get_secret(id=cert_secrets["private_key_secret_id"])
            private_key = private_key_secret.get_content().get("private-key")

            ca_server_secret = self.model.get_secret(id=cert_secrets["ca_server_cert_secret_id"])
            ca_cert = ca_server_secret.get_content().get("ca-cert")
            server_cert = ca_server_secret.get_content().get("server-cert")

            # Save the workload certificates
            self._container.push(TEMPO_CERT_FILE, server_cert or "", make_dirs=True)
            self._container.push(TEMPO_KEY_FILE, private_key or "", make_dirs=True)
            self._container.push(TEMPO_CLIENT_CA_FILE, ca_cert or "", make_dirs=True)
            self._container.push(ca_cert_path, ca_cert or "", make_dirs=True)

            self._container.exec(["update-ca-certificates", "--fresh"]).wait()
            subprocess.run(["update-ca-certificates", "--fresh"])

            return True
        else:
            self._container.remove_path(TEMPO_CERT_FILE, recursive=True)
            self._container.remove_path(TEMPO_KEY_FILE, recursive=True)
            self._container.remove_path(TEMPO_CLIENT_CA_FILE, recursive=True)
            self._container.remove_path(ca_cert_path, recursive=True)

            ca_cert_path.unlink(missing_ok=True)
            self._container.exec(["update-ca-certificates", "--fresh"]).wait()
            subprocess.run(["update-ca-certificates", "--fresh"])

            return True

    def _update_tempo_config(self) -> bool:
        """Set Tempo config.

        Returns: True if config has changed, otherwise False.
        Raises: BlockedStatusError exception if PebbleError, ProtocolError, PathError exceptions
            are raised by container.remove_path
        """
        tempo_config = self.tempo_cluster.get_tempo_config()
        if not tempo_config:
            logger.warning("cannot update tempo config: coordinator hasn't published one yet.")
            return False

        if self._running_tempo_config() != tempo_config:
            config_as_yaml = yaml.safe_dump(tempo_config)
            self._container.push(TEMPO_CONFIG_FILE, config_as_yaml, make_dirs=True)
            logger.info("Pushed new Tempo configuration")
            return True

        return False

    def _running_tempo_config(self) -> Optional[dict]:
        """Return the Tempo config as dict, or None if retrieval failed."""
        if not self._container.can_connect():
            logger.debug("Could not connect to Tempo container")
            return None

        try:
            raw_current = self._container.pull(TEMPO_CONFIG_FILE).read()
            return yaml.safe_load(raw_current)
        except (ProtocolError, PathError) as e:
            logger.warning(
                "Could not check the current Tempo configuration due to "
                "a failure in retrieving the file: %s",
                e,
            )
            return None

    def restart(self):
        """Restart the pebble service or start if not already running."""
        if not self._container.exists(TEMPO_CONFIG_FILE):
            logger.error("cannot restart tempo: config file doesn't exist (yet).")

        if not self._tempo_roles:
            logger.debug("cannot restart tempo: no roles have been configured.")
            return

        try:
            if self._container.get_service(self._name).is_running():
                self._container.restart(self._name)
            else:
                self._container.start(self._name)
        except pebble.ChangeError as e:
            logger.error(f"failed to (re)start tempo job: {e}", exc_info=True)
            return

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
        if not self._tempo_roles:
            e.add_status(BlockedStatus("No roles assigned: please configure some roles"))
        e.add_status(ActiveStatus(""))

    @property
    def tempo_endpoint(self) -> Optional[str]:
        """Tempo endpoint for charm tracing."""
        if self.tracing.is_ready():
            return self.tracing.otlp_http_endpoint()
        else:
            return None

    @property
    def server_cert_path(self) -> Optional[str]:
        """Server certificate path for tls tracing."""
        return TEMPO_CERT_FILE


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


if __name__ == "__main__":  # pragma: nocover
    main(TempoWorkerK8SOperatorCharm)
