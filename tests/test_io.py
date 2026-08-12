from __future__ import annotations

import pandas as pd
import pytest

from postgres_local_client import io as io_mod


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})


def test_save_dataframe_csv(tmp_path, frame):
    path = io_mod.save_dataframe(frame, str(tmp_path / "salida.csv"), fmt="csv")
    assert path.endswith("salida.csv")
    assert pd.read_csv(path).equals(frame)


def test_save_dataframe_parquet(tmp_path, frame):
    path = io_mod.save_dataframe(frame, str(tmp_path / "salida.parquet"), fmt="parquet")
    assert pd.read_parquet(path).equals(frame)


def test_save_dataframe_formato_invalido(tmp_path, frame):
    with pytest.raises(ValueError, match="csv"):
        io_mod.save_dataframe(frame, str(tmp_path / "x.txt"), fmt="txt")


def test_save_dataframe_crea_directorios(tmp_path, frame):
    destino = tmp_path / "a" / "b" / "c.csv"
    io_mod.save_dataframe(frame, str(destino), fmt="csv")
    assert destino.exists()


def test_save_outputs_genera_ambos_archivos(tmp_path, frame, events_log):
    """Criterio 15: save_csv y save_parquet generan ambos archivos."""
    collected, collect = events_log
    rutas = io_mod.save_outputs(
        frame,
        save_dir=str(tmp_path),
        base_name="mi_extraccion",
        save_csv=True,
        save_parquet=True,
        on_event=collect,
    )
    assert len(rutas) == 2
    assert (tmp_path / "mi_extraccion.csv").exists()
    assert (tmp_path / "mi_extraccion.parquet").exists()

    guardados = [event for event in collected if event["event"] == "file_saved"]
    assert len(guardados) == 2
    assert all(event["rows"] == 3 for event in guardados)
    assert all(event["bytes"] > 0 for event in guardados)


def test_save_outputs_sin_save_dir_no_escribe(tmp_path, frame):
    """Si save_dir es None o vacio, solo se devuelve el DataFrame."""
    assert io_mod.save_outputs(frame, save_dir=None, base_name="x", save_csv=True) == []
    assert io_mod.save_outputs(frame, save_dir="", base_name="x", save_csv=True) == []
    assert list(tmp_path.iterdir()) == []


def test_save_outputs_sin_formatos_no_escribe(tmp_path, frame):
    assert io_mod.save_outputs(frame, save_dir=str(tmp_path), base_name="x") == []
    assert list(tmp_path.iterdir()) == []
