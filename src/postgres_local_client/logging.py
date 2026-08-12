from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

LOGGER_NAME = "postgres_local_client"
_CLI_HANDLER_FLAG = "_pgc_cli_handler"


def get_logger(suffix: Optional[str] = None) -> logging.Logger:
    """
    Devuelve el logger propio de la libreria con un NullHandler.

    Nunca toca el root logger ni la configuracion global de logging: un proyecto host
    que importe esta libreria junto a otra debe encontrar su configuracion de logging
    exactamente como la dejo.
    """
    name = LOGGER_NAME if not suffix else f"{LOGGER_NAME}.{suffix}"
    logger = logging.getLogger(name)
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    return logger


def get_ssh_logger() -> logging.Logger:
    """
    Logger que se le pasa a sshtunnel/paramiko.

    Lleva NullHandler y `propagate=False` por dos razones:
      1. sshtunnel agrega un StreamHandler a consola si el logger que recibe no
         tiene ninguno; con el NullHandler presente se salta ese paso.
      2. paramiko es verboso y puede loggear detalle de la negociacion de
         autenticacion. Con propagate=False eso no llega al root logger del host.
    """
    logger = get_logger("ssh")
    logger.propagate = False
    return logger


def configure_logging(level: Optional[str] = None) -> None:
    """
    Manda el logger propio a consola. Pensado para la CLI, no para uso como libreria.

    Configura unicamente el logger `postgres_local_client`; el root logger queda
    intacto. Llamarlo dos veces no duplica handlers.
    """
    logger = get_logger()
    for handler in list(logger.handlers):
        if getattr(handler, _CLI_HANDLER_FLAG, False):
            logger.removeHandler(handler)

    handler = logging.StreamHandler()
    setattr(handler, _CLI_HANDLER_FLAG, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel((level or "INFO").upper())
    logger.propagate = False


@contextmanager
def quiet_logger(*names: str, level: int = logging.ERROR) -> Iterator[None]:
    """
    Silencia loggers de terceros de forma temporal y restaura su estado exacto.

    Existe para que el warning que sqlglot emite en cada sentencia que no modela no
    aparezca en el stderr del usuario. Tocar un logger ajeno solo es aceptable si no
    queda mutacion neta, asi que se toma snapshot de level, propagate y handlers.
    """
    saved: List[Tuple[logging.Logger, int, bool, List[logging.Handler]]] = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.propagate, list(logger.handlers)))
        logger.setLevel(level)
        logger.propagate = False
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
    try:
        yield
    finally:
        for logger, saved_level, saved_propagate, saved_handlers in saved:
            try:
                logger.setLevel(saved_level)
                logger.propagate = saved_propagate
                logger.handlers = saved_handlers
            except Exception:  # noqa: BLE001 - restaurar logging jamas debe fallar
                pass


__all__ = [
    "LOGGER_NAME",
    "configure_logging",
    "get_logger",
    "get_ssh_logger",
    "quiet_logger",
]
