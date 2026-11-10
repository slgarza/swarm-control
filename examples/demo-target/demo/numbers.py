def clamp(value: int, lower: int, upper: int) -> int:
    """Baseline: clamp values for already ordered bounds."""
    return min(max(value, lower), upper)
