import hmac
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st
from psycopg2 import errors
from psycopg2.extras import Json, RealDictCursor

# ----------------------------------------------------------------------------
# Configuración y constantes
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Cotizador PU", layout="wide")

TZ = ZoneInfo("America/Mexico_City")
IVA = 0.16
ISR_RESICO = 0.01
ISR_ACT_EMP = 0.20
MAX_FALLOS_LOGIN = 5
BLOQUEO_SEG = 300

FACTORES_URGENCIA = [1.0, 1.15, 1.25, 1.5, 2.0]
REGIMENES = ["RESICO (1% ISR)", "Actividad Empresarial (~20% s/util)"]

# esquema -> [(nombre, tarifa por defecto, ayuda)]
ESQUEMAS = {
    "2 Ingenieros": [
        ("Ing. 1", 300.0, "Costo por hora del primer ingeniero (diagnóstico, pruebas y ejecución)."),
        ("Ing. 2", 300.0, "Costo por hora del segundo ingeniero (ejecución, maniobras y pruebas)."),
    ],
    "Especialista + Ayudante": [
        ("Especialista", 300.0, "Costo por hora del especialista (diagnóstico, pruebas y puesta en marcha)."),
        ("Ayudante", 180.0, "Costo por hora del apoyo técnico (maniobras, cableado, soporte en campo)."),
    ],
    "1 Ingeniero": [
        ("Ingeniero", 300.0, "Costo por hora del ingeniero que ejecuta el servicio."),
    ],
}
NOMBRES_ESQUEMA = list(ESQUEMAS)

COLS_CAT = ["id", "concepto", "unidad", "costo_sin_iva", "cantidad_default", "con_factura"]
COLS_SEL = ["Concepto", "unidad", "costo_sin_iva", "con_factura", "Cantidad"]

DESC_DEFAULT = (
    "Servicio de mantenimiento correctivo en polipasto monofásico: diagnóstico,"
    " habilitación provisional y montaje definitivo de banco capacitivo"
    " 150 µF / 400 VAC."
)


def _defaults() -> dict:
    d = {
        "folio": "",
        "cliente": "Cliente Residencial",
        "ubicacion": "Colima",
        "desc": DESC_DEFAULT,
        "esquema": NOMBRES_ESQUEMA[0],
        "factor_urg": 1.25,
        "dividir": True,
        "pct_ing_1": 50,
        "pct_herr": 3,
        "pct_ind": 12,
        "pct_gar": 5,
        "pct_uti": 25,
        "regimen": REGIMENES[0],
        "sobrescribir": False,
        "base_df": None,   # None = usar catálogo; DataFrame = cotización cargada
        "ver": 0,          # cambia para reiniciar el editor de insumos
        "cot_cargada": None,
    }
    for e, nombre in enumerate(NOMBRES_ESQUEMA):
        for i, (_, tarifa, _) in enumerate(ESQUEMAS[nombre]):
            d[f"t_{e}_{i}"] = tarifa
            d[f"h_{e}_{i}"] = 4.0
    return d


