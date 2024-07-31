import scenario

from charm import TempoWorkerK8SOperatorCharm


def set_role(state: scenario.State, role: str):
    """Modify a state to activate a tempo role."""
    cfg = {}
    for role_ in TempoWorkerK8SOperatorCharm._valid_roles:
        cfg[f"role-{role_}"] = False
    cfg[f"role-{role}"] = True
    return state.replace(config=cfg)
