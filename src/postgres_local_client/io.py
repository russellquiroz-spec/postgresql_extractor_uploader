from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import pandas as pd

from postgres_local_client.events import OnEvent, emit

Format = Literal["csv", "parquet"]

_PARQUET_HINT = (
    "Parquet necesita pyarrow. Instalalo con: pip install \"postgres-local-client[parquet]\""
)


def save_dataframe(
    df: pd.DataFrame,
    output_path: str,
    fmt: Format = "parquet",
    index: bool = False,
) -> str:
    """Guarda un DataFrame en CSV o Parquet y devuelve la ruta absoluta."""
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        df.to_csv(path, index=index, encoding="utf-8")
    elif fmt == "parquet":
        try:
            df.to_parquet(path, index=index)
        except ImportError as exc:
            raise ImportError(f"{_PARQUET_HINT}. Detalle: {exc}") from exc
    else:
        raise ValueError("fmt debe ser 'csv' o 'parquet'")

    return str(path.resolve())


def save_outputs(
    df: pd.DataFrame,
    *,
    save_dir: Optional[str],
    base_name: str,
    save_csv: bool = False,
    save_parquet: bool = False,
    on_event: Optional[OnEvent] = None,
) -> List[str]:
    """
    Persistencia opcional. Devuelve las rutas escritas.

    Si `save_dir` es None o vacio no escribe nada: el DataFrame se devuelve igual.
    """
    if not save_dir or not (save_csv or save_parquet):
        return []

    out_dir = Path(save_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    written: List[str] = []
    for enabled, fmt in ((save_csv, "csv"), (save_parquet, "parquet")):
        if not enabled:
            continue
        path = save_dataframe(df, str(out_dir / f"{base_name}.{fmt}"), fmt=fmt)  # type: ignore[arg-type]
        written.append(path)
        emit(
            on_event,
            level="INFO",
            event="file_saved",
            message=f"{fmt.upper()} guardado.",
            path=path,
            rows=int(len(df)),
            bytes=int(os.path.getsize(path)),
        )
    return written


__all__ = ["save_dataframe", "save_outputs"]