# ----------------------------------------------------------------------------
# Seguridad: acceso con contraseña (falla cerrado si no hay APP_PASSWORD)
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
        st.error("Falta configurar APP_PASSWORD en los secrets. La app queda bloqueada.")
        st.stop()

    estado = _estado_login()
    st.title("🔒 Cotizador PU")
    restante = estado["bloqueado_hasta"] - time.time()
    if restante > 0:
        st.error(f"Demasiados intentos fallidos. Intenta de nuevo en {int(restante // 60) + 1} min.")
        st.stop()

    with st.form("login"):
        pwd = st.text_input("Contraseña", type="password")
        entrar = st.form_submit_button("Entrar")

    if entrar:
        if hmac.compare_digest(pwd.encode("utf-8"), str(esperado).encode("utf-8")):
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
st.title("⚡ Cotizador PU")
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
    """Abre, hace commit/rollback y SIEMPRE cierra la conexión."""
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
    unidad VARCHAR(20) NOT NULL DEFAULT 'Pza',
    costo_sin_iva NUMERIC(12, 2) NOT NULL DEFAULT 0,
    cantidad_default NUMERIC(10, 2) NOT NULL DEFAULT 1.00,
    con_factura BOOLEAN NOT NULL DEFAULT TRUE,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE catalogo_insumos
    ADD COLUMN IF NOT EXISTS cantidad_default NUMERIC(10, 2) NOT NULL DEFAULT 1.00;

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
    fecha_creacion TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cotizacion_insumos (
    id SERIAL PRIMARY KEY,
    folio VARCHAR(30) REFERENCES cotizaciones(folio) ON DELETE CASCADE,
    concepto VARCHAR(150) NOT NULL,
    cantidad NUMERIC(10, 2) NOT NULL,
    costo_unitario_sin_iva NUMERIC(12, 2) NOT NULL,
    importe NUMERIC(12, 2) NOT NULL
);

ALTER TABLE cotizaciones ADD COLUMN IF NOT EXISTS parametros JSONB;
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
CREATE INDEX IF NOT EXISTS idx_cotins_folio ON cotizacion_insumos (folio);
"""


@st.cache_resource(show_spinner=False)
def init_db() -> bool:
    """Corre la migración una sola vez por proceso (no en cada rerun)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRACION_SQL)
    return True


init_db()


def _txt(v, default: str = "") -> str:
    return default if v is None or pd.isna(v) else str(v).strip()


def _num(v, default: float) -> float:
    return default if v is None or pd.isna(v) else float(v)


# ---- Catálogo --------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def cargar_catalogo() -> pd.DataFrame:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, concepto, unidad, costo_sin_iva::float AS costo_sin_iva,"
                " cantidad_default::float AS cantidad_default, con_factura"
                " FROM catalogo_insumos WHERE activo = TRUE ORDER BY id"
            )
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=COLS_CAT)
    df["id"] = df["id"].astype("Int64")
    df["costo_sin_iva"] = df["costo_sin_iva"].astype(float)
    df["cantidad_default"] = df["cantidad_default"].astype(float)
    df["con_factura"] = df["con_factura"].astype(bool)
    return df


def guardar_catalogo(df: pd.DataFrame) -> list[str]:
    """Valida y guarda. Devuelve lista de errores (vacía si todo salió bien)."""
    errores, filas = [], []
    for pos, row in df.iterrows():
        n = pos + 1 if isinstance(pos, int) else pos
        concepto = _txt(row.get("concepto"))
        es_nueva = pd.isna(row.get("id"))
        if not concepto:
            if not es_nueva:
                errores.append(f"Fila {n}: el concepto no puede quedar vacío.")
            continue  # fila nueva vacía: se ignora
        if len(concepto) > 150:
            errores.append(f"Fila {n}: el concepto excede 150 caracteres.")
        unidad = _txt(row.get("unidad"), "Pza") or "Pza"
        if len(unidad) > 20:
            errores.append(f"Fila {n}: la unidad excede 20 caracteres.")
        costo = _num(row.get("costo_sin_iva"), 0.0)
        cant = _num(row.get("cantidad_default"), 1.0)
        if costo < 0 or cant < 0:
            errores.append(f"Fila {n}: costo y cantidad no pueden ser negativos.")
        if costo >= 1e10 or cant >= 1e8:
            errores.append(f"Fila {n}: costo o cantidad demasiado grandes.")
        con_fac = True if pd.isna(row.get("con_factura")) else bool(row["con_factura"])
        filas.append(
            (None if es_nueva else int(row["id"]), concepto, unidad, round(costo, 2), round(cant, 2), con_fac)
        )
    if errores:
        return errores

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM catalogo_insumos WHERE activo = TRUE")
            existentes = {r[0] for r in cur.fetchall()}
            conservados = {f[0] for f in filas if f[0] is not None}
            # Filas borradas en el editor -> baja lógica (no rompe cotizaciones viejas)
            a_desactivar = list(existentes - conservados)
            if a_desactivar:
                cur.execute(
                    "UPDATE catalogo_insumos SET activo = FALSE WHERE id = ANY(%s)",
                    (a_desactivar,),
                )
            for id_, concepto, unidad, costo, cant, con_fac in filas:
                if id_ is None:
                    cur.execute(
                        "INSERT INTO catalogo_insumos"
                        " (concepto, unidad, costo_sin_iva, cantidad_default, con_factura)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (concepto, unidad, costo, cant, con_fac),
                    )
                else:
                    cur.execute(
                        "UPDATE catalogo_insumos SET concepto=%s, unidad=%s,"
                        " costo_sin_iva=%s, cantidad_default=%s, con_factura=%s"
                        " WHERE id=%s",
                        (concepto, unidad, costo, cant, con_fac, id_),
                    )
    cargar_catalogo.clear()
    return []


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


