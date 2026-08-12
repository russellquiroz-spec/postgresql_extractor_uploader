from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Mapping

_USER_KEYS = ("user", "username")
_PASSWORD_KEYS = ("password", "pass", "pwd")


def read_windows_env_value_from_registry(env_name: str) -> str | None:
    if os.name != "nt":
        return None

    try:
        import winreg
    except ImportError:
        return None

    key_specs = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for hive, subkey in key_specs:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, env_name)
        except FileNotFoundError:
            continue
        except OSError:
            continue

        rendered = str(value).strip()
        if rendered:
            return rendered
    return None


def read_system_env_value(env_name: str) -> str | None:
    value = os.getenv(env_name)
    if value:
        return value
    return read_windows_env_value_from_registry(env_name)


def normalize_plain_secret(raw: str) -> str:
    normalized = raw.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in ("'", '"'):
        normalized = normalized[1:-1]
    normalized = normalized.replace(r"\"", '"').replace(r"\'", "'")
    return normalized


def resolve_secret_reference_from_keyring_manager(env_name: str) -> tuple[str, str] | None:
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None

    credentials_path = Path(appdata) / "KeyringManager" / "credentials.json"
    if not credentials_path.exists():
        return None

    try:
        entries = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"No se pudo leer el archivo de credenciales de KeyringManager: {credentials_path}"
        ) from exc

    if not isinstance(entries, list):
        raise ValueError(
            f"El archivo de credenciales de KeyringManager no tiene el formato esperado: {credentials_path}"
        )

    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and str(item.get("env_var", "")).strip() == env_name
        ),
        None,
    )
    if not entry:
        return None

    user = str(entry.get("usuario", "")).strip()
    service = str(entry.get("service", "")).strip()
    if not user or not service:
        raise ValueError(
            f"La entrada '{env_name}' en KeyringManager no tiene 'usuario' o 'service' validos."
        )

    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "Se encontro una entrada en KeyringManager pero falta la dependencia 'keyring'. "
            "Instalala en el entorno donde uses postgres_local_client."
        ) from exc

    password = keyring.get_password(service, user)
    if not password:
        raise ValueError(
            f"KeyringManager encontro la entrada '{env_name}', pero no se pudo recuperar el password "
            f"para el servicio '{service}' y usuario '{user}'."
        )

    return user, normalize_plain_secret(str(password))


def _pick_value(data: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        rendered = str(value).strip()
        if rendered:
            return rendered
    return None


def _normalize_secret_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def _find_secret_value(payload: object, target_keys: tuple[str, ...]) -> str | None:
    normalized_targets = {_normalize_secret_key(key) for key in target_keys}

    def walk(node: object) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    continue
                if _normalize_secret_key(key) not in normalized_targets:
                    continue
                rendered = str(value).strip()
                if rendered:
                    return rendered

            for value in node.values():
                found = walk(value)
                if found:
                    return found
            return None

        if isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def _decode_json_payload(raw: str) -> object | None:
    candidates = [raw.strip()]
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        candidates.append(stripped[1:-1].strip())
    if r"\"" in stripped:
        candidates.append(stripped.replace(r"\"", '"'))
    if r"\'" in stripped:
        candidates.append(stripped.replace(r"\'", "'"))

    for candidate in candidates:
        current: object = candidate
        for _ in range(3):
            if not isinstance(current, str):
                return current

            text = current.strip()
            try:
                current = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass

            try:
                current = ast.literal_eval(text)
                continue
            except (SyntaxError, ValueError):
                break

    return None


def parse_credentials_secret(raw: str) -> tuple[str, str] | None:
    payload = _decode_json_payload(raw)
    if isinstance(payload, (dict, list)):
        user = _find_secret_value(payload, _USER_KEYS)
        password = _find_secret_value(payload, _PASSWORD_KEYS)
        if user and password:
            return user, password

    parts = [chunk.strip() for chunk in re.split(r"[;\r\n]+", raw) if chunk.strip()]
    if parts:
        pairs: dict[str, str] = {}
        is_key_value = True
        for part in parts:
            if "=" not in part:
                is_key_value = False
                break
            key, value = part.split("=", 1)
            pairs[key.strip().lower()] = value.strip().strip("\"'")

        if is_key_value:
            user = _pick_value(pairs, _USER_KEYS)
            password = _pick_value(pairs, _PASSWORD_KEYS)
            if user and password:
                return user, password

    for delimiter in ("|", ":"):
        if delimiter not in raw:
            continue
        user, password = raw.split(delimiter, 1)
        user = user.strip()
        password = password.strip()
        if user and password:
            return user, password

    return None


def resolve_secret_reference(env_name: str) -> tuple[str, str]:
    keyring_credentials = resolve_secret_reference_from_keyring_manager(env_name)
    if keyring_credentials:
        return keyring_credentials

    secret = read_system_env_value(env_name)
    if not secret:
        raise ValueError(f"La variable de sistema '{env_name}' no existe o esta vacia.")

    parsed_credentials = parse_credentials_secret(secret)
    if parsed_credentials:
        return parsed_credentials

    raise ValueError(
        f"La variable '{env_name}' no tiene un formato valido. "
        "Usa JSON ({\"user\": \"...\", \"password\": \"...\"}), "
        "pares USER=...;PASSWORD=... o user:password."
    )


__all__ = [
    "normalize_plain_secret",
    "parse_credentials_secret",
    "read_system_env_value",
    "read_windows_env_value_from_registry",
    "resolve_secret_reference",
    "resolve_secret_reference_from_keyring_manager",
]
