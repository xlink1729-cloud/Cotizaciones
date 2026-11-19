from contextlib import contextmanager
from datetime import datetime
import hmac
import io
import time
from zoneinfo import ZoneInfo

from fpdf import FPDF
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
import pandas as pd
import psycopg2
from psycopg2 import errors
from psycopg2.extras import Json, RealDictCursor
import streamlit as st

# ----------------------------------------------------------------------------
# Configuración y constantes
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Cotizador PU · Etapa 2", layout="wide")

TZ = ZoneInfo("America/Mexico_City")
IVA = 0.16
ISR_RESICO = 0.01
ISR_ACT_EMP = 0.20
MAX_FALLOS_LOGIN = 5
BLOQUEO_SEG = 300

TIPOS_INSUMO = ["Material", "Equipo / Herramienta"]
FACTORES_URGENCIA = [1.0, 1.15, 1.25, 1.5, 2.0]
REGIMENES = ["RESICO (1% ISR)", "Actividad Empresarial (~20% s/util)"]

ESQUEMAS = {
    "2 Ingenieros": [
        (
            "Ing. 1",
            300.0,
            "Costo por hora del primer ingeniero (diagnóstico, pruebas y"
            " ejecución).",
        ),
        (
            "Ing. 2",
            300.0,
            "Costo por hora del segundo ingeniero (ejecución, maniobras y"
            " pruebas).",
        ),
    ],
    "Especialista + Ayudante": [
        (
            "Especialista",
            300.0,
            "Costo por hora del especialista (diagnóstico, pruebas y puesta en"
            " marcha).",
        ),
        (
            "Ayudante",
            180.0,
            "Costo por hora del apoyo técnico (maniobras, cableado, soporte en"
            " campo).",
        ),
    ],
    "1 Ingeniero": [
        (
            "Ingeniero",
            300.0,
            "Costo por hora del ingeniero que ejecuta el servicio.",
        ),
    ],
}
NOMBRES_ESQUEMA = list(ESQUEMAS)

COLS_CAT = [
    "id",
    "concepto",
    "tipo_insumo",
    "unidad",
    "costo_sin_iva",
    "cantidad_default",
    "con_factura",
]
COLS_SEL = [
    "Concepto",
    "tipo_insumo",
    "unidad",
    "costo_sin_iva",
    "con_factura",
    "Cantidad",
]

PARTIDA_DEFAULT_1 = (
    "Diagnóstico inicial, pruebas de carga y habilitación provisional de"
    " polipasto/elevador"
)
PARTIDA_DEFAULT_2 = (
    "Montaje definitivo de capacitor / banco capacitivo (75 µF / 150 µF) y"
    " pruebas finales"
)

def _siguiente_uid() -> int:
  ss = st.session_state
  val = int(ss.get("_uid_seq", 0)) + 1
  ss["_uid_seq"] = val
  return val

def _mo_defaults(prefix: str) -> dict:
  d = {}
  for e, nombre in enumerate(NOMBRES_ESQUEMA):
    for i, (_, tarifa, _) in enumerate(ESQUEMAS[nombre]):
      d[f"{prefix}_t_{e}_{i}"] = tarifa
      d[f"{prefix}_h_{e}_{i}"] = 4.0
  return d

def _nueva_partida_estado(
    nombre: str = "Nueva partida",
    unidad: str = "Serv",
    cantidad: float = 1.0,
    esquema: str | None = None,
    factor_urg: float = 1.25,
    pct_herr: int = 3,
    pct_ind: int = 12,
    pct_gar: int = 5,
    pct_uti: int = 25,
    regimen: str | None = None,
    dividir: bool = True,
    pct_ing_1: int = 50,
    insumos_df: pd.DataFrame | None = None,
    tarifas_horas: dict | None = None,
    iniciar_en_cero: bool = False,
) -> dict:
  esq = esquema if esquema in ESQUEMAS else NOMBRES_ESQUEMA[0]
  reg = regimen if regimen in REGIMENES else REGIMENES[0]
  p = {
      "uid": _siguiente_uid(),
      "nombre": nombre,
      "unidad": unidad,
      "cantidad": float(cantidad),
      "esquema": esq,
      "factor_urg": float(factor_urg),
      "pct_herr": int(pct_herr),
      "pct_ind": int(pct_ind),
      "pct_gar": int(pct_gar),
      "pct_uti": int(pct_uti),
      "regimen": reg,
      "dividir": bool(dividir),
      "pct_ing_1": int(pct_ing_1),
      "insumos_df": insumos_df.copy() if insumos_df is not None else None,
      "iniciar_en_cero": bool(iniciar_en_cero),
  }
  p.update(_mo_defaults("mo"))
  if tarifas_horas:
    for k, v in tarifas_horas.items():
      if k in p:
        p[k] = float(v)
  return p

def _defaults() -> dict:
  return {
      "folio": "",
      "cliente": "Cliente Residencial",
      "ubicacion": "Colima",
      "sobrescribir": False,
      "ver": 0,
      "cot_cargada": None,
      "partidas": [
          _nueva_partida_estado(
              PARTIDA_DEFAULT_1, "Serv", 1.0, iniciar_en_cero=False
          ),
          _nueva_partida_estado(
              PARTIDA_DEFAULT_2,
              "Serv",
              1.0,
              factor_urg=1.0,
              iniciar_en_cero=True,
          ),
      ],
      "partida_activa": 0,
  }

# ----------------------------------------------------------------------------
# Seguridad: acceso con contraseña
# ----------------------------------------------------------------------------
@st.cache_resource
def _estado_login() -> dict:
  return {"fallos": 0, "bloqueado_hasta": 0.0}

def verificar_acceso():
  ss = st.session_state
  if ss.get("auth_ok"):
    return

  esperado = st.secrets.get("APP_PASSWORD", "")
  if not esperado:
    st.error(
        "Falta configurar APP_PASSWORD en los secrets. La app queda bloqueada."
    )
    st.stop()

  estado = _estado_login()
  st.title("🔒 Cotizador PU")
  restante = estado["bloqueado_hasta"] - time.time()
  if restante > 0:
    st.error(
        f"Demasiados intentos fallidos. Intenta de nuevo en {int(restante // 60) + 1} min."
    )
    st.stop()

  with st.form("login"):
    pwd = st.text_input("Contraseña", type="password")
    entrar = st.form_submit_button("Entrar")

  if entrar:
    if hmac.compare_digest(
        pwd.encode("utf-8"), str(esperado).encode("utf-8")
    ):
      estado["fallos"] = 0
      ss["auth_ok"] = True
      st.rerun()
    estado["fallos"] += 1
    if estado["fallos"] >= MAX_FALLOS_LOGIN:
      estado["fallos"] = 0
      estado["bloqueado_hasta"] = time.time() + BLOQUEO_SEG
    time.sleep(1)
    st.error("Contraseña incorrecta.")
  st.stop()

verificar_acceso()
st.title("⚡ Cotizador PU · Multi-Partida & APU")
with st.sidebar:
  if st.button("Cerrar sesión"):
    st.session_state["auth_ok"] = False
    st.rerun()

for _k, _v in _defaults().items():
  st.session_state.setdefault(_k, _v)

# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------
@contextmanager
def get_conn():
  conn = psycopg2.connect(
      st.secrets["DB"],
      sslmode="require",
      connect_timeout=10,
      application_name="cotizador_pu",
  )
  try:
    yield conn
    conn.commit()
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.close()

