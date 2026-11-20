r"""Exportación de resultados filtrados del tablero a CSV y XLSX (RF-017).

Módulo independiente: no importa Streamlit, requests ni ``app.*`` (así no carga el ``.env`` que lee
``app/core/config.py`` al importarse) y no lee variables de entorno. La función HTTP se recibe como
parámetro (``get``), de modo que las pruebas la simulan sin red ni base de datos.

Garantías y limitaciones de la lectura paginada (``traer_todo``)
----------------------------------------------------------------
La API pagina con ``ORDER BY <id> DESC LIMIT/OFFSET``; cada página es una solicitud distinta y, dentro
de una misma solicitud, ``count(*)`` y el ``SELECT`` son dos sentencias separadas. Por eso la
exportación **no es una instantánea transaccional**.

Si ``traer_todo`` termina sin error, se garantiza que:

* el número de filas es igual al ``total`` informado, y ese total fue el mismo en todas las lecturas;
* ningún identificador se repite;
* cada página tuvo exactamente el tamaño esperado;
* la página 1 fue idéntica (mismos identificadores, mismo orden) al principio y al final; esta
  verificación de cierre se hace siempre, también con una sola página o sin filas;
* cada fila es copia exacta, en las columnas de la lista blanca, de lo que la API devolvió en el
  momento en que se leyó su página.

Eso detecta las inserciones nuevas (llevan el id más alto y aparecen en la página 1), los cambios del
total y los desplazamientos que producen duplicados. **No** detecta:

* una salida y una entrada al conjunto filtrado que se compensan en medio del conjunto (por ejemplo,
  con ``estado=reconocida``, una alerta pasa a ``asignada`` y otra vieja pasa a ``reconocida``):
  puede omitirse una fila o incluirse otra que ya no pertenece al conjunto;
* inserciones confirmadas tarde con identificadores más bajos;
* cambios de valor sin cambio de pertenencia (el archivo mezcla momentos distintos).

Valores no finitos (CSV y XLSX por igual)
-----------------------------------------
* ``None``, ``NaN`` de float/numpy, ``pd.NA`` y ``pd.NaT`` son marcas de dato faltante de
  Python/pandas: se exportan como celda vacía.
* ``float("inf")``, ``float("-inf")`` y sus equivalentes de numpy se rechazan con
  ``ErrorExportacion``.
* Un ``Decimal`` no finito (``Infinity``, ``-Infinity``, ``NaN``, ``sNaN``) se rechaza. ``Decimal("NaN")``
  no es una marca de faltante (PostgreSQL admite ``'NaN'`` como valor de ``numeric``), así que
  convertirlo en celda vacía ocultaría un dato inválido. Ojo: ``pd.isna`` sí lo trata como nulo; por
  eso no se usa ``pd.isna``.

Retornos de carro en XLSX
-------------------------
XML 1.0 (§2.11) obliga a todo lector a convertir ``\r\n`` y ``\r`` en ``\n``. openpyxl solo escapa
el ``\r`` (como ``&#13;``) cuando escribe con lxml, y el entorno del proyecto no tiene lxml: el
``\r`` se escribe literal y al leerlo llega como ``\n``. Para que el XLSX sea igual con o sin lxml,
``a_xlsx`` convierte ``\r\n`` y ``\r`` en ``\n`` en todos sus textos, **después** de neutralizar
(``"\r=3"`` queda ``"'\n=3"``: el apóstrofo se decide sobre el valor original).
El CSV conserva ``\r``.

Una garantía completa exige una lectura en una sola transacción ``REPEATABLE READ`` del lado de la
API. No hay reintentos automáticos: ante ``PaginacionInconsistente`` se reintenta a mano.
"""
from __future__ import annotations

import io
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

