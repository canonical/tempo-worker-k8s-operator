from pathlib import Path

import jubilant
import pytest
from jubilant import Juju, all_active, all_blocked

from helpers import TEMPO_APP, S3_APP, WORKER_APP
from tests.integration.helpers import WORKER_RESOURCES, deploy_minio_and_s3


@pytest.mark.setup
def test_deploy_worker(juju: Juju, tempo_worker_charm: Path):
    juju.deploy(
        tempo_worker_charm, WORKER_APP, resources=WORKER_RESOURCES, trust=True
    )

    # worker will be blocked because of missing s3 and coordinator integration
    juju.wait(
        lambda status: all_blocked(status, WORKER_APP),
        timeout=1000
    )


def test_all_active_when_coordinator_and_s3_added(juju: Juju):
    deploy_minio_and_s3(juju)
    juju.deploy("tempo-coordinator-k8s", TEMPO_APP, trust=True, base="ubuntu@22.04", channel="latest/edge")
    juju.integrate(TEMPO_APP + ":s3", S3_APP + ":s3-credentials")
    juju.integrate(TEMPO_APP, WORKER_APP)
    juju.wait(
        lambda status: all_active(status, TEMPO_APP, WORKER_APP), timeout=1000
    )
