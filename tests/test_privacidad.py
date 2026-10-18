"""
Escaneo de privacidad: revisa que nada de lo que se sube a GitHub tenga secretos ni datos que identifiquen a
cuentas o personas. Reemplaza la revisión manual antes de cada `git push`.

Qué se considera "publicable": lo que subiría `git add .` (archivos versionados + nuevos no ignorados por .gitignore).
Si Git no está disponible o la carpeta todavía no es un repositorio, se calcula leyendo .gitignore.

Las pruebas que necesitan datos locales (.env, logs, CSV con usernames) se omiten si esos archivos no existen, por
ejemplo en GitHub Actions. Los mensajes de error nunca muestran el secreto encontrado, solo dónde está.

Ejecutar desde la raíz del proyecto:  python -m pytest -q tests/test_privacidad.py
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]

# Archivos y carpetas que nunca deben publicarse
SENSIBLES = [".env", "data/posts_features.csv", "data/cuentas_resumen.csv", "data/predicciones_validacion.csv",
             "data/barberias_santiago.csv", "logs/extraccion.log", "salida_prueba/x/posts.jsonl",
             "powerbi/datos/posts.csv", "powerbi/dashboard.pbix", "reportes/calidad/validacion.md"]

BINARIOS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".joblib", ".pkl", ".pbix", ".xlsx", ".zip"}
IMAGENES = {".png", ".jpg", ".jpeg", ".gif", ".ico"}

# Patrones de secretos (sin depender de datos locales)
PATRONES_SECRETOS = {
    "token de acceso de Meta": re.compile(r"EAA[A-Za-z0-9]{40,}"),
    "ACCESS_TOKEN con valor": re.compile(r"ACCESS_TOKEN\s*=\s*['\"]?[A-Za-z0-9]{40,}"),
    "APP_SECRET con valor": re.compile(r"APP_SECRET\s*=\s*['\"]?[0-9a-f]{32}", re.I),
    "contraseña en cadena de conexión": re.compile(r"\b(?:PWD|Password)\s*=\s*[^;\s'\"{}]{3,}", re.I),
}
# Palabras genéricas: un nombre de barbería formado solo por ellas ("La Barbería") no identifica a nadie
GENERICAS = {"la", "el", "los", "las", "de", "del", "y", "the", "and", "barberia", "barbería", "barber", "barbers",
             "barbershop", "shop", "peluqueria", "peluquería", "studio", "estudio", "salon", "salón", "for", "men",
             "hombres", "para", "club", "spa", "cl", "chile", "santiago"}


# --------------------------------------------------------------------------------------------
# Qué archivos se publicarían
# --------------------------------------------------------------------------------------------

class ReglasGitignore:
    """Intérprete mínimo de .gitignore: carpetas (`logs/`), rutas (`data/x.csv`) y comodines (`*.pbix`)."""

    def __init__(self, ruta: Path):
        lineas = ruta.read_text(encoding="utf-8").splitlines() if ruta.exists() else []
        self.patrones = [l.strip() for l in lineas if l.strip() and not l.strip().startswith("#")]

    def ignorado(self, relativa: str) -> bool:
        partes = relativa.split("/")
        for patron in self.patrones:
            if patron.endswith("/"):
                carpeta = patron.rstrip("/")
                if "/" in carpeta:
                    if relativa.startswith(carpeta + "/"):
                        return True
                elif carpeta in partes[:-1]:
                    return True
            elif "/" in patron:
                if fnmatch.fnmatch(relativa, patron.lstrip("/")):
                    return True
            elif any(fnmatch.fnmatch(p, patron) for p in partes):
                return True
        return False


@lru_cache(maxsize=1)
def archivos_publicables() -> tuple[Path, ...]:
    try:
        salida = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                cwd=RAIZ, capture_output=True, text=True, check=True, timeout=30)
        dentro = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=RAIZ, capture_output=True, text=True,
                                check=True, timeout=30).stdout.strip()
        if Path(dentro).resolve() == RAIZ:
            rutas = [RAIZ / p for p in salida.stdout.split("\0") if p]
            return tuple(sorted(p for p in rutas if p.is_file()))
    except (OSError, subprocess.SubprocessError):
        pass
    reglas = ReglasGitignore(RAIZ / ".gitignore")
    encontrados = []
    for carpeta, subcarpetas, archivos in os.walk(RAIZ):
        relativa_carpeta = Path(carpeta).relative_to(RAIZ).as_posix()
        subcarpetas[:] = [d for d in subcarpetas if d != ".git"
                          and not reglas.ignorado(f"{relativa_carpeta}/{d}/x".lstrip("./"))]
        for nombre in archivos:
            relativa = (Path(carpeta) / nombre).relative_to(RAIZ).as_posix()
            if not reglas.ignorado(relativa):
                encontrados.append(RAIZ / relativa)
    return tuple(sorted(encontrados))


def texto_de(ruta: Path) -> str | None:
    """Texto que se publica de un archivo. En notebooks: código, markdown y salidas de texto (no las imágenes)."""
    if ruta.suffix.lower() in BINARIOS:
        return None
    if ruta.suffix == ".ipynb":
        nb = json.loads(ruta.read_text(encoding="utf-8"))
        partes = []
        for celda in nb.get("cells", []):
            partes.append("".join(celda.get("source", "")))
            for salida in celda.get("outputs", []):
                partes.append("".join(salida.get("text", "")))
                for tipo, valor in salida.get("data", {}).items():
                    if tipo.startswith("text/"):
                        partes.append(valor if isinstance(valor, str) else "".join(valor))
        return "\n".join(partes)
    return ruta.read_text(encoding="utf-8", errors="replace")


def relativa(ruta: Path) -> str:
    return ruta.relative_to(RAIZ).as_posix()


def oculto(valor: str) -> str:
    return valor[:4] + "…" if len(valor) > 4 else "…"


# --------------------------------------------------------------------------------------------
# Datos locales que no deben aparecer en lo publicado
# --------------------------------------------------------------------------------------------

def credenciales_locales() -> dict[str, str]:
    ruta = RAIZ / ".env"
    if not ruta.exists():
        return {}
    valores = {}
    for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in linea and not linea.lstrip().startswith("#"):
            clave, valor = linea.split("=", 1)
            valor = valor.strip().strip('"').strip("'")
            if clave.strip() in {"ACCESS_TOKEN", "IG_USER_ID", "APP_SECRET"} and len(valor) >= 8 \
                    and not valor.startswith(("pega_aqui", "tu_")):
                valores[clave.strip()] = valor
    return valores


def cuenta_personal() -> set[str]:
    """Tu usuario de Instagram, tal como aparece en los logs de extracción ("Conectado como @...")."""
    usuarios = set()
    for log in (RAIZ / "logs").glob("*.log") if (RAIZ / "logs").exists() else []:
        usuarios |= set(re.findall(r"Conectado como @([\w.]+)", log.read_text(encoding="utf-8", errors="replace")))
    return usuarios


def identificadores_reales() -> dict[str, set[str]]:
    """Usernames, nombres y direcciones reales sacados de los archivos locales (ignorados por Git)."""
    usernames, nombres, direcciones = set(), set(), set()
    for archivo in ("posts_features.csv", "cuentas_resumen.csv", "barberias_santiago.csv", "predicciones_validacion.csv"):
        ruta = RAIZ / "data" / archivo
        if not ruta.exists():
            continue
        columnas = pd.read_csv(ruta, encoding="utf-8-sig", nrows=0).columns
        df = pd.read_csv(ruta, encoding="utf-8-sig", usecols=[c for c in ("username", "nombre", "direccion") if c in columnas],
                         dtype=str)
        if "username" in df:
            usernames |= {u.strip().lstrip("@").lower() for u in df["username"].dropna() if len(u.strip()) >= 4}
        if "nombre" in df:
            for n in df["nombre"].dropna():
                palabras = re.findall(r"\w+", n.lower())
                if len(n.strip()) >= 6 and not set(palabras) <= GENERICAS:
                    nombres.add(n.strip().lower())
        if "direccion" in df:
            direcciones |= {d.strip().lower() for d in df["direccion"].dropna() if len(d.strip()) >= 8}
    usernames |= {u.lower() for u in cuenta_personal()}
    return {"username": usernames, "nombre": nombres, "dirección": direcciones}


def buscar(texto: str, terminos: set[str]) -> set[str]:
    if not terminos:
        return set()
    patron = re.compile(r"(?<![\w.@])@?(" + "|".join(re.escape(t) for t in sorted(terminos, key=len, reverse=True))
                        + r")(?!\w)", re.I)
    return {m.group(1).lower() for m in patron.finditer(texto)}


# --------------------------------------------------------------------------------------------
# Pruebas
# --------------------------------------------------------------------------------------------

def test_gitignore_cubre_los_archivos_sensibles():
    reglas = ReglasGitignore(RAIZ / ".gitignore")
    no_cubiertos = [r for r in SENSIBLES if not reglas.ignorado(r)]
    assert not no_cubiertos, f".gitignore no ignora: {no_cubiertos}"


def test_no_se_publica_ningun_archivo_sensible():
    reglas = ReglasGitignore(RAIZ / ".gitignore")
    sensibles = {relativa(p) for p in archivos_publicables()} & set(SENSIBLES)
    carpetas = [relativa(p) for p in archivos_publicables()
                if relativa(p).split("/")[0] in {"logs", "salida_prueba", "reportes", ".venv"}
                or relativa(p).startswith("powerbi/datos/") or p.suffix == ".pbix" or p.name == ".env"]
    assert not sensibles and not carpetas, f"Se publicarían: {sorted(sensibles | set(carpetas))}"
    assert reglas.ignorado(".env")


def test_sin_secretos_en_lo_publicable():
    hallazgos = []
    for ruta in archivos_publicables():
        texto = texto_de(ruta)
        if texto is None:
            continue
        for nombre, patron in PATRONES_SECRETOS.items():
            for m in patron.finditer(texto):
                linea = texto.count("\n", 0, m.start()) + 1
                hallazgos.append(f"{relativa(ruta)}:{linea} {nombre} ({oculto(m.group(0))})")
    assert not hallazgos, "Posibles secretos:\n" + "\n".join(hallazgos)


def test_tus_credenciales_no_aparecen_en_lo_publicable():
    credenciales = credenciales_locales()
    if not credenciales:
        pytest.skip("no hay .env con credenciales (normal en GitHub Actions)")
    hallazgos = []
    for ruta in archivos_publicables():
        contenido = ruta.read_bytes()
        for clave, valor in credenciales.items():
            if valor.encode() in contenido:
                hallazgos.append(f"{relativa(ruta)}: contiene tu {clave}")
    assert not hallazgos, "\n".join(hallazgos)


def test_no_aparecen_cuentas_ni_barberias_reales():
    reales = identificadores_reales()
    if not any(reales.values()):
        pytest.skip("no hay datos locales con usernames (normal en GitHub Actions)")
    hallazgos = []
    for ruta in archivos_publicables():
        if ruta.suffix.lower() in IMAGENES:
            continue
        if ruta.suffix.lower() in BINARIOS:
            contenido = ruta.read_bytes().lower()
            encontrados = {u for u in reales["username"] if u.encode() in contenido}
            if encontrados:
                hallazgos.append(f"{relativa(ruta)}: {len(encontrados)} usernames (archivo binario)")
            continue
        texto = texto_de(ruta)
        for tipo, terminos in reales.items():
            encontrados = buscar(texto, terminos)
            if encontrados:
                hallazgos.append(f"{relativa(ruta)}: {tipo} {sorted(encontrados)}")
    assert not hallazgos, "Datos reales en archivos publicables:\n" + "\n".join(hallazgos)


def test_notebooks_publicados_estan_anonimizados():
    notebooks = [p for p in archivos_publicables() if p.suffix == ".ipynb"]
    sin_anonimizar = []
    for ruta in notebooks:
        fuente = "\n".join("".join(c.get("source", "")) for c in json.loads(ruta.read_text(encoding="utf-8"))["cells"])
        if "ANONIMIZAR" in fuente and not re.search(r"^ANONIMIZAR\s*=\s*True\b", fuente, re.M):
            sin_anonimizar.append(relativa(ruta))
    assert not sin_anonimizar, f"Notebooks con ANONIMIZAR distinto de True: {sin_anonimizar}"


# ------------------------------------------------------------------ pruebas del propio escáner

def test_el_escaner_detecta_lo_que_debe(tmp_path):
    (tmp_path / ".gitignore").write_text("logs/\npowerbi/datos/\n*.pbix\ndata/x.csv\n.env\n", encoding="utf-8")
    reglas = ReglasGitignore(tmp_path / ".gitignore")
    assert reglas.ignorado("logs/a.log") and reglas.ignorado("powerbi/datos/p.csv") and reglas.ignorado("a/b.pbix")
    assert reglas.ignorado("data/x.csv") and reglas.ignorado(".env")
    assert not reglas.ignorado("data/y.csv") and not reglas.ignorado("powerbi/tema.json")

    token = "EAA" + "x7Kq" * 15
    assert PATRONES_SECRETOS["token de acceso de Meta"].search(f"ACCESS_TOKEN={token}")
    assert not PATRONES_SECRETOS["token de acceso de Meta"].search("EAAtokenSecretoDePrueba123")
    assert buscar("visita @barberia.uno y barberia.unoextra", {"barberia.uno"}) == {"barberia.uno"}
    assert not buscar("la cuenta otra.barberia.uno", {"barberia.uno"})