LIMITE_FILAS_POR_DEFECTO = 10_000
TAMANO_PAGINA_MAX = 500                      # igual al le=500 de la API (app/api/main.py: Tamano)
PREFIJOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")
COLUMNAS_EXCLUIDAS = frozenset({"usuario_asignado", "comentario"})
PARAMETROS_RESERVADOS = frozenset({"pagina", "tamano"})
FILTROS_FECHA = frozenset({"desde", "hasta"})
MAX_CARACTERES_CELDA_XLSX = 32_767           # límite de Excel por celda

AVISO_INSTANTANEA = (
    "No es una instantánea transaccional: cada página se leyó en una consulta distinta. Se verificó "
    "que el total, el tamaño de las páginas, la ausencia de duplicados y la primera página no "
    "cambiaran, pero una salida y una entrada al conjunto filtrado que se compensen durante la "
    "lectura pueden no detectarse.")
ADVERTENCIA_FRANJA_NULA = (
    "Se aplicó un filtro desde/hasta: la API lo compara con franja_inicio, así que las alertas con "
    "franja_inicio nulo quedan excluidas de esta exportación. La API no informa cuántas son.")

# Número completo (p. ej. "-12.5", "+3", "-1e3"): no se neutraliza aunque empiece por + o -.
_NUMERO = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class ErrorExportacion(Exception):
    """Base de los errores de exportación: nunca se entrega un resultado parcial."""


class LimiteExcedido(ErrorExportacion):
    """El total informado por la API supera el límite de filas permitido."""


class PaginacionInconsistente(ErrorExportacion):
    """Los datos cambiaron durante la lectura paginada."""


class RespuestaInvalida(ErrorExportacion):
    """La respuesta de la API no tiene la estructura esperada."""


class ErrorConsulta(ErrorExportacion):
    """Falló la función HTTP al pedir una página; conserva la causa en ``__cause__``."""

    def __init__(self, mensaje: str, pagina: int):
        super().__init__(mensaje)
        self.pagina = pagina


@dataclass(frozen=True)
class Conjunto:
    """Describe un conjunto exportable: ruta de la API, identificador, lista blanca y filtros."""
    nombre: str
    ruta: str
    columna_id: str
    columnas: tuple[str, ...]
    filtros: frozenset[str]
    valores_permitidos: Mapping[str, frozenset[str]] = field(default_factory=dict)
    advertencia_filtro_fecha: str | None = None

    def __post_init__(self):
        if len(set(self.columnas)) != len(self.columnas):
            raise ValueError(f"{self.nombre}: columnas repetidas en la lista blanca")
        if self.columna_id not in self.columnas:
            raise ValueError(f"{self.nombre}: la columna de id no está en la lista blanca")
        if COLUMNAS_EXCLUIDAS & set(self.columnas):
            raise ValueError(f"{self.nombre}: la lista blanca incluye columnas excluidas")
        if PARAMETROS_RESERVADOS & self.filtros:
            raise ValueError(f"{self.nombre}: un filtro usa un nombre reservado")


# Valores de los CHECK de ai_frecuencias.alert (migrations/sql/0001_esquema.sql).
ESTADOS_ALERTA = frozenset({"abierta", "reconocida", "asignada", "cerrada", "descartada"})
SEVERIDADES_ALERTA = frozenset({"baja", "media", "alta"})

ALERTAS = Conjunto(
    nombre="alertas",
    ruta="alerts",
    columna_id="alert_id",
    # Todas las columnas de alert salvo usuario_asignado (datos personales) y clave_dedup (interna).
    columnas=("alert_id", "execution_id", "codigo", "severidad", "estado", "idruta", "franja_inicio",
              "horizonte_min", "valor_observado", "valor_esperado", "umbral", "umbral_version",
              "puntuacion_anomalia", "explicacion", "recomendacion", "creada_en", "actualizada_en"),
    filtros=frozenset({"idruta", "estado", "severidad", "codigo", "desde", "hasta"}),
    valores_permitidos={"estado": ESTADOS_ALERTA, "severidad": SEVERIDADES_ALERTA},
    advertencia_filtro_fecha=ADVERTENCIA_FRANJA_NULA,
)

