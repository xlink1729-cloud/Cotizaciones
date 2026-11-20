      else:
        try:
          folio_final = guardar_cotizacion_multipartida(
              {**datos_totales, "cliente": cliente_val},
              partidas_resumen,
              {
                  "version": 6,
                  "modelo_remuneracion": "mixto",
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
