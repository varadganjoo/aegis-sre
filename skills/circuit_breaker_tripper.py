"""Circuit Breaker Tripper Skill."""
def run(context: dict) -> dict:
    service = context.get("service")
    return {
        "success": True,
        "action": "trip_circuit_breaker",
        "service": service,
        "log": f"Tripped circuit breaker for {service} to prevent downstream cascading failure."
    }
