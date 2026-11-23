"""Shared rules for checkpointed worker rollouts and offline rehearsals."""

MINIMUM_VALIDATED_PERCENT = 80


def rollout_waves(task_count: int, workers: int, ramp_start: int) -> list[int]:
    """Partition the selected tasks into successively larger bounded waves."""
    if type(workers) is not int or not 1 <= workers <= 100:
        raise ValueError("workers must be between 1 and 100")
    if type(ramp_start) is not int or not 1 <= ramp_start < min(workers, task_count):
        raise ValueError("--ramp-start must be at least 1 and below both workers and selected tasks")
    remaining = task_count
    capacity = ramp_start
    waves = []
    while remaining:
        size = min(remaining, capacity)
        waves.append(size)
        remaining -= size
        capacity = min(workers, 2 * capacity)
    return waves


def wave_passes(validated: int, completed: int) -> bool:
    """Decide whether a completed wave has enough validated results to expand."""
    return completed > 0 and 100 * validated >= MINIMUM_VALIDATED_PERCENT * completed