MIGRACION_SQL = """
CREATE TABLE IF NOT EXISTS catalogo_insumos (
    id SERIAL PRIMARY KEY,
    concepto VARCHAR(150) NOT NULL,
    tipo_insumo VARCHAR(30) NOT NULL DEFAULT 'Material',
    unidad VARCHAR(20) NOT NULL DEFAULT 'Pza',
    costo_sin_iva NUMERIC(12, 2) NOT NULL DEFAULT 0,
    cantidad_default NUMERIC(10, 2) NOT NULL DEFAULT 1.00,
    con_factura BOOLEAN NOT NULL DEFAULT TRUE,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE catalogo_insumos
    ADD COLUMN IF NOT EXISTS cantidad_default NUMERIC(10, 2) NOT NULL DEFAULT 1.00;
ALTER TABLE catalogo_insumos
    ADD COLUMN IF NOT EXISTS tipo_insumo VARCHAR(30) NOT NULL DEFAULT 'Material';

CREATE TABLE IF NOT EXISTS cotizaciones (
    folio VARCHAR(30) PRIMARY KEY,
    cliente VARCHAR(150) NOT NULL,
    ubicacion VARCHAR(120) DEFAULT 'Colima',
    descripcion_servicio TEXT,
    factor_urgencia NUMERIC(4, 2) DEFAULT 1.00,
    costo_directo NUMERIC(12, 2) NOT NULL,
    indirectos_monto NUMERIC(12, 2) NOT NULL,
    garantia_monto NUMERIC(12, 2) NOT NULL,
    utilidad_monto NUMERIC(12, 2) NOT NULL,
    subtotal NUMERIC(12, 2) NOT NULL,
    iva_trasladado NUMERIC(12, 2) NOT NULL,
    total_facturado NUMERIC(12, 2) NOT NULL,
    iva_por_pagar NUMERIC(12, 2) NOT NULL,
    isr_estimado NUMERIC(12, 2) NOT NULL,
    ganancia_neta_libre NUMERIC(12, 2) NOT NULL,
    parametros JSONB,
    fecha_creacion TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cotizacion_partidas (
    id SERIAL PRIMARY KEY,
    folio VARCHAR(30) NOT NULL REFERENCES cotizaciones(folio) ON DELETE CASCADE,
    orden INT NOT NULL DEFAULT 1,
    nombre VARCHAR(200) NOT NULL,
    unidad VARCHAR(20) NOT NULL DEFAULT 'Serv',
    cantidad NUMERIC(10, 2) NOT NULL DEFAULT 1.00,
    pu_sin_iva NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    importe NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    costo_directo NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    ganancia_neta_libre NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    parametros JSONB
);

CREATE TABLE IF NOT EXISTS cotizacion_insumos (
    id SERIAL PRIMARY KEY,
    folio VARCHAR(30) REFERENCES cotizaciones(folio) ON DELETE CASCADE,
    partida_id INT REFERENCES cotizacion_partidas(id) ON DELETE CASCADE,
    concepto VARCHAR(150) NOT NULL,
    tipo_insumo VARCHAR(30) NOT NULL DEFAULT 'Material',
    unidad VARCHAR(20) NOT NULL DEFAULT 'Pza',
    cantidad NUMERIC(10, 2) NOT NULL,
    costo_unitario_sin_iva NUMERIC(12, 2) NOT NULL,
    con_factura BOOLEAN NOT NULL DEFAULT TRUE,
    importe NUMERIC(12, 2) NOT NULL
);

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS parametros JSONB;
ALTER TABLE cotizacion_insumos
    ADD COLUMN IF NOT EXISTS partida_id INT REFERENCES cotizacion_partidas(id) ON DELETE CASCADE;
ALTER TABLE cotizacion_insumos
    ADD COLUMN IF NOT EXISTS tipo_insumo VARCHAR(30) NOT NULL DEFAULT 'Material';
ALTER TABLE cotizacion_insumos
    ADD COLUMN IF NOT EXISTS unidad VARCHAR(20) NOT NULL DEFAULT 'Pza';
ALTER TABLE cotizacion_insumos
    ADD COLUMN IF NOT EXISTS con_factura BOOLEAN NOT NULL DEFAULT TRUE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'cotizaciones'
          AND column_name = 'fecha_creacion'
          AND data_type = 'timestamp without time zone'
    ) THEN
        ALTER TABLE cotizaciones
            ALTER COLUMN fecha_creacion TYPE TIMESTAMPTZ
            USING fecha_creacion AT TIME ZONE 'UTC';
        ALTER TABLE cotizaciones
            ALTER COLUMN fecha_creacion SET DEFAULT now();
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS folio_consecutivo (
    anio INT PRIMARY KEY,
    ultimo INT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cot_fecha ON cotizaciones (fecha_creacion DESC);
CREATE INDEX IF NOT EXISTS idx_cotpart_folio ON cotizacion_partidas (folio, orden);
CREATE INDEX IF NOT EXISTS idx_cotins_folio ON cotizacion_insumos (folio);
"""

@st.cache_resource(show_spinner=False)
def init_db() -> bool:
  with get_conn() as conn:
    with conn.cursor() as cur:
      cur.execute(MIGRACION_SQL)
  return True

init_db()

def _txt(v, default: str = "") -> str:
  return default if v is None or pd.isna(v) else str(v).strip()

def _num(v, default: float) -> float:
  return default if v is None or pd.isna(v) else float(v)

def _tipo(v) -> str:
  s = _txt(v, "Material")
  return s if s in TIPOS_INSUMO else "Material"

# ---- Catálogo --------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def cargar_catalogo() -> pd.DataFrame:
  with get_conn() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
      cur.execute(
          "SELECT id, concepto, tipo_insumo, unidad,"
          " costo_sin_iva::float AS costo_sin_iva,"
          " cantidad_default::float AS cantidad_default, con_factura"
          " FROM catalogo_insumos WHERE activo = TRUE ORDER BY id"
      )
      rows = cur.fetchall()
  df = pd.DataFrame(rows, columns=COLS_CAT)
  df["id"] = df["id"].astype("Int64")
  df["tipo_insumo"] = df["tipo_insumo"].apply(_tipo)
  df["costo_sin_iva"] = df["costo_sin_iva"].astype(float)
  df["cantidad_default"] = df["cantidad_default"].astype(float)
  df["con_factura"] = df["con_factura"].astype(bool)
  return df

def guardar_catalogo(df: pd.DataFrame) -> list[str]:
  errores, filas = [], []
  for pos, row in df.iterrows():
    n = pos + 1 if isinstance(pos, int) else pos
    concepto = _txt(row.get("concepto"))
    es_nueva = pd.isna(row.get("id"))
    if not concepto:
      if not es_nueva:
        errores.append(f"Fila {n}: el concepto no puede quedar vacío.")
      continue
    if len(concepto) > 150:
      errores.append(f"Fila {n}: el concepto excede 150 caracteres.")
    tipo = _tipo(row.get("tipo_insumo"))
    unidad = _txt(row.get("unidad"), "Pza") or "Pza"
    if len(unidad) > 20:
      errores.append(f"Fila {n}: la unidad excede 20 caracteres.")
    costo = _num(row.get("costo_sin_iva"), 0.0)
    cant = _num(row.get("cantidad_default"), 1.0)
    if costo < 0 or cant < 0:
      errores.append(f"Fila {n}: costo y cantidad no pueden ser negativos.")
    if costo >= 1e10 or cant >= 1e8:
      errores.append(f"Fila {n}: costo o cantidad demasiado grandes.")
    con_fac = (
        True if pd.isna(row.get("con_factura")) else bool(row["con_factura"])
    )
    filas.append((
        None if es_nueva else int(row["id"]),
        concepto,
        tipo,
        unidad,
        round(costo, 2),
        round(cant, 2),
        con_fac,
    ))
  if errores:
    return errores

  with get_conn() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT id FROM catalogo_insumos WHERE activo = TRUE")
      existentes = {r[0] for r in cur.fetchall()}
      conservados = {f[0] for f in filas if f[0] is not None}
      a_desactivar = list(existentes - conservados)
      if a_desactivar:
        cur.execute(
            "UPDATE catalogo_insumos SET activo = FALSE WHERE id = ANY(%s)",
            (a_desactivar,),
        )
      for id_, concepto, tipo, unidad, costo, cant, con_fac in filas:
        if id_ is None:
          cur.execute(
              "INSERT INTO catalogo_insumos"
              " (concepto, tipo_insumo, unidad, costo_sin_iva, cantidad_default, con_factura)"
              " VALUES (%s, %s, %s, %s, %s, %s)",
              (concepto, tipo, unidad, costo, cant, con_fac),
          )
        else:
          cur.execute(
              "UPDATE catalogo_insumos SET concepto=%s, tipo_insumo=%s,"
              " unidad=%s, costo_sin_iva=%s, cantidad_default=%s,"
              " con_factura=%s WHERE id=%s",
              (concepto, tipo, unidad, costo, cant, con_fac, id_),
          )
  cargar_catalogo.clear()
  return []

# ---- Cálculo APU por partida y explosión de insumos ------------------------
def calcular_partida(
    df_sel: pd.DataFrame,
    cantidad_partida: float,
    costo_mo_base: float,
    factor_urg: float,
    pct_herr: float,
    pct_ind: float,
    pct_gar: float,
    pct_uti: float,
    regimen: str,
) -> dict:
  cant_p = max(0.0, float(cantidad_partida or 0.0))
  es_mat = df_sel["tipo_insumo"] == "Material"
  costo_mat_unit = float(df_sel.loc[es_mat, "Importe"].sum())
  costo_eq_unit = float(df_sel.loc[~es_mat, "Importe"].sum())

  con_fac_unit = float(df_sel.loc[df_sel["con_factura"], "Importe"].sum())
  sin_fac_unit = float(df_sel.loc[~df_sel["con_factura"], "Importe"].sum())

  costo_mo_unit = float(costo_mo_base) * float(factor_urg)
  herr_unit = costo_mo_unit * float(pct_herr)

  cd_unit = costo_mat_unit + costo_eq_unit + costo_mo_unit + herr_unit
  ci_unit = cd_unit * float(pct_ind)
  gar_unit = (cd_unit + ci_unit) * float(pct_gar)
  uti_unit = (cd_unit + ci_unit + gar_unit) * float(pct_uti)
  pu_sin_iva = cd_unit + ci_unit + gar_unit + uti_unit

  cd = cd_unit * cant_p
  ci = ci_unit * cant_p
  gar = gar_unit * cant_p
  uti = uti_unit * cant_p
  subtotal = pu_sin_iva * cant_p
  iva_tras = subtotal * IVA
  total = subtotal + iva_tras

  con_fac_tot = con_fac_unit * cant_p
  sin_fac_tot = sin_fac_unit * cant_p
  iva_acred_tot = con_fac_tot * IVA
  costo_mo_tot = costo_mo_unit * cant_p

  iva_pagar = max(0.0, iva_tras - iva_acred_tot)
  isr = (
      subtotal * ISR_RESICO
      if "RESICO" in regimen
      else max(0.0, uti * ISR_ACT_EMP)
  )
  desembolso = con_fac_tot * (1 + IVA) + sin_fac_tot
  ganancia = total - desembolso - iva_pagar - isr

  return {
      "costo_mat": costo_mat_unit * cant_p,
      "costo_eq": costo_eq_unit * cant_p,
      "costo_mo": costo_mo_tot,
      "herr": herr_unit * cant_p,
      "cd": cd,
      "ci": ci,
      "gar": gar,
      "uti": uti,
      "pu_sin_iva": pu_sin_iva,
      "subtotal": subtotal,
      "iva_tras": iva_tras,
      "total": total,
      "iva_pagar": iva_pagar,
      "isr": isr,
      "ganancia": ganancia,
      "ganancia_sin_mo": ganancia - costo_mo_tot,
  }