def guardar_cotizacion(datos: dict, df_detalle: pd.DataFrame, parametros: dict, sobrescribir: bool) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if not datos["folio"]:
                datos["folio"] = siguiente_folio(cur)
            elif sobrescribir:
                cur.execute("DELETE FROM cotizaciones WHERE folio = %s", (datos["folio"],))

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
                {**datos, "parametros": Json(parametros)},
            )
            usados = df_detalle[df_detalle["Cantidad"] > 0]
            cur.executemany(
                "INSERT INTO cotizacion_insumos"
                " (folio, concepto, unidad, cantidad, costo_unitario_sin_iva, con_factura, importe)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        datos["folio"],
                        str(it["Concepto"]),
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
    return datos["folio"]


@st.cache_data(ttl=60, show_spinner=False)
def cargar_historial() -> pd.DataFrame:
    cols = ["folio", "cliente", "ubicacion", "total_facturado", "ganancia_neta_libre", "fecha_creacion"]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT folio, cliente, ubicacion, total_facturado::float AS total_facturado,"
                " ganancia_neta_libre::float AS ganancia_neta_libre, fecha_creacion"
                " FROM cotizaciones ORDER BY fecha_creacion DESC"
            )
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["fecha_creacion"] = (
            pd.to_datetime(df["fecha_creacion"], utc=True).dt.tz_convert(TZ).dt.strftime("%Y-%m-%d %H:%M")
        )
    return df


def obtener_cotizacion(folio: str):
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
                "SELECT concepto, unidad, cantidad::float AS cantidad,"
                " costo_unitario_sin_iva::float AS costo_sin_iva, con_factura"
                " FROM cotizacion_insumos WHERE folio = %s ORDER BY id",
                (folio,),
            )
            insumos = cur.fetchall()
    return cot, insumos


