def calculate_discount(subtotal_minor: int, percent: int) -> int:
    """Integer-only, floor-rounded, capped at subtotal. Deterministic."""
    return min((subtotal_minor * percent) // 100, subtotal_minor)