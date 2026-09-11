"""Horizontal Pod Autoscaler Skill."""
def run(context: dict) -> dict:
    service = context.get("service")
    current = context.get("replicas", 2)
    target = min(current * 2, 10)
    return {
        "success": True,
        "action": "scale_replicas",
        "service": service,
        "previous_replicas": current,
        "target_replicas": target,
        "log": f"Scaled {service} from {current} to {target} pods under high traffic load."
    }
