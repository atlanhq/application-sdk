import os


def get_actual_cpu_count():
    """Get the actual number of CPUs available on the system.
    https://stackoverflow.com/a/55423170/1710342
    """
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:
        return os.cpu_count() or 1

def get_safe_num_threads():
    """Get the number of threads to use for parallel processing.
    """
    return get_actual_cpu_count() * 2 or 2