# Predicciones y recomendaciones se agregan después de P1 (trazabilidad de predicciones).
CONJUNTOS: dict[str, Conjunto] = {ALERTAS.nombre: ALERTAS}


@dataclass(frozen=True)
class ResultadoExportacion:
    conjunto: str
    datos: pd.DataFrame
    total: int
    solicitudes: int
    filtros: dict[str, str]
    iniciado_en: datetime
    terminado_en: datetime
    advertencias: tuple[str, ...]


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------------- lectura paginada

def _validar_filtros(conjunto: Conjunto, filtros: Mapping[str, object] | None) -> dict[str, str]:
    enviados: dict[str, str] = {}
    fechas: dict[str, datetime] = {}
    for nombre, valor in (filtros or {}).items():
        if nombre in PARAMETROS_RESERVADOS:
            raise ValueError(f"'{nombre}' es un parámetro reservado de la paginación")
        if nombre not in conjunto.filtros:
            raise ValueError(f"Filtro no admitido para {conjunto.nombre}: {nombre!r}")
        if valor is None:
            continue
        if nombre in FILTROS_FECHA:
            if not isinstance(valor, datetime) or valor.utcoffset() is None:
                raise ValueError(f"'{nombre}' debe ser un datetime con zona horaria")
            fechas[nombre] = valor
            enviados[nombre] = valor.isoformat()
        elif nombre in conjunto.valores_permitidos:
            if not isinstance(valor, str) or valor not in conjunto.valores_permitidos[nombre]:
                permitidos = ", ".join(sorted(conjunto.valores_permitidos[nombre]))
                raise ValueError(f"Valor no permitido para '{nombre}': {valor!r} (válidos: {permitidos})")
            enviados[nombre] = valor
        elif isinstance(valor, bool):
            raise ValueError(f"'{nombre}' no admite valores booleanos")
        elif isinstance(valor, (int, str)):
            enviados[nombre] = str(valor)
        else:
            raise ValueError(f"Tipo no admitido para '{nombre}': {type(valor).__name__}")
    if "desde" in fechas and "hasta" in fechas and fechas["desde"] >= fechas["hasta"]:
        raise ValueError("'desde' debe ser anterior a 'hasta'")
    return enviados


def _es_id_valido(valor: object) -> bool:
    return isinstance(valor, (int, str)) and not isinstance(valor, bool)


def _leer_pagina(get, conjunto: Conjunto, filtros: dict[str, str], pagina: int,
                 tamano: int) -> tuple[list[Mapping], int]:
    params = {**filtros, "pagina": pagina, "tamano": tamano}
    try:
        respuesta = get(conjunto.ruta, params)
    except Exception as e:  # la causa queda en __cause__
        raise ErrorConsulta(f"Falló la consulta de la página {pagina} de {conjunto.nombre}: "
                            f"{type(e).__name__}", pagina) from e
    if not isinstance(respuesta, Mapping):
        raise RespuestaInvalida(f"Página {pagina}: la respuesta no es un objeto JSON")
    datos, paginacion = respuesta.get("datos"), respuesta.get("paginacion")
    if not isinstance(datos, list) or not isinstance(paginacion, Mapping):
        raise RespuestaInvalida(f"Página {pagina}: faltan 'datos' o 'paginacion'")
    total = paginacion.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise RespuestaInvalida(f"Página {pagina}: 'paginacion.total' no es un entero >= 0")
    if paginacion.get("pagina") != pagina or paginacion.get("tamano") != tamano:
        raise RespuestaInvalida(f"Página {pagina}: la paginación devuelta no coincide con la pedida")
    for fila in datos:
        if not isinstance(fila, Mapping):
            raise RespuestaInvalida(f"Página {pagina}: una fila no es un objeto JSON")
        if not _es_id_valido(fila.get(conjunto.columna_id)):
            raise RespuestaInvalida(f"Página {pagina}: una fila no tiene '{conjunto.columna_id}' válido")
    return datos, total


