import datetime
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st

st.set_page_config(page_title="Cotizador PU ", layout="wide")
st.title("⚡ Cotizador PU Técnico con Neon")


def get_conn():
  return psycopg2.connect(st.secrets["DB"])


def init_db():
  with get_conn() as conn:
    with conn.cursor() as cur:
      cur.execute("""
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
                    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cotizacion_insumos (
                    id SERIAL PRIMARY KEY,
                    folio VARCHAR(30) REFERENCES cotizaciones(folio) ON DELETE CASCADE,
                    concepto VARCHAR(150) NOT NULL,
                    cantidad NUMERIC(10, 2) NOT NULL,
                    costo_unitario_sin_iva NUMERIC(12, 2) NOT NULL,
                    importe NUMERIC(12, 2) NOT NULL
                );
            """)
    conn.commit()


def cargar_catalogo() -> pd.DataFrame:
  with get_conn() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
      cur.execute(
          "SELECT id, concepto, unidad, costo_sin_iva::float,"
          " cantidad_default::float, con_factura FROM catalogo_insumos WHERE"
          " activo = TRUE ORDER BY id"
      )
      rows = cur.fetchall()
  return pd.DataFrame(rows)


def guardar_catalogo(df: pd.DataFrame):
  with get_conn() as conn:
    with conn.cursor() as cur:
      for _, row in df.iterrows():
        cant_def = float(row.get("cantidad_default", 1.0))
        if pd.isna(cant_def):
          cant_def = 1.0
        if pd.notna(row.get("id")) and row["id"] != "":
          cur.execute(
              """
                        UPDATE catalogo_insumos
                        SET concepto = %s, unidad = %s, costo_sin_iva = %s,
                            cantidad_default = %s, con_factura = %s
                        WHERE id = %s
                        """,
              (
                  row["concepto"],
                  row["unidad"],
                  float(row["costo_sin_iva"]),
                  cant_def,
                  bool(row["con_factura"]),
                  int(row["id"]),
              ),
          )
        else:
          if row.get("concepto"):
            cur.execute(
                """
                            INSERT INTO catalogo_insumos (concepto, unidad, costo_sin_iva, cantidad_default, con_factura)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                (
                    row["concepto"],
                    row.get("unidad", "Pza"),
                    float(row.get("costo_sin_iva", 0.0)),
                    cant_def,
                    bool(row.get("con_factura", True)),
                ),
            )
    conn.commit()


def guardar_cotizacion(datos_cot: dict, df_detalle: pd.DataFrame):
  with get_conn() as conn:
    with conn.cursor() as cur:
      cur.execute(
          """
                INSERT INTO cotizaciones (
                    folio, cliente, ubicacion, descripcion_servicio, factor_urgencia,
                    costo_directo, indirectos_monto, garantia_monto, utilidad_monto,
                    subtotal, iva_trasladado, total_facturado, iva_por_pagar,
                    isr_estimado, ganancia_neta_libre
                ) VALUES (
                    %(folio)s, %(cliente)s, %(ubicacion)s, %(descripcion_servicio)s, %(factor_urgencia)s,
                    %(costo_directo)s, %(indirectos_monto)s, %(garantia_monto)s, %(utilidad_monto)s,
                    %(subtotal)s, %(iva_trasladado)s, %(total_facturado)s, %(iva_por_pagar)s,
                    %(isr_estimado)s, %(ganancia_neta_libre)s
                )
                """,
          datos_cot,
      )

      usados = df_detalle[df_detalle["Cantidad"] > 0]
      for _, item in usados.iterrows():
        cur.execute(
            """
                    INSERT INTO cotizacion_insumos (folio, concepto, cantidad, costo_unitario_sin_iva, importe)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
            (
                datos_cot["folio"],
                item["Concepto"],
                float(item["Cantidad"]),
                float(item["costo_sin_iva"]),
                float(item["Importe"]),
            ),
        )
    conn.commit()