# ----------------------------------------------------------------------------
# Utilidades de interfaz
# ----------------------------------------------------------------------------
def tabla(fn, *args, **kwargs):
    """Ancho completo compatible con versiones nuevas y viejas de Streamlit."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except (TypeError, st.errors.StreamlitAPIException):
        return fn(*args, use_container_width=True, **kwargs)


def base_desde_catalogo(cat: pd.DataFrame) -> pd.DataFrame:
    df = cat.rename(columns={"concepto": "Concepto"}).copy()
    df["Cantidad"] = df["cantidad_default"].fillna(0.0)
    return df[COLS_SEL].reset_index(drop=True)


def _clamp(v, lo, hi):
    return int(max(lo, min(hi, round(float(v)))))


def cb_cargar(folio: str):
    """Callback: lleva una cotización guardada al formulario."""
    ss = st.session_state
    cot, insumos = obtener_cotizacion(folio)
    if not cot:
        ss["msg_historial"] = ("error", f"No se encontró la cotización {folio}.")
        return
    p = cot["parametros"] or {}
    ss["folio"] = cot["folio"]
    ss["cliente"] = cot["cliente"]
    ss["ubicacion"] = cot["ubicacion"] or ""
    ss["desc"] = cot["descripcion_servicio"] or ""
    ss["factor_urg"] = float(cot["factor_urgencia"] or 1.0)
    ss["sobrescribir"] = False

    if p:
        esq = p.get("esquema", NOMBRES_ESQUEMA[0])
        if esq not in ESQUEMAS:
            esq = NOMBRES_ESQUEMA[0]
        ss["esquema"] = esq
        e = NOMBRES_ESQUEMA.index(esq)
        for i, (t, h) in enumerate(zip(p.get("tarifas", []), p.get("horas", []))):
            if f"t_{e}_{i}" in ss:
                ss[f"t_{e}_{i}"] = float(t)
                ss[f"h_{e}_{i}"] = float(h)
        ss["pct_herr"] = _clamp(p.get("pct_herr", 3), 0, 10)
        ss["pct_ind"] = _clamp(p.get("pct_ind", 12), 0, 30)
        ss["pct_gar"] = _clamp(p.get("pct_gar", 5), 0, 15)
        ss["pct_uti"] = _clamp(p.get("pct_uti", 25), 5, 60)
        ss["regimen"] = p.get("regimen") if p.get("regimen") in REGIMENES else REGIMENES[0]
        ss["dividir"] = bool(p.get("dividir", True))
        ss["pct_ing_1"] = _clamp(p.get("pct_ing_1", 50), 0, 100)
        aviso = None
    else:
        aviso = (
            "Esta cotización es anterior a la v4 y no guardó sus parámetros"
            " (tarifas, horas, porcentajes). Se cargaron los insumos y los"
            " parámetros quedaron en valores por defecto: ajústalos antes de"
            " volver a guardarla."
        )

    cat = cargar_catalogo()
    filas = [
        {
            "Concepto": i["concepto"],
            "unidad": i["unidad"],
            "costo_sin_iva": i["costo_sin_iva"],
            "con_factura": i["con_factura"],
            "Cantidad": i["cantidad"],
        }
        for i in insumos
    ]
    usados = {i["concepto"] for i in insumos}
    for _, r in cat.iterrows():
        if r["concepto"] not in usados:
            filas.append(
                {
                    "Concepto": r["concepto"],
                    "unidad": r["unidad"],
                    "costo_sin_iva": r["costo_sin_iva"],
                    "con_factura": r["con_factura"],
                    "Cantidad": 0.0,
                }
            )
    ss["base_df"] = pd.DataFrame(filas, columns=COLS_SEL)
    ss["ver"] += 1
    ss["cot_cargada"] = cot["folio"]
    ss["msg_historial"] = ("warning", aviso) if aviso else ("success", f"Cotización {folio} cargada. Ve a la pestaña «2. Nueva Cotización».")


def cb_nueva():
    ss = st.session_state
    ver = ss["ver"] + 1
    for k, v in _defaults().items():
        ss[k] = v
    ss["ver"] = ver


# ----------------------------------------------------------------------------
# Cálculo (mismo esquema que la v3, aislado para poder revisarlo)
# ----------------------------------------------------------------------------
def calcular(df_sel, costo_mo_base, factor_urg, pct_herr, pct_ind, pct_gar, pct_uti, regimen) -> dict:
    costo_mat = df_sel["Importe"].sum()
    con_fac = df_sel.loc[df_sel["con_factura"], "Importe"].sum()
    sin_fac = df_sel.loc[~df_sel["con_factura"], "Importe"].sum()
    iva_acreditable = con_fac * IVA

    costo_mo = costo_mo_base * factor_urg
    herr = costo_mo * pct_herr
    cd = costo_mat + costo_mo + herr
    ci = cd * pct_ind
    gar = (cd + ci) * pct_gar
    uti = (cd + ci + gar) * pct_uti
    subtotal = cd + ci + gar + uti
    iva_tras = subtotal * IVA
    total = subtotal + iva_tras

    iva_pagar = max(0.0, iva_tras - iva_acreditable)
    isr = subtotal * ISR_RESICO if "RESICO" in regimen else max(0.0, uti * ISR_ACT_EMP)
    desembolso = con_fac * (1 + IVA) + sin_fac
    ganancia = total - desembolso - iva_pagar - isr
    return {
        "costo_mat": costo_mat,
        "costo_mo": costo_mo,
        "herr": herr,
        "cd": cd,
        "ci": ci,
        "gar": gar,
        "uti": uti,
        "subtotal": subtotal,
        "iva_tras": iva_tras,
        "total": total,
        "iva_pagar": iva_pagar,
        "isr": isr,
        "ganancia": ganancia,
        "ganancia_sin_mo": ganancia - costo_mo,
    }


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------
ss = st.session_state
tab1, tab2, tab3 = st.tabs(["1. Catálogo", "2. Nueva Cotización", "3. Historial"])

# ---- Tab 1: Catálogo -------------------------------------------------------
with tab1:
    st.subheader("Catálogo")
    st.caption("Para eliminar un insumo, selecciona la fila y presiona Supr. Las cotizaciones anteriores conservan su copia del precio.")
    df_cat = cargar_catalogo()
    df_editado = tabla(
        st.data_editor,
        df_cat,
        num_rows="dynamic",
        column_config={
            "id": st.column_config.NumberColumn("id", disabled=True),
            "concepto": st.column_config.TextColumn("Concepto", max_chars=150),
            "unidad": st.column_config.TextColumn("Unidad", max_chars=20, default="Pza"),
            "costo_sin_iva": st.column_config.NumberColumn("Costo s/IVA", min_value=0.0, format="$%.2f", default=0.0),
            "cantidad_default": st.column_config.NumberColumn(
                "Cantidad", min_value=0.0, step=0.5, format="%.2f", default=1.0
            ),
            "con_factura": st.column_config.CheckboxColumn("Con factura", default=True),
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

# ---- Tab 2: Nueva cotización -----------------------------------------------
with tab2:
    df_cat2 = cargar_catalogo()
    if df_cat2.empty and ss["base_df"] is None:
        st.warning("Primero agrega insumos en la pestaña «Catálogo».")
    else:
        if ss["cot_cargada"]:
            c_a, c_b = st.columns([4, 1])
            c_a.info(
                f"Editando la cotización cargada **{ss['cot_cargada']}**. Para reemplazarla marca"
                " «Actualizar si el folio ya existe»; para crear una nueva borra el folio."
            )
            c_b.button("➕ Nueva cotización", on_click=cb_nueva)

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

        base = ss["base_df"].copy() if ss["base_df"] is not None else base_desde_catalogo(df_cat2)
        seleccion = tabla(
            st.data_editor,
            base,
            column_config={
                "unidad": "Unidad",
                "costo_sin_iva": st.column_config.NumberColumn("Costo s/IVA", format="$%.2f"),
                "con_factura": "Con factura",
                "Cantidad": st.column_config.NumberColumn(min_value=0.0, step=0.5, format="%.2f"),
            },
            disabled=["Concepto", "unidad", "costo_sin_iva", "con_factura"],
            key=f"sel_{ss['ver']}",
        ).copy()
        seleccion["Cantidad"] = seleccion["Cantidad"].fillna(0.0).astype(float)
        seleccion["costo_sin_iva"] = seleccion["costo_sin_iva"].astype(float)
        seleccion["con_factura"] = seleccion["con_factura"].fillna(True).astype(bool)
        seleccion["Importe"] = seleccion["costo_sin_iva"] * seleccion["Cantidad"]

        col_mo, col_calc = st.columns(2)
        with col_mo:
            st.markdown("**Mano de obra**")
            esquema = st.selectbox(
                "Esquema de mano de obra",
                NOMBRES_ESQUEMA,
                key="esquema",
                help="Elige quiénes participaron para calcular el costo directo de mano de obra.",
            )
            e_idx = NOMBRES_ESQUEMA.index(esquema)
            personas = ESQUEMAS[esquema]
            tarifas, horas = [], []
            for i, (nombre, _, ayuda) in enumerate(personas):
                tarifas.append(
                    st.number_input(f"Tarifa {nombre} ($/hr)", min_value=0.0, step=10.0, key=f"t_{e_idx}_{i}", help=ayuda)
                )
                horas.append(
                    st.number_input(f"Horas {nombre}", min_value=0.0, step=0.5, key=f"h_{e_idx}_{i}", help=f"Horas estimadas de {nombre}.")
                )
            costo_mo_base = sum(t * h for t, h in zip(tarifas, horas))

            opciones = sorted(set(FACTORES_URGENCIA + [float(ss["factor_urg"])]))
            factor_urg = st.selectbox(
                "Factor urgencia / prioridad",
                opciones,
                key="factor_urg",
                help="Multiplicador sobre la mano de obra por urgencia o atención fuera de horario.",
            )

            dividir = False
            pct_1 = 100
            if len(personas) == 2:
                n1, n2 = personas[0][0], personas[1][0]
                dividir = st.checkbox(
                    f"Dividir la ganancia libre entre {n1} y {n2}",
                    key="dividir",
                    help="Reparte la ganancia libre real entre los dos participantes.",
                )
                if dividir:
                    pct_1 = st.slider(
                        f"Porcentaje de ganancia para {n1} (%)",
                        0,
                        100,
                        key="pct_ing_1",
                        help=f"El resto va a {n2}.",
                    )
            pct_2 = 100 - pct_1

        with col_calc:
            st.markdown("**Indirectos, margen y SAT**")
            pct_herr = st.slider(
                "Herramienta menor (% de la mano de obra)", 0, 10, key="pct_herr",
                help="Desgaste de herramienta menor, calculado sobre la mano de obra.",
            ) / 100
            pct_ind = st.slider(
                "Indirectos (%)", 0, 30, key="pct_ind",
                help="Gastos operativos generales, aplicados sobre el Costo Directo.",
            ) / 100
            pct_gar = st.slider(
                "Garantía / contingencia (%)", 0, 15, key="pct_gar",
                help="Colchón por revisiones o imprevistos, sobre Costo Directo + Indirectos.",
            ) / 100
            pct_uti = st.slider(
                "Utilidad (%)", 5, 60, key="pct_uti",
                help=(
                    "Se aplica sobre Costo Directo + Indirectos + Garantía (markup, no margen):"
                    " 25% de utilidad equivale a ~20% de margen sobre el precio de venta."
                ),
            ) / 100
            regimen = st.radio(
                "Régimen", REGIMENES, key="regimen",
                help="Estimación orientativa de ISR; confírmala con tu contador (retenciones a personas morales, etc.).",
            )

        st.text_area("Concepto para cotización / factura", key="desc")

        r = calcular(seleccion, costo_mo_base, factor_urg, pct_herr, pct_ind, pct_gar, pct_uti, regimen)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Costo Directo", f"${r['cd']:,.2f}")
        m2.metric("Subtotal s/IVA", f"${r['subtotal']:,.2f}")
        m3.metric("Total c/IVA", f"${r['total']:,.2f}")
        m4.metric("Ganancia libre (incluye tu MO)", f"${r['ganancia']:,.2f}")
        st.caption(
            f"Si pagas la mano de obra (${r['costo_mo']:,.2f}) como sueldo, lo que queda libre es"
            f" **${r['ganancia_sin_mo']:,.2f}**."
        )

        if r["ganancia"] < 0:
            st.error("La ganancia libre es negativa: esta cotización genera pérdida. Revisa utilidad, indirectos o costos.")
        elif r["ganancia_sin_mo"] < 0:
            st.warning("Después de pagar la mano de obra, la ganancia libre queda en negativo.")

        if dividir:
            p1 = max(0.0, r["ganancia"] * pct_1 / 100)
            p2 = max(0.0, r["ganancia"] * pct_2 / 100)
            r1, r2 = st.columns(2)
            r1.info(f"{personas[0][0]} ({pct_1}%): **${p1:,.2f} MXN**")
            r2.info(f"{personas[1][0]} ({pct_2}%): **${p2:,.2f} MXN**")

        st.checkbox("Actualizar si el folio ya existe", key="sobrescribir")

        if st.button("📌 Guardar Cotización"):
            folio = ss["folio"].strip()
            cliente = ss["cliente"].strip()
            if not cliente:
                st.error("Captura el nombre del cliente.")
            elif r["cd"] <= 0:
                st.error("El costo directo es cero: agrega cantidades o mano de obra.")
            else:
                r2f = lambda x: round(float(x), 2)
                datos = {
                    "folio": folio,
                    "cliente": cliente,
                    "ubicacion": ss["ubicacion"].strip(),
                    "descripcion_servicio": ss["desc"],
                    "factor_urgencia": r2f(factor_urg),
                    "costo_directo": r2f(r["cd"]),
                    "indirectos_monto": r2f(r["ci"]),
                    "garantia_monto": r2f(r["gar"]),
                    "utilidad_monto": r2f(r["uti"]),
                    "subtotal": r2f(r["subtotal"]),
                    "iva_trasladado": r2f(r["iva_tras"]),
                    "total_facturado": r2f(r["total"]),
                    "iva_por_pagar": r2f(r["iva_pagar"]),
                    "isr_estimado": r2f(r["isr"]),
                    "ganancia_neta_libre": r2f(r["ganancia"]),
                }
                parametros = {
                    "version": 4,
                    "esquema": esquema,
                    "tarifas": tarifas,
                    "horas": horas,
                    "factor_urg": factor_urg,
                    "pct_herr": round(pct_herr * 100, 2),
                    "pct_ind": round(pct_ind * 100, 2),
                    "pct_gar": round(pct_gar * 100, 2),
                    "pct_uti": round(pct_uti * 100, 2),
                    "regimen": regimen,
                    "dividir": dividir,
                    "pct_ing_1": pct_1,
                    "iva": IVA,
                    "costo_mo": r2f(r["costo_mo"]),
                    "herramienta_menor": r2f(r["herr"]),
                    "ganancia_sin_mo": r2f(r["ganancia_sin_mo"]),
                }
                try:
                    folio_final = guardar_cotizacion(datos, seleccion, parametros, ss["sobrescribir"])
                    st.success(f"Cotización {folio_final} guardada.")
                except errors.UniqueViolation:
                    st.error(
                        f"Ya existe una cotización con el folio {folio}. Borra el folio para"
                        " asignar uno nuevo o marca «Actualizar si el folio ya existe»."
                    )
                except psycopg2.Error as exc:
                    st.error(f"No se pudo guardar en la base de datos: {exc.__class__.__name__}")

# ---- Tab 3: Historial ------------------------------------------------------
with tab3:
    st.subheader("Cotizaciones guardadas")
    df_hist = cargar_historial()
    if df_hist.empty:
        st.info("Aún no hay cotizaciones guardadas.")
    else:
        tabla(
            st.dataframe,
            df_hist,
            column_config={
                "total_facturado": st.column_config.NumberColumn("Total c/IVA", format="$%.2f"),
                "ganancia_neta_libre": st.column_config.NumberColumn("Ganancia libre", format="$%.2f"),
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