def traer_todo(get: Callable[[str, Mapping[str, object]], Mapping],
               conjunto: Conjunto,
               filtros: Mapping[str, object] | None = None, *,
               tamano: int = TAMANO_PAGINA_MAX,
               limite_filas: int = LIMITE_FILAS_POR_DEFECTO,
               reloj: Callable[[], datetime] = _ahora_utc) -> ResultadoExportacion:
    """Lee todas las páginas de ``conjunto`` con ``filtros`` y devuelve las filas de la lista blanca.

    ``get(ruta, params)`` debe devolver el JSON ya decodificado y lanzar una excepción ante un error
    HTTP. Lanza ``ValueError`` (parámetros, antes de llamar a la API), ``LimiteExcedido``,
    ``PaginacionInconsistente``, ``RespuestaInvalida`` o ``ErrorConsulta``. Sin reintentos.
    """
    if isinstance(tamano, bool) or not isinstance(tamano, int) or not 1 <= tamano <= TAMANO_PAGINA_MAX:
        raise ValueError(f"'tamano' debe ser un entero entre 1 y {TAMANO_PAGINA_MAX}")
    if isinstance(limite_filas, bool) or not isinstance(limite_filas, int) or limite_filas < 1:
        raise ValueError("'limite_filas' debe ser un entero >= 1")
    enviados = _validar_filtros(conjunto, filtros)
    iniciado_en = reloj()

    datos, total = _leer_pagina(get, conjunto, enviados, 1, tamano)
    solicitudes = 1
    if total > limite_filas:
        raise LimiteExcedido(f"La exportación tendría {total} filas y el límite es {limite_filas}; "
                             "acote los filtros")
    paginas = max(1, math.ceil(total / tamano))

    def esperado(p: int) -> int:
        return max(0, min(tamano, total - (p - 1) * tamano))

    if len(datos) != esperado(1):
        raise PaginacionInconsistente(f"La página 1 trajo {len(datos)} filas y se esperaban {esperado(1)}")
    ids_pagina_1 = [f[conjunto.columna_id] for f in datos]
    filas: list[Mapping] = list(datos)
    vistos = set(ids_pagina_1)
    if len(vistos) != len(ids_pagina_1):
        raise PaginacionInconsistente("Identificadores repetidos en la página 1")

    for p in range(2, paginas + 1):
        datos_p, total_p = _leer_pagina(get, conjunto, enviados, p, tamano)
        solicitudes += 1
        if total_p != total:
            raise PaginacionInconsistente(f"El total cambió de {total} a {total_p} en la página {p}")
        if len(datos_p) != esperado(p):
            raise PaginacionInconsistente(f"La página {p} trajo {len(datos_p)} filas y se esperaban "
                                          f"{esperado(p)}")
        for fila in datos_p:
            ident = fila[conjunto.columna_id]
            if ident in vistos:
                raise PaginacionInconsistente(f"Identificador repetido entre páginas (página {p})")
            vistos.add(ident)
        filas.extend(datos_p)

    # Verificación de cierre, también con una sola página (o ninguna fila): la página 1 debe traer
    # el mismo total y los mismos identificadores, en el mismo orden, que en la primera lectura.
    datos_v, total_v = _leer_pagina(get, conjunto, enviados, 1, tamano)
    solicitudes += 1
    if total_v != total or [f[conjunto.columna_id] for f in datos_v] != ids_pagina_1:
        raise PaginacionInconsistente("Los datos cambiaron durante la exportación (la página 1 "
                                      "ya no coincide); vuelva a intentarlo")

    omitidas: set[str] = set()
    registros = []
    for fila in filas:
        faltan = [c for c in conjunto.columnas if c not in fila]
        if faltan:
            raise RespuestaInvalida(f"Faltan columnas esperadas en la respuesta: {', '.join(faltan)}")
        omitidas.update(str(c) for c in fila if c not in conjunto.columnas)
        registros.append([fila[c] for c in conjunto.columnas])
    df = pd.DataFrame(registros, columns=list(conjunto.columnas), dtype=object)

    advertencias = []
    if conjunto.advertencia_filtro_fecha and FILTROS_FECHA & enviados.keys():
        advertencias.append(conjunto.advertencia_filtro_fecha)
    if omitidas:
        advertencias.append("Columnas no exportadas: " + ", ".join(sorted(omitidas)))
    return ResultadoExportacion(conjunto=conjunto.nombre, datos=df, total=total,
                                solicitudes=solicitudes, filtros=enviados, iniciado_en=iniciado_en,
                                terminado_en=reloj(), advertencias=tuple(advertencias))


