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