def cargar_historial() -> pd.DataFrame:
  with get_conn() as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
      cur.execute("""
                SELECT folio, cliente, ubicacion, total_facturado::float,
                       ganancia_neta_libre::float, fecha_creacion
                FROM cotizaciones
                ORDER BY fecha_creacion DESC
            """)
      rows = cur.fetchall()
  return pd.DataFrame(rows)


init_db()
tab1, tab2, tab3 = st.tabs(
    ["1. Catálogo Neon", "2. Nueva Cotización", "3. Historial"]
)

with tab1:
  st.subheader("Catálogo")
  df_cat = cargar_catalogo()
  df_editado = st.data_editor(
      df_cat,
      num_rows="dynamic",
      column_config={
          "id": st.column_config.NumberColumn(disabled=True),
          "cantidad_default": st.column_config.NumberColumn(
              "Cantidad", min_value=0.0, step=0.5, format="%.2f"
          ),
      },
      use_container_width=True,
  )
  if st.button("💾 Guardar cambios"):
    guardar_catalogo(df_editado)
    st.success("Catálogo actualizado.")
    st.rerun()

with tab2:
  df_base = cargar_catalogo()
  if df_base.empty:
    st.warning("Primero agrega insumos en la pestaña 'Catálogo'.")
  else:
    c_info1, c_info2, c_info3 = st.columns(3)
    folio_default = (
        f"COT-{datetime.datetime.now().strftime('%Y%m%d-%H%M')}"
    )
    folio = c_info1.text_input("Folio", value=folio_default)
    cliente = c_info2.text_input("Cliente", value="Cliente Residencial")
    ubicacion = c_info3.text_input("Ubicación", value="Colima")

    df_sel = df_base.copy()
    df_sel["Cantidad"] = df_sel["cantidad_default"].fillna(0.0)
    seleccion = st.data_editor(
        df_sel,
        column_config={
            "id": None,
            "cantidad_default": None,
            "Cantidad": st.column_config.NumberColumn(
                min_value=0.0, step=0.5, format="%.2f"
            ),
        },
        disabled=["concepto", "unidad", "costo_sin_iva", "con_factura"],
        use_container_width=True,
    )
    seleccion.rename(columns={"concepto": "Concepto"}, inplace=True)
    seleccion["Importe"] = (
        seleccion["costo_sin_iva"] * seleccion["Cantidad"]
    )

    col_mo, col_calc = st.columns(2)
    with col_mo:
      st.markdown("**Mano de obra**")
      t_esp = st.number_input("Tarifa Especialista ($/hr)", value=300.0)
      h_esp = st.number_input("Horas Especialista", value=4.0, step=0.5)
      t_ayu = st.number_input("Tarifa Ayudante ($/hr)", value=180.0)
      h_ayu = st.number_input("Horas Ayudante", value=4.0, step=0.5)
      factor_urg = st.selectbox(
          "Factor urgencia / prioridad", [1.0, 1.15, 1.25, 1.5, 2.0], index=2
      )

      ambos_ingenieros = st.checkbox(
          "Ambos somos ingenieros (dividir ganancia neta entre los 2)",
          value=True,
      )
      pct_ing_1 = 50
      if ambos_ingenieros:
        pct_ing_1 = st.slider(
            "Porcentaje de ganancia para Ing. 1 (%)", 0, 100, 50
        )
      pct_ing_2 = 100 - pct_ing_1

    with col_calc:
      st.markdown("**Indirectos, margen y SAT**")
      pct_ind = (
          st.slider(
              "Indirectos (%)",
              0,
              30,
              12,
              help=(
                  "Gastos operativos generales del trabajo (búsqueda de"
                  " refacciones, gestión, desgaste de equipo) aplicados sobre el"
                  " Costo Directo."
              ),
          )
          / 100
      )

      pct_gar = (
          st.slider(
              "Garantía / contingencia (%)",
              0,
              15,
              5,
              help=(
                  "Colchón preventivo por revisiones posteriores, ajustes o"
                  " imprevistos menores, calculado sobre Costo Directo +"
                  " Indirectos."
              ),
          )
          / 100
      )

      pct_uti = (
          st.slider(
              "Utilidad (%)",
              5,
              60,
              25,
              help=(
                  "Margen de ganancia antes de impuestos y desembolsos de"
                  " compra, aplicado sobre Costo Directo + Indirectos +"
                  " Garantía."
              ),
          )
          / 100
      )

      regimen = st.radio(
          "Régimen",
          ["RESICO (1% ISR)", "Actividad Empresarial (~20% s/util)"],
          help=(
              "Estimación orientativa de ISR para calcular la ganancia libre"
              " aproximada; confírmala con tu contador según tu alta final."
          ),
      )

    desc = st.text_area(
        "Concepto para cotización / factura",
        value=(
            "Servicio de mantenimiento correctivo en polipasto monofásico:"
            " diagnóstico, habilitación provisional y montaje definitivo de"
            " banco capacitivo 150 µF / 400 VAC."
        ),
    )

    costo_mat = seleccion["Importe"].sum()
    con_fac_importe = seleccion[seleccion["con_factura"]]["Importe"].sum()
    sin_fac_importe = seleccion[~seleccion["con_factura"]]["Importe"].sum()
    iva_acreditable = con_fac_importe * 0.16
    costo_mo = ((t_esp * h_esp) + (t_ayu * h_ayu)) * factor_urg
    herr_menor = costo_mo * 0.03

    cd = costo_mat + costo_mo + herr_menor
    ci = cd * pct_ind
    gar = (cd + ci) * pct_gar
    uti = (cd + ci + gar) * pct_uti
    subtotal = cd + ci + gar + uti
    iva_tras = subtotal * 0.16
    total_fac = subtotal + iva_tras

    iva_pagar = max(0.0, iva_tras - iva_acreditable)
    isr_est = (
        subtotal * 0.01 if "RESICO" in regimen else max(0.0, uti * 0.20)
    )
    desembolso_mat = (con_fac_importe * 1.16) + sin_fac_importe
    ganancia_libre = total_fac - desembolso_mat - iva_pagar - isr_est

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Costo Directo", f"${cd:,.2f}")
    m2.metric("Subtotal s/IVA", f"${subtotal:,.2f}")
    m3.metric("Total c/IVA", f"${total_fac:,.2f}")
    m4.metric("Ganancia Libre Real", f"${ganancia_libre:,.2f}")

    if ambos_ingenieros:
      pago_1 = max(0.0, ganancia_libre * (pct_ing_1 / 100))
      pago_2 = max(0.0, ganancia_libre * (pct_ing_2 / 100))
      r1, r2 = st.columns(2)
      r1.info(f"Ingeniero 1 ({pct_ing_1}%): **${pago_1:,.2f} MXN**")
      r2.info(f"Ingeniero 2 ({pct_ing_2}%): **${pago_2:,.2f} MXN**")

    if st.button("📌 Guardar Cotización en Neon"):
      guardar_cotizacion(
          {
              "folio": folio,
              "cliente": cliente,
              "ubicacion": ubicacion,
              "descripcion_servicio": desc,
              "factor_urgencia": factor_urg,
              "costo_directo": cd,
              "indirectos_monto": ci,
              "garantia_monto": gar,
              "utilidad_monto": uti,
              "subtotal": subtotal,
              "iva_trasladado": iva_tras,
              "total_facturado": total_fac,
              "iva_por_pagar": iva_pagar,
              "isr_estimado": isr_est,
              "ganancia_neta_libre": ganancia_libre,
          },
          seleccion,
      )
      st.success(f"Cotización {folio} guardada en Neon.")

with tab3:
  st.subheader("Cotizaciones Guardadas")
  df_hist = cargar_historial()
  if df_hist.empty:
    st.info("Aún no hay cotizaciones guardadas.")
  else:
    st.dataframe(df_hist, use_container_width=True)