def metadatos(resultado: ResultadoExportacion) -> dict[str, str]:
    """Metadatos de la hoja 'exportacion' del XLSX, incluido el aviso de no-instantánea."""
    filtros = "; ".join(f"{k}={v}" for k, v in sorted(resultado.filtros.items())) or "(ninguno)"
    meta = {"conjunto": resultado.conjunto, "filtros": filtros, "total_filas": str(resultado.total),
            "solicitudes": str(resultado.solicitudes),
            "iniciado_en": resultado.iniciado_en.isoformat(),
            "terminado_en": resultado.terminado_en.isoformat(), "limitacion": AVISO_INSTANTANEA}
    for i, adv in enumerate(resultado.advertencias, start=1):
        meta[f"advertencia_{i}"] = adv
    return meta


# ------------------------------------------------------------------------------------ serialización

def _es_nulo(valor: object) -> bool:
    if valor is None or valor is pd.NaT or valor is pd.NA:
        return True
    if isinstance(valor, (float, np.floating)):
        return math.isnan(valor)
    return False


def neutralizar_formula(valor: object) -> object:
    """Antepone ``'`` a un texto que empieza por =, +, -, @, tabulador o retorno de carro.

    No toca valores que no son texto (números reales, booleanos, nulos) ni textos que son un número
    completo como ``"-12.5"``.
    """
    if not isinstance(valor, str) or not valor.startswith(PREFIJOS_FORMULA):
        return valor
    if _NUMERO.fullmatch(valor):
        return valor
    return "'" + valor


def _comprobar_finito(valor: object, columna: object, fila: int, ident: object) -> None:
    """Rechaza infinitos (float/numpy) y cualquier Decimal no finito, incluido Decimal('NaN')."""
    if isinstance(valor, (float, np.floating)) and math.isinf(valor):
        raise ErrorExportacion(f"Valor infinito en la columna {str(columna)!r} ({_donde(fila, ident)})")
    if isinstance(valor, Decimal) and not valor.is_finite():
        raise ErrorExportacion(f"Decimal no finito en la columna {str(columna)!r} "
                               f"({_donde(fila, ident)})")


def _a_texto(valor: object) -> object:
    """Fechas a ISO 8601 (conservando el desplazamiento) y fórmulas neutralizadas."""
    if _es_nulo(valor):
        return None
    if isinstance(valor, datetime):  # incluye pd.Timestamp
        if valor.utcoffset() is None:
            raise ValueError("Fecha sin zona horaria: no se puede exportar sin ambigüedad")
        return valor.isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    return neutralizar_formula(valor)


def preparar(df: pd.DataFrame, *, columna_id: str | None = None) -> pd.DataFrame:
    """Copia de ``df`` con fechas como texto ISO 8601 y textos neutralizados. No modifica ``df``.

    Los nulos (None, NaN de float/numpy, pd.NA, pd.NaT) quedan como None; los infinitos y los
    Decimal no finitos lanzan ``ErrorExportacion`` (con columna, fila y, si se da, el id).
    """
    ids = df[columna_id].tolist() if columna_id is not None and columna_id in df.columns else None
    salida = {}
    for col in df.columns:
        serie = df[col]
        if pd.api.types.is_datetime64_any_dtype(serie.dtype) and not isinstance(serie.dtype,
                                                                                 pd.DatetimeTZDtype):
            raise ValueError(f"La columna {col!r} tiene fechas sin zona horaria")
        valores = []
        for i, v in enumerate(serie.tolist(), start=1):
            _comprobar_finito(v, col, i, ids[i - 1] if ids is not None else None)
            valores.append(_a_texto(v))
        salida[neutralizar_formula(str(col))] = pd.Series(valores, index=df.index, dtype=object)
    return pd.DataFrame(salida, index=df.index, columns=list(salida))