def construir_explosion(partidas_resumen: list[dict]) -> pd.DataFrame:
  """Consolida insumos de todas las partidas multiplicando por cantidad de partida."""
  filas = []
  for p in partidas_resumen:
    cant_p = max(0.0, float(p["cantidad"] or 0.0))
    df_p = p["df_sel"]
    usados = df_p[df_p["Cantidad"] > 0]
    for _, r in usados.iterrows():
      cant_req = float(r["Cantidad"]) * cant_p
      pu = float(r["costo_sin_iva"])
      filas.append({
          "Concepto": str(r["Concepto"]),
          "Tipo": _tipo(r.get("tipo_insumo")),
          "Unidad": _txt(r.get("unidad"), "Pza") or "Pza",
          "Cantidad Total": round(cant_req, 2),
          "P.U. s/IVA": round(pu, 2),
          "Importe Total": round(cant_req * pu, 2),
          "Con factura": bool(r.get("con_factura", True)),
      })
  if not filas:
    return pd.DataFrame(
        columns=[
            "Concepto",
            "Tipo",
            "Unidad",
            "Cantidad Total",
            "P.U. s/IVA",
            "Importe Total",
            "Con factura",
        ]
    )
  df_raw = pd.DataFrame(filas)
  agg = (
      df_raw.groupby(
          ["Concepto", "Tipo", "Unidad", "P.U. s/IVA", "Con factura"],
          as_index=False,
      )[["Cantidad Total", "Importe Total"]]
      .sum()
      .sort_values(["Tipo", "Concepto"])
      .reset_index(drop=True)
  )
  return agg[
      [
          "Concepto",
          "Tipo",
          "Unidad",
          "Cantidad Total",
          "P.U. s/IVA",
          "Importe Total",
          "Con factura",
      ]
  ]

# ---- Cotizaciones ----------------------------------------------------------
def siguiente_folio(cur) -> str:
  anio = datetime.now(TZ).year
  cur.execute(
      "INSERT INTO folio_consecutivo (anio, ultimo) VALUES (%s, 1)"
      " ON CONFLICT (anio) DO UPDATE SET ultimo = folio_consecutivo.ultimo + 1"
      " RETURNING ultimo",
      (anio,),
  )
  return f"COT-{anio}-{cur.fetchone()[0]:04d}"

