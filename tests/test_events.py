from __future__ import annotations

import logging

import pytest

from postgres_local_client import events


def test_emit_construye_el_contrato(events_log):
    collected, collect = events_log
    events.emit(collect, level="INFO", event="query_start", message="hola", db="uno", rows=3)
    assert len(collected) == 1
    evento = collected[0]
    assert set(evento) >= {"ts", "level", "event", "message"}
    assert evento["level"] == "INFO"
    assert evento["event"] == "query_start"
    assert evento["message"] == "hola"
    assert evento["db"] == "uno"
    assert evento["rows"] == 3


def test_emit_sin_callback_no_falla():
    events.emit(None, level="INFO", event="query_start", message="sin callback")


def test_callback_roto_no_tumba_la_operacion():
    def explota(_evento):
        raise RuntimeError("el printer del host esta roto")

    # No debe propagar: una operacion de datos no puede morir por un on_event malo.
    events.emit(explota, level="INFO", event="query_start", message="x")


def test_redaccion_de_secretos_registrados(events_log):
    """Criterio 23: red de seguridad para que un secreto no llegue a un evento."""
    collected, collect = events_log
    events.register_secret("pa(ss)+wo|rd$")
    events.emit(
        collect,
        level="ERROR",
        event="error",
        message="fallo con password pa(ss)+wo|rd$ dentro",
        detalle="otra vez pa(ss)+wo|rd$",
    )
    evento = collected[0]
    assert "pa(ss)+wo|rd$" not in evento["message"]
    assert "pa(ss)+wo|rd$" not in evento["detalle"]
    assert "***" in evento["message"]


def test_secretos_muy_cortos_no_se_registran(events_log):
    """Un secreto de 1-3 chars tacharia texto normal; no se registra."""
    collected, collect = events_log
    events.register_secret("abc")
    events.emit(collect, level="INFO", event="query_start", message="abcdefg")
    assert collected[0]["message"] == "abcdefg"


def test_eventos_minimos_del_contrato():
    """La seccion 9 lista los eventos minimos; deben estar todos declarados."""
    esperados = {
        "config_loaded",
        "tunnel_open",
        "tunnel_reused",
        "tunnel_retry",
        "tunnel_close",
        "connect",
        "query_start",
        "query_done",
        "write_start",
        "write_progress",
        "write_done",
        "tx_begin",
        "tx_commit",
        "tx_rollback",
        "file_saved",
        "error",
    }
    assert esperados == set(events.KNOWN_EVENTS)


def test_emit_no_toca_el_root_logger():
    """Criterio 28: emitir eventos no configura logging global."""
    root = logging.getLogger()
    antes_handlers = list(root.handlers)
    antes_level = root.level
    events.emit(None, level="ERROR", event="error", message="algo")
    assert list(root.handlers) == antes_handlers
    assert root.level == antes_level


def test_quiet_logger_restaura_el_estado():
    from postgres_local_client.logging import quiet_logger

    ajeno = logging.getLogger("libreria.ajena.de.prueba")
    ajeno.setLevel(logging.DEBUG)
    ajeno.propagate = True
    handler = logging.NullHandler()
    ajeno.addHandler(handler)

    with quiet_logger("libreria.ajena.de.prueba"):
        assert ajeno.level == logging.ERROR
        assert ajeno.propagate is False

    assert ajeno.level == logging.DEBUG
    assert ajeno.propagate is True
    assert ajeno.handlers == [handler]


@pytest.mark.parametrize("nivel", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_todos_los_niveles(events_log, nivel):
    collected, collect = events_log
    events.emit(collect, level=nivel, event="query_start", message="x")
    assert collected[0]["level"] == nivel