def a_csv(df: pd.DataFrame, *, columna_id: str | None = None) -> bytes:
    """CSV UTF-8 con BOM, separador coma, fin de línea CRLF (RFC 4180) y nulos como celda vacía."""
    texto = preparar(df, columna_id=columna_id).to_csv(index=False, lineterminator="\r\n")
    return texto.encode("utf-8-sig")


def normalizar_fin_de_linea(texto: str) -> str:
    r"""``\r\n`` y ``\r`` a ``\n``, como haría cualquier lector XML (XML 1.0 §2.11).

    Se aplica solo al XLSX y siempre después de ``neutralizar_formula``.
    """
    return texto.replace("\r\n", "\n").replace("\r", "\n")


def _valor_xlsx(valor: object, columna: str, fila: int, ident: object) -> object:
    if _es_nulo(valor):
        return None
    if isinstance(valor, np.generic):
        valor = valor.item()
    _comprobar_finito(valor, columna, fila, ident)  # defensa adicional; preparar() ya lo comprobó
    if isinstance(valor, str):
        valor = normalizar_fin_de_linea(valor)
        if ILLEGAL_CHARACTERS_RE.search(valor):
            raise ErrorExportacion(f"La columna {columna!r} ({_donde(fila, ident)}) tiene caracteres "
                                   "de control que XLSX no admite")
        if len(valor) > MAX_CARACTERES_CELDA_XLSX:
            raise ErrorExportacion(f"La columna {columna!r} ({_donde(fila, ident)}) supera "
                                   f"{MAX_CARACTERES_CELDA_XLSX} caracteres")
        return valor
    if isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, Decimal):
        return valor
    raise ErrorExportacion(f"Tipo no admitido en la columna {columna!r} ({_donde(fila, ident)}): "
                           f"{type(valor).__name__}")


def _donde(fila: int, ident: object) -> str:
    return f"fila {fila}" + (f", id {ident}" if ident is not None else "")


def a_xlsx(df: pd.DataFrame, metadatos: Mapping[str, object] | None = None, *,
           columna_id: str | None = None) -> bytes:
    """XLSX con la hoja 'datos' y, si hay metadatos, la hoja 'exportacion'.

    Todas las fechas quedan como texto ISO 8601 y ninguna celda queda como fórmula. Los textos ya
    neutralizados pasan por ``normalizar_fin_de_linea`` (ver «Retornos de carro en XLSX»).
    """
    preparado = preparar(df, columna_id=columna_id)
    libro = Workbook()
    hoja = libro.active
    hoja.title = "datos"
    columnas = [str(c) for c in preparado.columns]
    hoja.append([_valor_xlsx(c, "(encabezado)", 0, None) for c in columnas])
    ids = df[columna_id].tolist() if columna_id is not None and columna_id in df.columns else None
    for i, fila in enumerate(preparado.itertuples(index=False, name=None), start=1):
        ident = ids[i - 1] if ids is not None else None
        hoja.append([_valor_xlsx(v, columnas[j], i, ident) for j, v in enumerate(fila)])
    if metadatos is not None:
        meta = libro.create_sheet("exportacion")
        meta.append(["clave", "valor"])
        for clave, valor in metadatos.items():
            meta.append([_valor_xlsx(_a_texto(str(clave)), "clave", 0, None),
                         _valor_xlsx(_a_texto(str(valor)), "valor", 0, None)])
    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()
