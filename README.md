# SOS Terremoto — Reporte ciudadano

Web mobile-first para reportar y consultar afectaciones tras el sismo en el
suroccidente colombiano (Valle del Cauca, Quindío, Chocó, Cauca, Risaralda).

**Secciones** (una sola página, navegación inferior + deep-links):

| Ruta | Qué hace |
|---|---|
| `#/mapa` | Mapa Leaflet con todos los reportes, filtros por tipo/departamento/ciudad/fecha y métricas |
| `#/danos` | Reporte de daño estructural (severidad, personas atrapadas) |
| `#/desaparecidos` | Dos pestañas: reporte de persona desaparecida (familias) y **pacientes sin identificar** (hospitales: llegaron solos/inconscientes). Ambos con foto opcional comprimida en el navegador. El teléfono de contacto **no se publica**: solo se entrega con "Tengo información" / "Es mi familiar" y cada consulta queda auditada |
| `#/donaciones` | Puntos que reciben donaciones y qué insumos necesitan |

Decisiones clave:

- **Cola offline:** los reportes se guardan en `localStorage` y se reenvían
  solos al volver la conexión (`POST /api/reportes` es idempotente por UUID).
- **Geocoding con degradación elegante:** departamento → ciudad (centroide
  local) → autocompletado de dirección vía Photon (geocoder abierto de OSM,
  sin llave) que ubica el pin solo; si no hay red o el geocoder falla, queda
  el flujo manual (pin arrastrable) sin romper nada. Leaflet va vendorizado;
  los únicos servicios externos son los tiles de OSM (fallback: vista de
  lista) y Photon (fallback: pin manual).
- **Publicación inmediata + moderación:** todo reporte sale al mapa al
  instante; `/admin` (JWT + solo por VPN en producción) permite ocultar o
  eliminar falsos (soft delete).

## Correr en local

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # generar JWT_SECRET_KEY (instrucciones dentro)
.venv/bin/python auth.py crear admin --admin
.venv/bin/python app.py --port 8085
# → http://localhost:8085  (app)   http://localhost:8085/admin  (moderación)
```

## Estructura

```
backend/
├── app.py         # Flask + SQLite (schema, validación por tipo, endpoints)
├── auth.py        # bcrypt + JWT (copiado del patrón del vault)
├── catalogo.py    # departamentos/ciudades con centroides (fuente única)
├── templates/     # index.html (app pública) · admin.html (moderación)
├── static/        # fuentes woff2 + Leaflet vendorizado
└── deploy/        # systemd + Caddyfile (VM nube, admin por VPN) + guía
```

## Pendientes

- [ ] **Revisión de seguridad completa** antes de publicar el dominio
      (rate-limits, inyección, exposición del teléfono, hardening VM).
- [ ] Elegir proveedor de nube y dominio; desplegar según `backend/deploy/README-DEPLOY.md`.
- [ ] **Fase 2 — bot WhatsApp (OpenWA):** ingesta de reportes por mensaje.
      La costura ya existe: `POST /api/reportes` acepta `canal:"whatsapp"` y
      `/api/catalogo` expone las ciudades; falta el bot conversacional y un
      header interno de autenticación.
- [ ] Fotos en daños (desaparecidos y pacientes ya las tienen: base64
      comprimido en el navegador → `data/fotos/`; falta extenderlo a daños
      y definir moderación de imágenes).
