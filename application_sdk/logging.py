import logging


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    """
    Get a logger with the given name and level.

    :param name: The name of the logger.
    :param level: The level of the logger.
    :return: The logger.

    Usage:
        >>> logger = get_logger(__name__)
        >>> logger.info("Hello, World!")
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logger.level)

    # create formatter
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    return logger
