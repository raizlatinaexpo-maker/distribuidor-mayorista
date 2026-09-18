# Distribuidor Logístico Raíz Latina V1.5

Esta versión agrega catálogo persistente y sincronización segura con PostgreSQL/Neon.

## Local
1. Instala: `pip install -r requirements.txt`
2. Para usar Neon, define `DATABASE_URL` antes de ejecutar Streamlit.
3. Ejecuta: `streamlit run app.py`

Sin `DATABASE_URL`, la app usa SQLite local como respaldo para pruebas.

## Streamlit Cloud
En Settings > Secrets agrega:

```toml
DATABASE_URL = "postgresql://...?...sslmode=require"
```

El CSV original de Tienda Nube se carga desde la interfaz con **Actualizar catálogo**.

La sincronización no borra productos históricos: los que no aparecen en el último CSV quedan conservados pero inactivos y no se usan para nuevas coincidencias.
### Despacho por cajas
La fecha máxima indicada por el usuario se asigna a la caja 1. Cada caja posterior recibe el siguiente día hábil, omitiendo sábados y domingos, ya que se despacha una sola caja por día.
### Fecha de despacho en Packing List
La fecha máxima de despacho muestra el día de la semana en español (por ejemplo, **Viernes 25/09/2026**). La caja 1 usa la fecha ingresada y cada caja posterior se programa para el siguiente día hábil, omitiendo sábados y domingos.
### Lectura de nombres largos en PDF
Los nombres de productos que vienen partidos en dos o más líneas se reconstruyen antes de la identificación por nombre. En Packing List y Factura Comercial permanecen dentro de una sola celda y pueden mostrarse con salto de línea.
