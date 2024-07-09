"""Tempo workload class."""

import logging
import re
from typing import List, Optional

import ops
import yaml
from ops import pebble
from ops.pebble import PathError, ProtocolError
from tempo_cluster import TempoRole

logger = logging.getLogger(__name__)

TEMPO_CONFIG_FILE = "/etc/tempo/tempo.yaml"
TEMPO_CERT_FILE = "/etc/tempo/tls/server.crt"
TEMPO_KEY_FILE = "/etc/tempo/tls/server.key"
TEMPO_CA_FILE = "/usr/local/share/ca-certificates/ca.crt"


class Tempo:
    """Tempo workload container facade."""

    _name = "tempo"

    def __init__(self, container: ops.Container, role: Optional[TempoRole], model: ops.Model):
        self._container = container
        self._role = role
        self._model = model

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer.

        Caller is responsible for checking whether the tempo role is valid before
        calling this method.
        """
        role = self._role.value  # let it raise # type:ignore
        if role == "all":
            role = "scalable-single-binary"
        return {
            "summary": "tempo worker layer",
            "description": "pebble config layer for tempo worker",
            "services": {
                "tempo": {
                    "override": "replace",
                    "summary": "tempo worker process",
                    "command": f"/bin/tempo -config.file={TEMPO_CONFIG_FILE} -target {role}",
                    "startup": "enabled",
                }
            },
        }

    def update_config(self, new_config: dict):
        """Set Tempo config.

        Returns: True if config has changed, otherwise False.
        Raises: BlockedStatusError exception if PebbleError, ProtocolError, PathError exceptions
            are raised by container.remove_path

        Caller is responsible for guarding for container connectivity.
        """
        if self.running_config() != new_config:
            config_as_yaml = yaml.safe_dump(new_config)
            self._container.push(TEMPO_CONFIG_FILE, config_as_yaml, make_dirs=True)
            logger.info("Pushed new Tempo configuration")
            return True
        return False

    def is_configured(self):
        """Check that the config file is where it should be."""
        container = self._container
        return container.can_connect() and not container.exists(TEMPO_CONFIG_FILE)

    def restart(self):
        """Restart the pebble service or start if not already running."""
        try:
            if self._container.get_service(self._name).is_running():
                self._container.restart(self._name)
            else:
                self._container.start(self._name)
        except pebble.ChangeError as e:
            logger.error(f"failed to (re)start tempo job: {e}", exc_info=True)
            return

    def running_version(self) -> Optional[str]:
        """Get the running version from the tempo process."""
        if not self._container.can_connect():
            return None

        version_output, _ = self._container.exec(["/bin/tempo", "-version"]).wait_output()
        # Output looks like this:
        # Tempo, version 2.4.0 (branch: HEAD, revision 32137ee...)
        if result := re.search(r"[Vv]ersion:?\s*(\S+)", version_output):
            return result.group(1)
        return None

    def running_config(self) -> Optional[dict]:
        """Return the Tempo config as dict, or None if retrieval failed.

        Assumes the container can connect and the config is there.
        """
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

    def pre_update_checklist(self) -> List[str]:
        """Check whether tempo is ready to run.

        Return a list of failed checks (str).
        """
        failures = []

        if not self._container.can_connect():
            failures.append("container cannot connect")

        if self.is_configured():
            failures.append("config file doesn't exist (yet)")

        if not self._role:
            failures.append("role misconfigured")

        return failures

    def plan(self) -> bool:
        """Set Pebble layer.

        Returns: True if Pebble layer was added, otherwise False.
        Caller is responsible for checking whether the preconditions are met.
        """
        current_layer = self._container.get_plan()
        new_layer = self._pebble_layer

        if (
            "services" not in current_layer.to_dict()
            or current_layer.services != new_layer["services"]
        ):
            self._container.add_layer(self._name, new_layer, combine=True)  # pyright: ignore
            return True

        return False

    def update_certs(
        self, private_key: Optional[str], ca_cert: Optional[str], server_cert: Optional[str]
    ):
        """Save all secrets and certificates to the workload container."""
        self._container.push(TEMPO_CERT_FILE, server_cert or "", make_dirs=True)
        self._container.push(TEMPO_KEY_FILE, private_key or "", make_dirs=True)
        self._container.push(TEMPO_CA_FILE, ca_cert or "", make_dirs=True)

        self._container.exec(["update-ca-certificates", "--fresh"]).wait()

    def clear_certs(self):
        """Remove all secrets and certs from the workload container."""
        self._container.remove_path(TEMPO_CERT_FILE, recursive=True)
        self._container.remove_path(TEMPO_KEY_FILE, recursive=True)
        self._container.remove_path(TEMPO_CA_FILE, recursive=True)

        self._container.exec(["update-ca-certificates", "--fresh"]).wait()
