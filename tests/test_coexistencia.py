"""
Convivencia con el ecosistema (seccion 3.1).

Los criterios 25 a 29 se validan sin necesidad de tener ninguna hermana instalada:
son los que hacen la convivencia cierta *por construccion*. Los criterios 30 a 32
requieren las hermanas y se saltan con mensaje explicito si no estan.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.coexistence

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "postgres_local_client"
FUNCIONES_DIR = REPO_ROOT.parent
HERMANAS = ("redshift_extractor", "mongo_extractor", "netsuite_extractor", "postgres_local_extractor")


# -----------------------------------------------------------------------------
# Criterio 25
# -----------------------------------------------------------------------------
def test_no_se_usa_load_dotenv_en_ninguna_parte():
    """
    Criterio 25: `grep -r "load_dotenv" postgres_local_client/` devuelve cero.

    Es el punto no negociable: con load_dotenv() la primera libreria en cargar gana
    y la segunda se queda en silencio con los valores de la otra.
    """
    ofensores = []
    for archivo in PACKAGE_DIR.rglob("*.py"):
        contenido = archivo.read_text(encoding="utf-8")
        if "load_dotenv" in contenido:
            ofensores.append(archivo.relative_to(REPO_ROOT))
    assert not ofensores, f"load_dotenv aparece en: {ofensores}"

    # Y tampoco en el codigo, por si algun dia aparece en un comentario legitimo.
    en_codigo = [
        archivo.relative_to(REPO_ROOT)
        for archivo in PACKAGE_DIR.rglob("*.py")
        if "load_dotenv" in _codigo_sin_comentarios(archivo)
    ]
    assert not en_codigo, f"load_dotenv se usa en: {en_codigo}"


def test_se_usa_dotenv_values():
    """La contraparte: se lee con dotenv_values, que devuelve un dict."""
    contenido = (PACKAGE_DIR / "config.py").read_text(encoding="utf-8")
    assert "dotenv_values" in contenido


# -----------------------------------------------------------------------------
# Criterio 26
# -----------------------------------------------------------------------------
def test_importar_no_toca_os_environ():
    """
    Criterio 26: importar la libreria no agrega ni modifica ninguna clave de os.environ.

    Se hace en un subproceso limpio para que el import sea realmente el primero.
    """
    script = (
        "import os, json\n"
        "antes = dict(os.environ)\n"
        "import postgres_local_client\n"
        "despues = dict(os.environ)\n"
        "agregadas = sorted(set(despues) - set(antes))\n"
        "cambiadas = sorted(k for k in antes if antes[k] != despues.get(k))\n"
        "quitadas = sorted(set(antes) - set(despues))\n"
        "print(json.dumps({'agregadas': agregadas, 'cambiadas': cambiadas, 'quitadas': quitadas}))\n"
    )
    salida = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180, check=True
    )
    import json

    resultado = json.loads(salida.stdout.strip().splitlines()[-1])
    assert resultado == {"agregadas": [], "cambiadas": [], "quitadas": []}, resultado


def test_primera_operacion_no_toca_os_environ(write_env, minimal_env):
    """Criterio 26: tampoco la primera operacion (no solo el import)."""
    from postgres_local_client import list_databases

    write_env(minimal_env)
    antes = dict(os.environ)
    list_databases()
    despues = dict(os.environ)

    assert set(despues) == set(antes), (
        f"cambio el conjunto de variables: "
        f"{set(despues) ^ set(antes)}"
    )
    assert all(antes[key] == despues[key] for key in antes), "se modifico el valor de una variable"


# -----------------------------------------------------------------------------
# Criterio 27
# -----------------------------------------------------------------------------
def test_una_hermana_que_contamina_environ_no_nos_afecta(write_env, minimal_env, tmp_path):
    """
    Criterio 27: reproduce el escenario real de convivencia.

    Se simula lo que hace una hermana: llamar `load_dotenv()` sobre su propio env, que
    define SSH_HOST/SSH_PORT/SSH_USER planos. Nuestra config debe seguir usando la suya.
    """
    from dotenv import load_dotenv

    from postgres_local_client import config as config_mod

    env_hermana = tmp_path / ".env.hermana"
    env_hermana.write_text(
        "SSH_HOST=bastion-de-la-hermana.example\n"
        "SSH_PORT=2222\n"
        "SSH_USER=ec2-user\n"
        "SSH_PKEY_PATH=/home/ec2-user/llave-ajena.pem\n"
        "LOG_LEVEL=DEBUG\n"
        "OUTPUT_DIR=/salida/de/la/hermana\n",
        encoding="utf-8",
    )
    write_env(minimal_env)

    entorno_original = dict(os.environ)
    try:
        # Esto es exactamente lo que hacen hoy las hermanas.
        load_dotenv(dotenv_path=env_hermana, override=False)
        assert os.environ["SSH_HOST"] == "bastion-de-la-hermana.example"

        app, ssh, _pg = config_mod.load_config()
        assert ssh.host == "ssh.example.test", "nos comimos el bastion de la hermana"
        assert ssh.port == 22
        assert ssh.user == "tester"
        assert app.log_level == "INFO"
        assert app.output_dir == "./output"
    finally:
        os.environ.clear()
        os.environ.update(entorno_original)


# -----------------------------------------------------------------------------
# Criterio 28
# -----------------------------------------------------------------------------
def test_importar_no_altera_el_root_logger():
    """Criterio 28: handlers y level del root logger identicos antes y despues."""
    script = (
        "import logging, json\n"
        "root = logging.getLogger()\n"
        "antes = (len(root.handlers), root.level, [type(h).__name__ for h in root.handlers])\n"
        "import postgres_local_client\n"
        "despues = (len(root.handlers), root.level, [type(h).__name__ for h in root.handlers])\n"
        "print(json.dumps({'antes': antes, 'despues': despues}))\n"
    )
    salida = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180, check=True
    )
    import json

    resultado = json.loads(salida.stdout.strip().splitlines()[-1])
    assert resultado["antes"] == resultado["despues"], resultado


def test_no_se_llama_basic_config_ni_se_toca_el_root():
    contenido = "\n".join(
        archivo.read_text(encoding="utf-8") for archivo in PACKAGE_DIR.rglob("*.py")
    )
    assert "basicConfig" not in contenido
    assert "getLogger()" not in contenido, "getLogger() sin nombre es el root logger"
    assert "filterwarnings" not in contenido


def test_el_logger_propio_tiene_nullhandler():
    from postgres_local_client.logging import LOGGER_NAME, get_logger

    logger = get_logger()
    assert logger.name == LOGGER_NAME
    assert any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)


@pytest.mark.sshserver
def test_abrir_un_tunel_no_deja_estado_global_de_logging(tunnel_env):
    """
    Seccion 3.1: sshtunnel muta estado global de logging y hay que revertirlo.

    `sshtunnel.create_logger()` hace `logging.captureWarnings(True)`, agrega handlers a
    `py.warnings` y asigna handlers al logger global `paramiko.transport`. Nada de eso
    puede quedar despues de que esta libreria abra un tunel.

    Se ejercita con una apertura exitosa, que es la que pasa por todo el codigo de
    sshtunnel (constructor, start y los forward servers).
    """
    import warnings

    from postgres_local_client.tunnel import close_all_tunnels, open_tunnel

    paramiko_logger = logging.getLogger("paramiko.transport")
    pywarnings_logger = logging.getLogger("py.warnings")
    root = logging.getLogger()

    antes = (
        list(paramiko_logger.handlers),
        paramiko_logger.level,
        paramiko_logger.propagate,
        list(pywarnings_logger.handlers),
        warnings.showwarning,
        list(root.handlers),
        root.level,
    )

    tunnel_env()
    info = open_tunnel()
    assert info.local_port > 0
    close_all_tunnels()

    despues = (
        list(paramiko_logger.handlers),
        paramiko_logger.level,
        paramiko_logger.propagate,
        list(pywarnings_logger.handlers),
        warnings.showwarning,
        list(root.handlers),
        root.level,
    )
    assert antes == despues, "sshtunnel dejo estado global de logging modificado"


# -----------------------------------------------------------------------------
# Criterio 29
# -----------------------------------------------------------------------------
def test_pyproject_no_declara_techos_sin_justificar():
    """
    Criterio 29: ningun techo de version sin un comentario que lo justifique.

    Verificable de forma automatica: se recorre el bloque de dependencias y por cada
    linea con techo (`<`, `<=`, `~=`, `==`) se exige un comentario inmediatamente
    arriba.
    """
    lineas = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()

    techo = re.compile(r"(<=?|~=|==)\s*\d")
    dentro = False
    sin_justificar = []

    for indice, linea in enumerate(lineas):
        limpia = linea.strip()
        if limpia.startswith("dependencies") or limpia.startswith("parquet =") or limpia.startswith("dev ="):
            dentro = True
        elif dentro and limpia == "]":
            dentro = False
        if not dentro or not limpia.startswith('"'):
            continue
        if not techo.search(limpia):
            continue
        # Se acepta si hay comentario justo arriba (una o varias lineas de #).
        justificado = False
        for previa in reversed(lineas[:indice]):
            if previa.strip().startswith("#"):
                justificado = True
                break
            if previa.strip():
                break
        if not justificado:
            sin_justificar.append(limpia)

    assert not sin_justificar, f"techos sin comentario que los justifique: {sin_justificar}"


def test_el_unico_techo_es_paramiko_y_esta_documentado():
    contenido = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lineas = contenido.split("dependencies = [")[1].splitlines()
    # Cortar en la linea que es exactamente "]": no se puede partir por el primer "]"
    # porque "psycopg[binary]" trae uno.
    bloque = []
    for linea in lineas:
        if linea.strip() == "]":
            break
        bloque.append(linea)

    con_techo = [
        linea.strip()
        for linea in bloque
        if linea.strip().startswith('"') and re.search(r"(<=?|~=|==)\s*\d", linea)
    ]
    assert len(con_techo) == 1, f"se esperaba un solo techo, hay: {con_techo}"
    assert "paramiko" in con_techo[0]
    assert "DSSKey" in contenido, "el techo debe explicar la incompatibilidad concreta"


# -----------------------------------------------------------------------------
# Criterios 30 a 32: requieren las hermanas
# -----------------------------------------------------------------------------
def _hermanas_disponibles() -> list[str]:
    return [nombre for nombre in HERMANAS if (FUNCIONES_DIR / nombre / "pyproject.toml").exists()]


def _codigo_sin_comentarios(path: Path) -> str:
    """
    Devuelve solo el codigo: sin comentarios ni cadenas (docstrings incluidos).

    Hace falta porque los comentarios de la migracion mencionan `load_dotenv()` para
    explicar por que ya no se usa, y un grep literal daria un falso positivo.
    """
    import io
    import tokenize

    piezas: list[str] = []
    with io.open(path, encoding="utf-8") as handle:
        try:
            for token in tokenize.generate_tokens(handle.readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                piezas.append(token.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return path.read_text(encoding="utf-8")
    return " ".join(piezas)


def test_ninguna_libreria_del_ecosistema_usa_load_dotenv():
    """
    Regresion para todo el ecosistema, no solo para esta libreria.

    El bug de os.environ solo aparece en el proyecto host que instala dos, nunca en el
    venv de desarrollo de cada una. Este test lo caza en el repo de al lado.
    """
    disponibles = _hermanas_disponibles()
    if not disponibles:
        pytest.skip(
            "Ninguna hermana disponible en esta maquina: no se pudo verificar. "
            "Los criterios 25 a 29 cubren la convivencia por construccion."
        )

    ofensores = {}
    for hermana in disponibles:
        raiz = FUNCIONES_DIR / hermana / "src"
        if not raiz.exists():
            continue
        for archivo in raiz.rglob("*.py"):
            if "load_dotenv" in _codigo_sin_comentarios(archivo):
                ofensores.setdefault(hermana, []).append(archivo.name)

    assert not ofensores, (
        "estas librerias siguen contaminando os.environ con load_dotenv(): "
        f"{ofensores}. Ver docs/compatibilidad.md."
    )


def test_convivencia_real_en_un_mismo_proceso(write_env, minimal_env):
    """
    Criterio 31: ejercicio cruzado con una hermana en el mismo proceso.

    Necesita la hermana instalada en ESTE venv (no solo presente en disco). Si no
    esta, se salta con mensaje explicito: nunca pasa en falso.
    """
    try:
        importlib.import_module("redshift_extractor")
    except ImportError:
        pytest.skip(
            "redshift_extractor no esta instalado en este venv (el modelo de "
            "distribucion es un venv por proyecto). Criterio 31 no verificado; ver "
            "docs/compatibilidad.md."
        )

    from postgres_local_client import config as config_mod

    write_env(minimal_env)
    _app, ssh, _pg = config_mod.load_config()
    assert ssh.host == "ssh.example.test"


def test_resolucion_conjunta_con_pip(tmp_path):
    """
    Criterio 30: `pip install` por pares resuelve sin ResolutionImpossible.

    Pega a la red y tarda minutos, asi que solo corre con PGC_RUN_PIP_AUDIT=1.
    La evidencia de la corrida manual queda en docs/compatibilidad.md.
    """
    if os.environ.get("PGC_RUN_PIP_AUDIT") != "1":
        pytest.skip(
            "Auditoria de pip omitida (pega a la red). Corre con PGC_RUN_PIP_AUDIT=1 o "
            "consulta docs/compatibilidad.md."
        )

    disponibles = _hermanas_disponibles()
    if not disponibles:
        pytest.skip("Ninguna hermana disponible para auditar.")

    fallos = {}
    for hermana in disponibles:
        proceso = subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
                "--quiet", "--report", str(tmp_path / f"{hermana}.json"),
                str(REPO_ROOT), str(FUNCIONES_DIR / hermana),
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proceso.returncode != 0:
            fallos[hermana] = proceso.stderr[-600:]

    assert not fallos, f"la resolucion conjunta fallo: {fallos}"