def guardar_cotizacion_multipartida(
    datos: dict,
    partidas_resumen: list[dict],
    parametros_global: dict,
    sobrescribir: bool,
) -> str:
  payload = {**datos, "parametros": Json(parametros_global)}
  with get_conn() as conn:
    with conn.cursor() as cur:
      if not payload["folio"]:
        payload["folio"] = siguiente_folio(cur)
        cur.execute(
            """
                INSERT INTO cotizaciones (
                    folio, cliente, ubicacion, descripcion_servicio, factor_urgencia,
                    costo_directo, indirectos_monto, garantia_monto, utilidad_monto,
                    subtotal, iva_trasladado, total_facturado, iva_por_pagar,
                    isr_estimado, ganancia_neta_libre, parametros
                ) VALUES (
                    %(folio)s, %(cliente)s, %(ubicacion)s, %(descripcion_servicio)s, %(factor_urgencia)s,
                    %(costo_directo)s, %(indirectos_monto)s, %(garantia_monto)s, %(utilidad_monto)s,
                    %(subtotal)s, %(iva_trasladado)s, %(total_facturado)s, %(iva_por_pagar)s,
                    %(isr_estimado)s, %(ganancia_neta_libre)s, %(parametros)s
                )
                """,
            payload,
        )
      elif sobrescribir:
        cur.execute(
            """
                UPDATE cotizaciones SET
                    cliente = %(cliente)s,
                    ubicacion = %(ubicacion)s,
                    descripcion_servicio = %(descripcion_servicio)s,
                    factor_urgencia = %(factor_urgencia)s,
                    costo_directo = %(costo_directo)s,
                    indirectos_monto = %(indirectos_monto)s,
                    garantia_monto = %(garantia_monto)s,
                    utilidad_monto = %(utilidad_monto)s,
                    subtotal = %(subtotal)s,
                    iva_trasladado = %(iva_trasladado)s,
                    total_facturado = %(total_facturado)s,
                    iva_por_pagar = %(iva_por_pagar)s,
                    isr_estimado = %(isr_estimado)s,
                    ganancia_neta_libre = %(ganancia_neta_libre)s,
                    parametros = %(parametros)s
                WHERE folio = %(folio)s
                """,
            payload,
        )
        if cur.rowcount == 0:
          raise errors.UndefinedTable
        cur.execute(
            "DELETE FROM cotizacion_insumos WHERE folio = %s",
            (payload["folio"],),
        )
        cur.execute(
            "DELETE FROM cotizacion_partidas WHERE folio = %s",
            (payload["folio"],),
        )
      else:
        cur.execute(
            """
                INSERT INTO cotizaciones (
                    folio, cliente, ubicacion, descripcion_servicio, factor_urgencia,
                    costo_directo, indirectos_monto, garantia_monto, utilidad_monto,
                    subtotal, iva_trasladado, total_facturado, iva_por_pagar,
                    isr_estimado, ganancia_neta_libre, parametros
                ) VALUES (
                    %(folio)s, %(cliente)s, %(ubicacion)s, %(descripcion_servicio)s, %(factor_urgencia)s,
                    %(costo_directo)s, %(indirectos_monto)s, %(garantia_monto)s, %(utilidad_monto)s,
                    %(subtotal)s, %(iva_trasladado)s, %(total_facturado)s, %(iva_por_pagar)s,
                    %(isr_estimado)s, %(ganancia_neta_libre)s, %(parametros)s
                )
                """,
            payload,
        )

      for idx, p in enumerate(partidas_resumen, start=1):
        res = p["res"]
        cur.execute(
            """
                INSERT INTO cotizacion_partidas (
                    folio, orden, nombre, unidad, cantidad, pu_sin_iva, importe,
                    costo_directo, ganancia_neta_libre, parametros
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
            (
                payload["folio"],
                idx,
                _txt(p["nombre"], f"Partida {idx}"),
                _txt(p["unidad"], "Serv") or "Serv",
                round(float(p["cantidad"]), 2),
                round(float(res["pu_sin_iva"]), 2),
                round(float(res["subtotal"]), 2),
                round(float(res["cd"]), 2),
                round(float(res["ganancia"]), 2),
                Json(p["parametros_partida"]),
            ),
        )
        partida_id = cur.fetchone()[0]
        usados = p["df_sel"][p["df_sel"]["Cantidad"] > 0]
        if not usados.empty:
          cur.executemany(
              "INSERT INTO cotizacion_insumos"
              " (folio, partida_id, concepto, tipo_insumo, unidad, cantidad,"
              " costo_unitario_sin_iva, con_factura, importe)"
              " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
              [
                  (
                      payload["folio"],
                      partida_id,
                      str(it["Concepto"]),
                      _tipo(it.get("tipo_insumo")),
                      _txt(it["unidad"], "Pza") or "Pza",
                      round(float(it["Cantidad"]), 2),
                      round(float(it["costo_sin_iva"]), 2),
                      bool(it["con_factura"]),
                      round(float(it["Importe"]), 2),
                  )
                  for _, it in usados.iterrows()
              ],
          )

  cargar_historial.clear()
  return payload["folio"]

@st.cache_data(ttl=60, show_spinner=False)
def cargar_historial() -> pd.DataFrame:
  cols = [
      "folio",
      "cliente",
      "ubicacion",
      "total_facturado",
      "ganancia_neta_libre",
      "fecha_creacion",
  ]
  with get_conn() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
      cur.execute(
          "SELECT folio, cliente, ubicacion, total_facturado::float AS"
          " total_facturado, ganancia_neta_libre::float AS"
          " ganancia_neta_libre, fecha_creacion FROM cotizaciones ORDER BY"
          " fecha_creacion DESC"
      )
      rows = cur.fetchall()
  df = pd.DataFrame(rows, columns=cols)
  if not df.empty:
    df["fecha_creacion"] = (
        pd.to_datetime(df["fecha_creacion"], utc=True)
        .dt.tz_convert(TZ)
        .dt.strftime("%Y-%m-%d %H:%M")
    )
  return df

def obtener_cotizacion_completa(folio: str):
  with get_conn() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
      cur.execute(
          "SELECT folio, cliente, ubicacion, descripcion_servicio,"
          " factor_urgencia::float AS factor_urgencia, parametros"
          " FROM cotizaciones WHERE folio = %s",
          (folio,),
      )
      cot = cur.fetchone()
      cur.execute(
          "SELECT id, orden, nombre, unidad, cantidad::float AS cantidad,"
          " pu_sin_iva::float AS pu_sin_iva, importe::float AS importe,"
          " parametros FROM cotizacion_partidas WHERE folio = %s ORDER BY"
          " orden, id",
          (folio,),
      )
      partidas = cur.fetchall()
      cur.execute(
          "SELECT partida_id, concepto, tipo_insumo, unidad,"
          " cantidad::float AS cantidad, costo_unitario_sin_iva::float AS"
          " costo_sin_iva, con_factura FROM cotizacion_insumos WHERE folio ="
          " %s ORDER BY id",
          (folio,),
      )
      insumos = cur.fetchall()
  return cot, partidas, insumos

# ----------------------------------------------------------------------------
# Exportación Excel y PDF (Multi-Partida + Explosión)
# ----------------------------------------------------------------------------
def generar_excel_cotizacion(
    datos: dict, partidas_resumen: list[dict], df_explosion: pd.DataFrame
) -> bytes:
  wb = openpyxl.Workbook()
  ws = wb.active
  ws.title = "Cotizacion"

  header_fill = PatternFill(
      start_color="1F3A52", end_color="1F3A52", fill_type="solid"
  )
  sub_fill = PatternFill(
      start_color="E8EEF5", end_color="E8EEF5", fill_type="solid"
  )
  font_title = Font(bold=True, size=14, color="1F3A52")
  font_header = Font(bold=True, color="FFFFFF")
  font_bold = Font(bold=True)
  thin_border = Border(
      left=Side(style="thin", color="D0D7DE"),
      right=Side(style="thin", color="D0D7DE"),
      top=Side(style="thin", color="D0D7DE"),
      bottom=Side(style="thin", color="D0D7DE"),
  )

  ws["A1"] = "COTIZACIÓN DE SERVICIO ELECTROMECÁNICO"
  ws["A1"].font = font_title

  ws["A3"] = "Folio:"
  ws["A3"].font = font_bold
  ws["B3"] = datos["folio"] or "BORRADOR"
  ws["D3"] = "Fecha:"
  ws["D3"].font = font_bold
  ws["E3"] = datetime.now(TZ).strftime("%d/%m/%Y")

  ws["A4"] = "Cliente:"
  ws["A4"].font = font_bold
  ws["B4"] = datos["cliente"]
  ws["D4"] = "Ubicación:"
  ws["D4"].font = font_bold
  ws["E4"] = datos["ubicacion"]

  row_idx = 6
  ws.cell(
      row=row_idx, column=1, value="Partidas / Conceptos del Servicio"
  ).font = font_bold
  row_idx += 1
  headers = ["Partida / Concepto", "Unidad", "Cantidad", "P.U. s/IVA", "Importe"]
  for col_i, h in enumerate(headers, start=1):
    cell = ws.cell(row=row_idx, column=col_i, value=h)
    cell.fill = header_fill
    cell.font = font_header
    cell.alignment = Alignment(horizontal="center")
  row_idx += 1

  for p in partidas_resumen:
    res = p["res"]
    ws.cell(row=row_idx, column=1, value=p["nombre"]).border = thin_border
    ws.cell(row=row_idx, column=2, value=p["unidad"]).border = thin_border
    c_cant = ws.cell(row=row_idx, column=3, value=float(p["cantidad"]))
    c_cant.number_format = "#,##0.00"
    c_cant.border = thin_border
    c_pu = ws.cell(row=row_idx, column=4, value=float(res["pu_sin_iva"]))
    c_pu.number_format = "$#,##0.00"
    c_pu.border = thin_border
    c_imp = ws.cell(row=row_idx, column=5, value=float(res["subtotal"]))
    c_imp.number_format = "$#,##0.00"
    c_imp.border = thin_border
    row_idx += 1

  row_idx += 1
  resumen = [
      ("Subtotal del Servicio (antes de IVA)", datos["subtotal"]),
      ("IVA (16%)", datos["iva_trasladado"]),
      ("TOTAL A PAGAR (MXN)", datos["total_facturado"]),
  ]
  for label, val in resumen:
    c_lbl = ws.cell(row=row_idx, column=4, value=label)
    c_val = ws.cell(row=row_idx, column=5, value=float(val))
    c_val.number_format = "$#,##0.00"
    c_lbl.border = thin_border
    c_val.border = thin_border
    if "TOTAL" in label:
      c_lbl.font = font_bold
      c_val.font = font_bold
      c_lbl.fill = sub_fill
      c_val.fill = sub_fill
    row_idx += 1

  if not df_explosion.empty:
    ws_exp = wb.create_sheet(title="Explosion_Insumos")
    ws_exp["A1"] = "EXPLOSIÓN DE INSUMOS Y EQUIPOS"
    ws_exp["A1"].font = font_title
    exp_cols = ["Concepto", "Tipo", "Unidad", "Cantidad Total", "Importe Total"]
    for col_i, h in enumerate(exp_cols, start=1):
      cell = ws_exp.cell(row=3, column=col_i, value=h)
      cell.fill = header_fill
      cell.font = font_header
      cell.alignment = Alignment(horizontal="center")
    r_i = 4
    for _, er in df_explosion.iterrows():
      ws_exp.cell(row=r_i, column=1, value=er["Concepto"]).border = thin_border
      ws_exp.cell(row=r_i, column=2, value=er["Tipo"]).border = thin_border
      ws_exp.cell(row=r_i, column=3, value=er["Unidad"]).border = thin_border
      c1 = ws_exp.cell(row=r_i, column=4, value=float(er["Cantidad Total"]))
      c1.number_format = "#,##0.00"
      c1.border = thin_border
      c2 = ws_exp.cell(row=r_i, column=5, value=float(er["Importe Total"]))
      c2.number_format = "$#,##0.00"
      c2.border = thin_border
      r_i += 1
    ws_exp.column_dimensions["A"].width = 40
    ws_exp.column_dimensions["B"].width = 22
    ws_exp.column_dimensions["C"].width = 14
    ws_exp.column_dimensions["D"].width = 16
    ws_exp.column_dimensions["E"].width = 18

  ws.column_dimensions["A"].width = 48
  ws.column_dimensions["B"].width = 14
  ws.column_dimensions["C"].width = 14
  ws.column_dimensions["D"].width = 28
  ws.column_dimensions["E"].width = 18

  buf = io.BytesIO()
  wb.save(buf)
  return buf.getvalue()


def generar_pdf_cotizacion(
    datos: dict, partidas_resumen: list[dict], df_explosion: pd.DataFrame
) -> bytes:
  def limpio(txt: str) -> str:
    return (
        str(txt)
        .replace("µ", "u")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )

  pdf = FPDF()
  pdf.add_page()
  pdf.set_auto_page_break(auto=True, margin=15)

  pdf.set_fill_color(31, 58, 82)
  pdf.set_text_color(255, 255, 255)
  pdf.set_font("Helvetica", "B", 14)
  pdf.cell(
      0,
      12,
      limpio("COTIZACIÓN DE SERVICIO ELECTROMECÁNICO"),
      ln=True,
      align="C",
      fill=True,
  )
  pdf.ln(5)

  pdf.set_text_color(30, 30, 30)
  pdf.set_font("Helvetica", "B", 10)
  pdf.cell(25, 7, "Folio:", 0, 0)
  pdf.set_font("Helvetica", "", 10)
  pdf.cell(80, 7, limpio(datos["folio"] or "BORRADOR"), 0, 0)
  pdf.set_font("Helvetica", "B", 10)
  pdf.cell(25, 7, "Fecha:", 0, 0)
  pdf.set_font("Helvetica", "", 10)
  pdf.cell(0, 7, datetime.now(TZ).strftime("%d/%m/%Y"), 0, 1)

  pdf.set_font("Helvetica", "B", 10)
  pdf.cell(25, 7, "Cliente:", 0, 0)
  pdf.set_font("Helvetica", "", 10)
  pdf.cell(80, 7, limpio(datos["cliente"]), 0, 0)
  pdf.set_font("Helvetica", "B", 10)
  pdf.cell(25, 7, limpio("Ubicación:"), 0, 0)
  pdf.set_font("Helvetica", "", 10)
  pdf.cell(0, 7, limpio(datos["ubicacion"]), 0, 1)
  pdf.ln(4)

  pdf.set_font("Helvetica", "B", 10)
  pdf.cell(0, 7, limpio("Partidas / Conceptos del Servicio:"), 0, 1)
  pdf.set_fill_color(232, 238, 245)
  pdf.cell(95, 7, limpio("Concepto / Partida"), 1, 0, "C", True)
  pdf.cell(18, 7, "Unid.", 1, 0, "C", True)
  pdf.cell(20, 7, "Cant.", 1, 0, "C", True)
  pdf.cell(28, 7, "P.U. s/IVA", 1, 0, "C", True)
  pdf.cell(29, 7, "Importe", 1, 1, "C", True)

  pdf.set_font("Helvetica", "", 9)
  for p in partidas_resumen:
    res = p["res"]
    pdf.cell(95, 7, limpio(p["nombre"])[:48], 1, 0)
    pdf.cell(18, 7, limpio(p["unidad"]), 1, 0, "C")
    pdf.cell(20, 7, f"{float(p['cantidad']):,.2f}", 1, 0, "R")
    pdf.cell(28, 7, f"${float(res['pu_sin_iva']):,.2f}", 1, 0, "R")
    pdf.cell(29, 7, f"${float(res['subtotal']):,.2f}", 1, 1, "R")
  pdf.ln(5)

  pdf.set_x(105)
  pdf.set_font("Helvetica", "", 10)
  pdf.cell(55, 7, "Subtotal (sin IVA):", 1, 0, "R")
  pdf.cell(35, 7, f"${datos['subtotal']:,.2f}", 1, 1, "R")

  pdf.set_x(105)
  pdf.cell(55, 7, "IVA (16%):", 1, 0, "R")
  pdf.cell(35, 7, f"${datos['iva_trasladado']:,.2f}", 1, 1, "R")

  pdf.set_x(105)
  pdf.set_font("Helvetica", "B", 11)
  pdf.set_fill_color(232, 238, 245)
  pdf.cell(55, 8, "TOTAL (MXN):", 1, 0, "R", True)
  pdf.cell(35, 8, f"${datos['total_facturado']:,.2f}", 1, 1, "R", True)

  if not df_explosion.empty:
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, limpio("Explosión de Insumos y Equipos Requeridos:"), 0, 1)
    pdf.set_fill_color(232, 238, 245)
    pdf.cell(110, 7, "Concepto", 1, 0, "C", True)
    pdf.cell(35, 7, "Tipo", 1, 0, "C", True)
    pdf.cell(20, 7, "Unid.", 1, 0, "C", True)
    pdf.cell(25, 7, "Cant.", 1, 1, "C", True)
    pdf.set_font("Helvetica", "", 9)
    for _, er in df_explosion.iterrows():
      pdf.cell(110, 7, limpio(er["Concepto"])[:56], 1, 0)
      pdf.cell(35, 7, limpio(er["Tipo"]), 1, 0, "C")
      pdf.cell(20, 7, limpio(er["Unidad"]), 1, 0, "C")
      pdf.cell(25, 7, f"{float(er['Cantidad Total']):,.2f}", 1, 1, "R")

  pdf.ln(7)
  pdf.set_font("Helvetica", "I", 8)
  pdf.set_text_color(90, 90, 90)
  pdf.multi_cell(
      0,
      5,
      limpio(
          "Nota: Precios expresados en Pesos Mexicanos (MXN). Vigencia de"
          " cotización: 15 días naturales. Cualquier trabajo adicional no"
          " especificado en el alcance se cotizará por separado."
      ),
  )

  return bytes(pdf.output())

# ----------------------------------------------------------------------------
# Utilidades de interfaz y callbacks
# ----------------------------------------------------------------------------
def tabla(fn, *args, **kwargs):
  try:
    return fn(*args, width="stretch", **kwargs)
  except (TypeError, st.errors.StreamlitAPIException):
    return fn(*args, use_container_width=True, **kwargs)

def base_desde_catalogo(
    cat: pd.DataFrame, en_cero: bool = False
) -> pd.DataFrame:
  df = cat.rename(columns={"concepto": "Concepto"}).copy()
  df["Cantidad"] = 0.0 if en_cero else df["cantidad_default"].fillna(0.0)
  return df[COLS_SEL].reset_index(drop=True)

def _clamp(v, lo, hi):
  return int(max(lo, min(hi, round(float(v)))))

def cb_agregar_partida(cat: pd.DataFrame):
  ss = st.session_state
  idx_actual = min(ss["partida_activa"], len(ss["partidas"]) - 1)
  actual = ss["partidas"][idx_actual]
  nueva = _nueva_partida_estado(
      nombre=f"Partida {len(ss['partidas']) + 1}",
      unidad="Serv",
      cantidad=1.0,
      esquema=actual["esquema"],
      factor_urg=1.0,
      pct_herr=actual["pct_herr"],
      pct_ind=actual["pct_ind"],
      pct_gar=actual["pct_gar"],
      pct_uti=actual["pct_uti"],
      regimen=actual["regimen"],
      dividir=actual["dividir"],
      pct_ing_1=actual["pct_ing_1"],
      insumos_df=base_desde_catalogo(cat, en_cero=True),
      iniciar_en_cero=True,
  )
  ss["partidas"].append(nueva)
  ss["partida_activa"] = len(ss["partidas"]) - 1
  ss["ver"] += 1

def cb_eliminar_partida():
  ss = st.session_state
  if len(ss["partidas"]) <= 1:
    return
  idx = min(ss["partida_activa"], len(ss["partidas"]) - 1)
  ss["partidas"].pop(idx)
  ss["partida_activa"] = max(0, min(idx, len(ss["partidas"]) - 1))
  ss["ver"] += 1

def cb_nueva():
  ss = st.session_state
  ver = ss["ver"] + 1
  for k, v in _defaults().items():
    ss[k] = v
  ss["ver"] = ver

def cb_cargar(folio: str):
  ss = st.session_state
  cot, partidas_db, insumos_db = obtener_cotizacion_completa(folio)
  if not cot:
    ss["msg_historial"] = ("error", f"No se encontró la cotización {folio}.")
    return

  cat = cargar_catalogo()
  ss["folio"] = cot["folio"]
  ss["cliente"] = cot["cliente"]
  ss["ubicacion"] = cot["ubicacion"] or ""
  ss["sobrescribir"] = False

  partidas_cargadas = []
  if partidas_db:
    ins_por_partida = {}
    for ins in insumos_db:
      ins_por_partida.setdefault(ins["partida_id"], []).append(ins)

    for pdb in partidas_db:
      pp = pdb["parametros"] or {}
      esq = pp.get("esquema", NOMBRES_ESQUEMA[0])
      if esq not in ESQUEMAS:
        esq = NOMBRES_ESQUEMA[0]
      e_idx = NOMBRES_ESQUEMA.index(esq)
      th = {}
      for i, (t, h) in enumerate(zip(pp.get("tarifas", []), pp.get("horas", []))):
        th[f"mo_t_{e_idx}_{i}"] = float(t)
        th[f"mo_h_{e_idx}_{i}"] = float(h)

      ins_p = ins_por_partida.get(pdb["id"], [])
      filas = [
          {
              "Concepto": i["concepto"],
              "tipo_insumo": _tipo(i.get("tipo_insumo")),
              "unidad": i["unidad"],
              "costo_sin_iva": i["costo_sin_iva"],
              "con_factura": i["con_factura"],
              "Cantidad": i["cantidad"],
          }
          for i in ins_p
      ]
      usados_names = {i["concepto"] for i in ins_p}
      for _, r in cat.iterrows():
        if r["concepto"] not in usados_names:
          filas.append({
              "Concepto": r["concepto"],
              "tipo_insumo": _tipo(r.get("tipo_insumo")),
              "unidad": r["unidad"],
              "costo_sin_iva": r["costo_sin_iva"],
              "con_factura": r["con_factura"],
              "Cantidad": 0.0,
          })

      partidas_cargadas.append(
          _nueva_partida_estado(
              nombre=pdb["nombre"],
              unidad=pdb["unidad"] or "Serv",
              cantidad=float(pdb["cantidad"] or 1.0),
              esquema=esq,
              factor_urg=float(pp.get("factor_urg", 1.25)),
              pct_herr=_clamp(pp.get("pct_herr", 3), 0, 10),
              pct_ind=_clamp(pp.get("pct_ind", 12), 0, 30),
              pct_gar=_clamp(pp.get("pct_gar", 5), 0, 15),
              pct_uti=_clamp(pp.get("pct_uti", 25), 5, 60),
              regimen=pp.get("regimen"),
              dividir=bool(pp.get("dividir", True)),
              pct_ing_1=_clamp(pp.get("pct_ing_1", 50), 0, 100),
              insumos_df=pd.DataFrame(filas, columns=COLS_SEL),
              tarifas_horas=th,
          )
      )
  else:
    p_leg = cot["parametros"] or {}
    esq = p_leg.get("esquema", NOMBRES_ESQUEMA[0])
    if esq not in ESQUEMAS:
      esq = NOMBRES_ESQUEMA[0]
    e_idx = NOMBRES_ESQUEMA.index(esq)
    th = {}
    for i, (t, h) in enumerate(
        zip(p_leg.get("tarifas", []), p_leg.get("horas", []))
    ):
      th[f"mo_t_{e_idx}_{i}"] = float(t)
      th[f"mo_h_{e_idx}_{i}"] = float(h)
    filas = [
        {
            "Concepto": i["concepto"],
            "tipo_insumo": _tipo(i.get("tipo_insumo")),
            "unidad": i["unidad"],
            "costo_sin_iva": i["costo_sin_iva"],
            "con_factura": i["con_factura"],
            "Cantidad": i["cantidad"],
        }
        for i in insumos_db
    ]
    usados_names = {i["concepto"] for i in insumos_db}
    for _, r in cat.iterrows():
      if r["concepto"] not in usados_names:
        filas.append({
            "Concepto": r["concepto"],
            "tipo_insumo": _tipo(r.get("tipo_insumo")),
            "unidad": r["unidad"],
            "costo_sin_iva": r["costo_sin_iva"],
            "con_factura": r["con_factura"],
            "Cantidad": 0.0,
        })
    partidas_cargadas.append(
        _nueva_partida_estado(
            nombre=cot["descripcion_servicio"] or "Partida 1",
            unidad="Serv",
            cantidad=1.0,
            esquema=esq,
            factor_urg=float(cot["factor_urgencia"] or 1.25),
            pct_herr=_clamp(p_leg.get("pct_herr", 3), 0, 10),
            pct_ind=_clamp(p_leg.get("pct_ind", 12), 0, 30),
            pct_gar=_clamp(p_leg.get("pct_gar", 5), 0, 15),
            pct_uti=_clamp(p_leg.get("pct_uti", 25), 5, 60),
            regimen=p_leg.get("regimen"),
            dividir=bool(p_leg.get("dividir", True)),
            pct_ing_1=_clamp(p_leg.get("pct_ing_1", 50), 0, 100),
            insumos_df=pd.DataFrame(filas, columns=COLS_SEL),
            tarifas_horas=th,
        )
    )

  ss["partidas"] = partidas_cargadas
  ss["partida_activa"] = 0
  ss["ver"] += 1
  ss["cot_cargada"] = cot["folio"]
  ss["msg_historial"] = (
      "success",
      f"Cotización {folio} cargada con {len(partidas_cargadas)} partida(s).",
  )

# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------
ss = st.session_state
tab1, tab2, tab3, tab4 = st.tabs([
    "1. Catálogo",
    "2. Cotización y APU",
    "3. Explosión de Insumos",
    "4. Historial",
])

with tab1:
  st.subheader("Catálogo de Insumos y Equipos")
  st.caption(
      "Clasifica cada concepto como **Material** o **Equipo / Herramienta**."
      " Para eliminar una fila, selecciónala y presiona Supr."
  )
  df_cat = cargar_catalogo()
  df_editado = tabla(
      st.data_editor,
      df_cat,
      num_rows="dynamic",
      column_config={
          "id": st.column_config.NumberColumn("id", disabled=True),
          "concepto": st.column_config.TextColumn("Concepto", max_chars=150),
          "tipo_insumo": st.column_config.SelectboxColumn(
              "Tipo", options=TIPOS_INSUMO, default="Material"
          ),
          "unidad": st.column_config.TextColumn(
              "Unidad", max_chars=20, default="Pza"
          ),
          "costo_sin_iva": st.column_config.NumberColumn(
              "Costo s/IVA", min_value=0.0, format="$%.2f", default=0.0
          ),
          "cantidad_default": st.column_config.NumberColumn(
              "Cantidad", min_value=0.0, step=0.5, format="%.2f", default=1.0
          ),
          "con_factura": st.column_config.CheckboxColumn(
              "Con factura", default=True
          ),
      },
      key="editor_catalogo",
  )
  if st.button("💾 Guardar cambios"):
    errs = guardar_catalogo(df_editado)
    if errs:
      for e in errs:
        st.error(e)
    else:
      st.success("Catálogo actualizado.")
      st.rerun()

# Evaluamos todas las partidas para tener totales y explosión disponibles en todas las pestañas
cat_actual = cargar_catalogo()
partidas_resumen = []

for idx_p, p_state in enumerate(ss["partidas"]):
  p_state.setdefault("uid", _siguiente_uid())
  if p_state["insumos_df"] is None:
    p_state["insumos_df"] = base_desde_catalogo(
        cat_actual, en_cero=bool(p_state.get("iniciar_en_cero", False))
    )
  base_p = p_state["insumos_df"].copy()
  base_p["Cantidad"] = base_p["Cantidad"].fillna(0.0).astype(float)
  base_p["tipo_insumo"] = base_p["tipo_insumo"].apply(_tipo)
  base_p["costo_sin_iva"] = base_p["costo_sin_iva"].astype(float)
  base_p["con_factura"] = base_p["con_factura"].fillna(True).astype(bool)
  base_p["Importe"] = base_p["costo_sin_iva"] * base_p["Cantidad"]

  esq_p = (
      p_state["esquema"]
      if p_state["esquema"] in ESQUEMAS
      else NOMBRES_ESQUEMA[0]
  )
  e_i = NOMBRES_ESQUEMA.index(esq_p)
  personas_p = ESQUEMAS[esq_p]
  tarifas_p, horas_p = [], []
  for j in range(len(personas_p)):
    tarifas_p.append(float(p_state.get(f"mo_t_{e_i}_{j}", 300.0)))
    horas_p.append(float(p_state.get(f"mo_h_{e_i}_{j}", 4.0)))
  mo_base_p = sum(t * h for t, h in zip(tarifas_p, horas_p))

  res_p = calcular_partida(
      df_sel=base_p,
      cantidad_partida=float(p_state.get("cantidad", 1.0)),
      costo_mo_base=mo_base_p,
      factor_urg=float(p_state.get("factor_urg", 1.25)),
      pct_herr=float(p_state.get("pct_herr", 3)) / 100.0,
      pct_ind=float(p_state.get("pct_ind", 12)) / 100.0,
      pct_gar=float(p_state.get("pct_gar", 5)) / 100.0,
      pct_uti=float(p_state.get("pct_uti", 25)) / 100.0,
      regimen=p_state.get("regimen", REGIMENES[0]),
  )

  partidas_resumen.append({
      "idx": idx_p,
      "nombre": p_state.get("nombre", f"Partida {idx_p + 1}"),
      "unidad": p_state.get("unidad", "Serv"),
      "cantidad": float(p_state.get("cantidad", 1.0)),
      "df_sel": base_p,
      "res": res_p,
      "parametros_partida": {
          "version": 5,
          "esquema": esq_p,
          "tarifas": tarifas_p,
          "horas": horas_p,
          "factor_urg": float(p_state.get("factor_urg", 1.25)),
          "pct_herr": int(p_state.get("pct_herr", 3)),
          "pct_ind": int(p_state.get("pct_ind", 12)),
          "pct_gar": int(p_state.get("pct_gar", 5)),
          "pct_uti": int(p_state.get("pct_uti", 25)),
          "regimen": p_state.get("regimen", REGIMENES[0]),
          "dividir": bool(p_state.get("dividir", True)),
          "pct_ing_1": int(p_state.get("pct_ing_1", 50)),
      },
  })

df_explosion = construir_explosion(partidas_resumen)

tot_cd = sum(x["res"]["cd"] for x in partidas_resumen)
tot_ci = sum(x["res"]["ci"] for x in partidas_resumen)
tot_gar = sum(x["res"]["gar"] for x in partidas_resumen)
tot_uti = sum(x["res"]["uti"] for x in partidas_resumen)
tot_sub = sum(x["res"]["subtotal"] for x in partidas_resumen)
tot_iva = sum(x["res"]["iva_tras"] for x in partidas_resumen)
tot_fac = sum(x["res"]["total"] for x in partidas_resumen)
tot_iva_pag = sum(x["res"]["iva_pagar"] for x in partidas_resumen)
tot_isr = sum(x["res"]["isr"] for x in partidas_resumen)
tot_gan = sum(x["res"]["ganancia"] for x in partidas_resumen)
tot_mo = sum(x["res"]["costo_mo"] for x in partidas_resumen)
tot_gan_sin_mo = sum(x["res"]["ganancia_sin_mo"] for x in partidas_resumen)

with tab2:
  if cat_actual.empty and all(
      ps["insumos_df"] is None for ps in ss["partidas"]
  ):
    st.warning("Primero agrega insumos en la pestaña «1. Catálogo».")
  else:
    top_a, top_b = st.columns([4, 1])
    if ss["cot_cargada"]:
      top_a.info(
          f"Editando **{ss['cot_cargada']}**. Marca «Actualizar si el folio ya"
          " existe» para sobrescribir o borra el folio para crear uno nuevo."
      )
    else:
      top_a.caption(
          "Captura los datos generales y edita el Análisis de Precio Unitario"
          " (APU) de cada partida."
      )
    top_b.button(
        "➕ Nueva cotización",
        on_click=cb_nueva,
        use_container_width=True,
        help="Limpia el formulario y reinicia las partidas para crear una cotización desde cero.",
    )

    c1, c2, c3 = st.columns(3)
    c1.text_input(
        "Folio",
        key="folio",
        max_chars=30,
        placeholder="Automático al guardar (COT-AAAA-0001)",
        help="Déjalo vacío para que se asigne el siguiente folio consecutivo.",
    )
    c2.text_input("Cliente", key="cliente", max_chars=150)
    c3.text_input("Ubicación", key="ubicacion", max_chars=120)

    st.markdown("### Partidas del presupuesto")
    df_partidas_tabla = pd.DataFrame([
        {
            "#": x["idx"] + 1,
            "Partida / Concepto": x["nombre"],
            "Unidad": x["unidad"],
            "Cantidad": x["cantidad"],
            "P.U. s/IVA": x["res"]["pu_sin_iva"],
            "Importe s/IVA": x["res"]["subtotal"],
            "Ganancia Libre": x["res"]["ganancia"],
        }
        for x in partidas_resumen
    ])
    tabla(
        st.dataframe,
        df_partidas_tabla,
        column_config={
            "Cantidad": st.column_config.NumberColumn(format="%.2f"),
            "P.U. s/IVA": st.column_config.NumberColumn(format="$%.2f"),
            "Importe s/IVA": st.column_config.NumberColumn(format="$%.2f"),
            "Ganancia Libre": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

    pa1, pa2, pa3 = st.columns([3, 1, 1])
    opciones_p = [
        f"Partida {i + 1}: {p.get('nombre', '')[:45]}"
        for i, p in enumerate(ss["partidas"])
    ]
    sel_p_idx = pa1.selectbox(
        "Seleccionar partida para editar su APU",
        range(len(opciones_p)),
        index=min(ss["partida_activa"], len(opciones_p) - 1),
        format_func=lambda i: opciones_p[i],
    )
    ss["partida_activa"] = sel_p_idx
    pa2.button(
        "➕ Agregar partida",
        on_click=cb_agregar_partida,
        args=(cat_actual,),
        use_container_width=True,
        help="Agrega una nueva partida limpia (con insumos en 0.00) al presupuesto.",
    )
    pa3.button(
        "🗑️ Quitar partida",
        on_click=cb_eliminar_partida,
        disabled=len(ss["partidas"]) <= 1,
        use_container_width=True,
        help="Elimina la partida seleccionada actualmente.",
    )

    p_cur = ss["partidas"][ss["partida_activa"]]
    uid = p_cur["uid"]
    st.markdown(f"#### APU — Partida {ss['partida_activa'] + 1}")
    pn1, pn2, pn3 = st.columns([4, 1, 1])
    p_cur["nombre"] = pn1.text_input(
        "Concepto de la partida",
        value=p_cur["nombre"],
        key=f"p_nom_{uid}_{ss['ver']}",
    )
    p_cur["unidad"] = pn2.text_input(
        "Unidad",
        value=p_cur["unidad"],
        max_chars=20,
        key=f"p_uni_{uid}_{ss['ver']}",
    )
    p_cur["cantidad"] = pn3.number_input(
        "Cantidad de partida",
        min_value=0.0,
        step=0.5,
        value=float(p_cur["cantidad"]),
        format="%.2f",
        key=f"p_cant_{uid}_{ss['ver']}",
    )

    df_edit_p = tabla(
        st.data_editor,
        p_cur["insumos_df"],
        column_config={
            "tipo_insumo": st.column_config.TextColumn("Tipo"),
            "unidad": "Unidad",
            "costo_sin_iva": st.column_config.NumberColumn(
                "Costo s/IVA", format="$%.2f"
            ),
            "con_factura": "Con factura",
            "Cantidad": st.column_config.NumberColumn(
                min_value=0.0, step=0.5, format="%.2f"
            ),
        },
        disabled=[
            "Concepto",
            "tipo_insumo",
            "unidad",
            "costo_sin_iva",
            "con_factura",
        ],
        key=f"apu_ins_{uid}_{ss['ver']}",
    ).copy()
    df_edit_p["Cantidad"] = df_edit_p["Cantidad"].fillna(0.0).astype(float)
    df_edit_p["Importe"] = df_edit_p["costo_sin_iva"] * df_edit_p["Cantidad"]
    p_cur["insumos_df"] = df_edit_p

    col_mo, col_calc = st.columns(2)
    with col_mo:
      st.markdown("**Mano de obra de la partida**")
      esq_sel = st.selectbox(
          "Esquema de mano de obra",
          NOMBRES_ESQUEMA,
          index=NOMBRES_ESQUEMA.index(p_cur["esquema"]),
          help="Elige quiénes participan en esta partida para calcular su mano de obra directa.",
          key=f"p_esq_{uid}_{ss['ver']}",
      )
      p_cur["esquema"] = esq_sel
      e_idx_p = NOMBRES_ESQUEMA.index(esq_sel)
      personas_p = ESQUEMAS[esq_sel]
      tarifas_in, horas_in = [], []
      for j, (nombre_per, _, ayuda_per) in enumerate(personas_p):
        t_key = f"mo_t_{e_idx_p}_{j}"
        h_key = f"mo_h_{e_idx_p}_{j}"
        t_val = st.number_input(
            f"Tarifa {nombre_per} ($/hr)",
            min_value=0.0,
            step=10.0,
            value=float(p_cur.get(t_key, 300.0)),
            help=ayuda_per,
            key=f"ui_{t_key}_{uid}_{ss['ver']}",
        )
        h_val = st.number_input(
            f"Horas {nombre_per}",
            min_value=0.0,
            step=0.5,
            value=float(p_cur.get(h_key, 4.0)),
            help=f"Horas estimadas de {nombre_per} para esta partida.",
            key=f"ui_{h_key}_{uid}_{ss['ver']}",
        )
        p_cur[t_key] = float(t_val)
        p_cur[h_key] = float(h_val)
        tarifas_in.append(float(t_val))
        horas_in.append(float(h_val))

      ops_urg = sorted(set(FACTORES_URGENCIA + [float(p_cur["factor_urg"])]))
      p_cur["factor_urg"] = st.selectbox(
          "Factor urgencia / prioridad",
          ops_urg,
          index=ops_urg.index(float(p_cur["factor_urg"])),
          help="Multiplicador sobre la mano de obra por urgencia o atención fuera de horario.",
          key=f"p_urg_{uid}_{ss['ver']}",
      )

      div_p = False
      pct1_p = 100
      if len(personas_p) == 2:
        n1, n2 = personas_p[0][0], personas_p[1][0]
        div_p = st.checkbox(
            f"Dividir ganancia libre de la partida entre {n1} y {n2}",
            value=bool(p_cur["dividir"]),
            help="Reparte la ganancia libre real de esta partida entre los dos participantes.",
            key=f"p_div_{uid}_{ss['ver']}",
        )
        p_cur["dividir"] = div_p
        if div_p:
          pct1_p = st.slider(
              f"Porcentaje para {n1} (%)",
              0,
              100,
              value=_clamp(p_cur["pct_ing_1"], 0, 100),
              help=f"El resto va a {n2}.",
              key=f"p_pct1_{uid}_{ss['ver']}",
          )
          p_cur["pct_ing_1"] = int(pct1_p)

    with col_calc:
      st.markdown("**Indirectos, margen y SAT de la partida**")
      p_cur["pct_herr"] = st.slider(
          "Herramienta menor (% de MO)",
          0,
          10,
          value=_clamp(p_cur["pct_herr"], 0, 10),
          help="Desgaste de herramienta menor, calculado sobre la mano de obra.",
          key=f"p_herr_{uid}_{ss['ver']}",
      )
      p_cur["pct_ind"] = st.slider(
          "Indirectos (%)",
          0,
          30,
          value=_clamp(p_cur["pct_ind"], 0, 30),
          help="Gastos operativos generales, aplicados sobre el Costo Directo.",
          key=f"p_ind_{uid}_{ss['ver']}",
      )
      p_cur["pct_gar"] = st.slider(
          "Garantía / contingencia (%)",
          0,
          15,
          value=_clamp(p_cur["pct_gar"], 0, 15),
          help="Colchón por revisiones o imprevistos, sobre Costo Directo + Indirectos.",
          key=f"p_gar_{uid}_{ss['ver']}",
      )
      p_cur["pct_uti"] = st.slider(
          "Utilidad (%)",
          5,
          60,
          value=_clamp(p_cur["pct_uti"], 5, 60),
          help=(
              "Se aplica sobre Costo Directo + Indirectos + Garantía (markup, no margen):"
              " 25% de utilidad equivale a ~20% de margen sobre el precio de venta."
          ),
          key=f"p_uti_{uid}_{ss['ver']}",
      )
      p_cur["regimen"] = st.radio(
          "Régimen",
          REGIMENES,
          index=REGIMENES.index(p_cur["regimen"]),
          help="Estimación orientativa de ISR; confírmala con tu contador (retenciones a personas morales, etc.).",
          key=f"p_reg_{uid}_{ss['ver']}",
      )

    # Recalcular partida activa con los valores recién editados
    res_act = calcular_partida(
        df_sel=df_edit_p,
        cantidad_partida=float(p_cur["cantidad"]),
        costo_mo_base=sum(t * h for t, h in zip(tarifas_in, horas_in)),
        factor_urg=float(p_cur["factor_urg"]),
        pct_herr=float(p_cur["pct_herr"]) / 100.0,
        pct_ind=float(p_cur["pct_ind"]) / 100.0,
        pct_gar=float(p_cur["pct_gar"]) / 100.0,
        pct_uti=float(p_cur["pct_uti"]) / 100.0,
        regimen=p_cur["regimen"],
    )
    partidas_resumen[ss["partida_activa"]]["nombre"] = p_cur["nombre"]
    partidas_resumen[ss["partida_activa"]]["unidad"] = p_cur["unidad"]
    partidas_resumen[ss["partida_activa"]]["cantidad"] = float(
        p_cur["cantidad"]
    )
    partidas_resumen[ss["partida_activa"]]["df_sel"] = df_edit_p
    partidas_resumen[ss["partida_activa"]]["res"] = res_act
    partidas_resumen[ss["partida_activa"]]["parametros_partida"] = {
        "version": 5,
        "esquema": esq_sel,
        "tarifas": tarifas_in,
        "horas": horas_in,
        "factor_urg": float(p_cur["factor_urg"]),
        "pct_herr": int(p_cur["pct_herr"]),
        "pct_ind": int(p_cur["pct_ind"]),
        "pct_gar": int(p_cur["pct_gar"]),
        "pct_uti": int(p_cur["pct_uti"]),
        "regimen": p_cur["regimen"],
        "dividir": bool(p_cur["dividir"]),
        "pct_ing_1": int(p_cur["pct_ing_1"]),
    }

    df_explosion = construir_explosion(partidas_resumen)
    tot_cd = sum(x["res"]["cd"] for x in partidas_resumen)
    tot_ci = sum(x["res"]["ci"] for x in partidas_resumen)
    tot_gar = sum(x["res"]["gar"] for x in partidas_resumen)
    tot_uti = sum(x["res"]["uti"] for x in partidas_resumen)
    tot_sub = sum(x["res"]["subtotal"] for x in partidas_resumen)
    tot_iva = sum(x["res"]["iva_tras"] for x in partidas_resumen)
    tot_fac = sum(x["res"]["total"] for x in partidas_resumen)
    tot_iva_pag = sum(x["res"]["iva_pagar"] for x in partidas_resumen)
    tot_isr = sum(x["res"]["isr"] for x in partidas_resumen)
    tot_gan = sum(x["res"]["ganancia"] for x in partidas_resumen)
    tot_mo = sum(x["res"]["costo_mo"] for x in partidas_resumen)
    tot_gan_sin_mo = sum(x["res"]["ganancia_sin_mo"] for x in partidas_resumen)

    st.caption(
        f"**APU Partida {ss['partida_activa'] + 1}:** P.U. s/IVA ="
        f" **${res_act['pu_sin_iva']:,.2f}** · Importe partida ="
        f" **${res_act['subtotal']:,.2f}** · Ganancia partida ="
        f" **${res_act['ganancia']:,.2f}**"
    )

    st.divider()
    st.markdown("### Totales del presupuesto completo")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Costo Directo Total", f"${tot_cd:,.2f}")
    m2.metric("Subtotal s/IVA", f"${tot_sub:,.2f}")
    m3.metric("Total c/IVA", f"${tot_fac:,.2f}")
    m4.metric("Ganancia Libre Total", f"${tot_gan:,.2f}")
    st.caption(
        f"Mano de obra acumulada: **${tot_mo:,.2f}** · Ganancia libre sin"
        f" contar tu MO como sueldo: **${tot_gan_sin_mo:,.2f}**."
    )

    if p_cur["dividir"] and len(personas_p) == 2:
      p1_val = max(0.0, res_act["ganancia"] * int(p_cur["pct_ing_1"]) / 100.0)
      p2_val = max(
          0.0, res_act["ganancia"] * (100 - int(p_cur["pct_ing_1"])) / 100.0
      )
      r1, r2 = st.columns(2)
      r1.info(
          f"{personas_p[0][0]} ({p_cur['pct_ing_1']}%) en Partida"
          f" {ss['partida_activa'] + 1}: **${p1_val:,.2f} MXN**"
      )
      r2.info(
          f"{personas_p[1][0]} ({100 - int(p_cur['pct_ing_1'])}%) en Partida"
          f" {ss['partida_activa'] + 1}: **${p2_val:,.2f} MXN**"
      )

    st.checkbox("Actualizar si el folio ya existe", key="sobrescribir")

    r2f = lambda x: round(float(x), 2)
    resumen_text = " | ".join(
        f"{x['nombre']} ({x['cantidad']:g} {x['unidad']})"
        for x in partidas_resumen
    )
    datos_totales = {
        "folio": ss["folio"].strip(),
        "cliente": ss["cliente"].strip() or "Cliente Residencial",
        "ubicacion": ss["ubicacion"].strip() or "Colima",
        "descripcion_servicio": resumen_text,
        "factor_urgencia": r2f(p_cur["factor_urg"]),
        "costo_directo": r2f(tot_cd),
        "indirectos_monto": r2f(tot_ci),
        "garantia_monto": r2f(tot_gar),
        "utilidad_monto": r2f(tot_uti),
        "subtotal": r2f(tot_sub),
        "iva_trasladado": r2f(tot_iva),
        "total_facturado": r2f(tot_fac),
        "iva_por_pagar": r2f(tot_iva_pag),
        "isr_estimado": r2f(tot_isr),
        "ganancia_neta_libre": r2f(tot_gan),
    }
    nombre_archivo = datos_totales["folio"] or "cotizacion_multipartida"

    b1, b2, b3 = st.columns(3)
    with b1:
      if st.button("📌 Guardar Cotización", use_container_width=True):
        cliente_val = ss["cliente"].strip()
        if not cliente_val:
          st.error("Captura el nombre del cliente.")
        elif tot_cd <= 0:
          st.error("El costo directo total es cero.")
        else:
          try:
            folio_final = guardar_cotizacion_multipartida(
                {**datos_totales, "cliente": cliente_val},
                partidas_resumen,
                {
                    "version": 5,
                    "num_partidas": len(partidas_resumen),
                    "ganancia_sin_mo": r2f(tot_gan_sin_mo),
                },
                ss["sobrescribir"],
            )
            ss["folio"] = folio_final
            st.success(f"Cotización {folio_final} guardada con partidas.")
          except errors.UniqueViolation:
            st.error(
                f"Ya existe el folio {datos_totales['folio']}. Borra el folio"
                " para uno nuevo o marca «Actualizar si el folio ya existe»."
            )
          except errors.UndefinedTable:
            st.error(
                f"No existe el folio {datos_totales['folio']} para actualizar."
            )
          except psycopg2.Error as exc:
            st.error(f"Error de base de datos: {exc.__class__.__name__}")

    with b2:
      st.download_button(
          label="📊 Descargar Excel (.xlsx)",
          data=generar_excel_cotizacion(
              datos_totales, partidas_resumen, df_explosion
          ),
          file_name=f"{nombre_archivo}.xlsx",
          mime=(
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          ),
          use_container_width=True,
      )

    with b3:
      st.download_button(
          label="📄 Descargar PDF (.pdf)",
          data=generar_pdf_cotizacion(
              datos_totales, partidas_resumen, df_explosion
          ),
          file_name=f"{nombre_archivo}.pdf",
          mime="application/pdf",
          use_container_width=True,
      )

with tab3:
  st.subheader("Explosión de Insumos del Presupuesto")
  st.caption(
      "Consolidado total sumando todas las partidas y multiplicando por la"
      " cantidad de cada partida."
  )
  if df_explosion.empty:
    st.info("No hay insumos con cantidad mayor a 0 en ninguna partida.")
  else:
    tabla(
        st.dataframe,
        df_explosion,
        column_config={
            "Cantidad Total": st.column_config.NumberColumn(format="%.2f"),
            "P.U. s/IVA": st.column_config.NumberColumn(format="$%.2f"),
            "Importe Total": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    e1, e2, e3 = st.columns(3)
    e1.metric(
        "Total Materiales",
        f"${df_explosion.loc[df_explosion['Tipo'] == 'Material', 'Importe Total'].sum():,.2f}",
    )
    e2.metric(
        "Total Equipo / Herramienta",
        f"${df_explosion.loc[df_explosion['Tipo'] != 'Material', 'Importe Total'].sum():,.2f}",
    )
    e3.metric(
        "Importe Insumos s/IVA",
        f"${df_explosion['Importe Total'].sum():,.2f}",
    )

with tab4:
  st.subheader("Cotizaciones guardadas")
  df_hist = cargar_historial()
  if df_hist.empty:
    st.info("Aún no hay cotizaciones guardadas.")
  else:
    tabla(
        st.dataframe,
        df_hist,
        column_config={
            "total_facturado": st.column_config.NumberColumn(
                "Total c/IVA", format="$%.2f"
            ),
            "ganancia_neta_libre": st.column_config.NumberColumn(
                "Ganancia libre", format="$%.2f"
            ),
            "fecha_creacion": "Fecha (hora de México)",
        },
    )
    sel_folio = st.selectbox("Abrir cotización", df_hist["folio"].tolist())
    st.button("📂 Cargar en el formulario", on_click=cb_cargar, args=(sel_folio,))
    msg = ss.get("msg_historial")
    if msg:
      getattr(st, msg[0])(msg[1])
  if st.button("🔄 Actualizar historial"):
    cargar_historial.clear()
    st.rerun()
