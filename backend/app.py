"""
SOS Terremoto — backend
=======================

Servicio Flask de reporte ciudadano tras el sismo (Valle del Cauca, Quindío,
Chocó, Cauca, Risaralda). Tres responsabilidades:

1. **Recibir reportes** — `POST /api/reportes` desde la web pública
   (idempotente por `id`; el navegador reintenta desde su cola offline sin
   duplicar). Tres tipos: daño estructural, persona desaparecida y punto de
   donación. Se publican de inmediato (`estado='visible'`).
2. **Servir el mapa público** — `GET /` sirve la app; `GET /api/reportes`
   y `GET /api/metricas` entregan los datos. El teléfono de contacto de un
   desaparecido NUNCA viaja en listados: solo lo entrega
   `POST /api/reportes/<id>/contacto` y cada entrega queda auditada.
3. **Moderación** — `GET /admin` + `/api/admin/*` (JWT de auth.py) para
   ocultar/eliminar reportes falsos y exportar CSV. En producción estas
   rutas solo son alcanzables por VPN (ver deploy/Caddyfile.example).

Fase 2 (no implementada): bot de WhatsApp vía OpenWA que hará POST a
/api/reportes con canal='whatsapp' protegido por un header interno.

Uso:
- Producción: gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8085 app:app
- Local/dev:  python app.py --port 8085
"""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import io
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

import auth
import catalogo

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ.get("SISMO_DB", BASE_DIR / "data" / "sismo.db"))
FOTOS_DIR = DB_PATH.parent / "fotos"
TOMTOM_KEY = os.environ.get("TOMTOM_KEY", "").strip()

app = Flask(__name__)
# la foto viaja en el JSON como base64 (~1.4x su peso real); tope de seguridad
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

TIPOS = ("dano", "desaparecido", "donacion", "hospital", "mascota")
SEVERIDADES = ("leve", "moderado", "grave", "colapso")
NECESIDADES = ("agua", "alimentos", "medicamentos", "ropa", "cobijas", "aseo", "otros")
TIPOS_AYUDA = ("herramientas", "maquinaria", "personas")
# 'donacion' agrupa las ayudas; el subtipo (en extras, NO en la columna tipo)
# distingue punto de acopio de refugio sin tocar el esquema ni migrar filas.
SERVICIOS_REFUGIO = ("agua", "comida", "dormida", "banos", "medica", "electricidad")

# ─── SQLite ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS reportes (
  id                 TEXT PRIMARY KEY,          -- UUID generado en el cliente (idempotencia de la cola offline)
  tipo               TEXT NOT NULL CHECK (tipo IN ('dano','desaparecido','donacion','hospital','mascota')),
  departamento       TEXT NOT NULL,
  ciudad             TEXT NOT NULL,
  direccion          TEXT,
  lat                REAL NOT NULL,
  lng                REAL NOT NULL,
  ubicacion_ajustada INTEGER DEFAULT 0,         -- 1 = el reportante movió el pin (precisión real, no centroide)
  descripcion        TEXT,
  extras             TEXT,                      -- JSON específico por tipo (validado en Python)
  telefono_contacto  TEXT,                      -- SOLO desaparecidos; JAMÁS en listados públicos
  fotos              TEXT,                      -- reservado v2 (las subidas no sirven con la red degradada de una emergencia)
  estado             TEXT DEFAULT 'visible',    -- visible | oculto | eliminado (soft delete: nada se borra de la BD)
  canal              TEXT DEFAULT 'web',        -- web | whatsapp (fase 2)
  confirmaciones     INTEGER DEFAULT 1,         -- cuántas personas han reportado/confirmado esto
  resuelto           INTEGER DEFAULT 0,         -- 1 = encontrado/reunido (desaparecidos, pacientes, mascotas) · 2 = fallecido (solo personas)
  resuelto_comentario TEXT,                     -- cómo/dónde apareció (público)
  resuelto_en        TEXT,
  ayudando           INTEGER DEFAULT 0,         -- coordinación en el punto (daños y ayudas):
  faltan             INTEGER DEFAULT 0,         --   cuántos ayudan ahora / cuántas manos faltan
  vigente_en         TEXT,                      -- última señal "sigo aquí, esto sigue vigente"
  creado_en          TEXT DEFAULT (datetime('now')),
  moderado_en        TEXT,
  moderado_por       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rep_tipo   ON reportes(tipo);
CREATE INDEX IF NOT EXISTS idx_rep_ciudad ON reportes(departamento, ciudad);
CREATE INDEX IF NOT EXISTS idx_rep_estado ON reportes(estado);
CREATE INDEX IF NOT EXISTS idx_rep_fecha  ON reportes(creado_en);

CREATE TABLE IF NOT EXISTS accesos_telefono (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  reporte_id         TEXT NOT NULL,
  nombre_solicitante TEXT,
  ip                 TEXT,
  user_agent         TEXT,
  creado_en          TEXT DEFAULT (datetime('now'))
);

-- Confirmaciones comunitarias: quién sumó al contador de un reporte (una por
-- IP y reporte; la persona que reporta cuenta como la primera).
CREATE TABLE IF NOT EXISTS confirmaciones_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  reporte_id TEXT NOT NULL,
  ip         TEXT,
  user_agent TEXT,
  creado_en  TEXT DEFAULT (datetime('now')),
  UNIQUE(reporte_id, ip)
);

-- Bitácora del reporte: notas libres de la comunidad ("lo vi en tal parte",
-- "la calle sigue cerrada") + eventos automáticos (confirmó vigencia, cambió
-- los contadores de gente). El campo evento distingue: nota | vigente | gente.
CREATE TABLE IF NOT EXISTS avistamientos (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  reporte_id TEXT NOT NULL,
  nota       TEXT NOT NULL,
  evento     TEXT DEFAULT 'nota',
  foto       TEXT,                              -- foto adjunta a la nota (data/fotos/av-*)
  ip         TEXT,
  user_agent TEXT,
  creado_en  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_avist ON avistamientos(reporte_id, creado_en);

-- Quién marcó un reporte como encontrado/reunido, con su comentario.
CREATE TABLE IF NOT EXISTS resoluciones_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  reporte_id TEXT NOT NULL,
  comentario TEXT,
  ip         TEXT,
  user_agent TEXT,
  creado_en  TEXT DEFAULT (datetime('now'))
);

-- Caché de geocoding: cada consulta resuelta (TomTom o Photon) se guarda y
-- las repeticiones salen de aquí sin gastar cuota externa.
CREATE TABLE IF NOT EXISTS geocache (
  clave      TEXT PRIMARY KEY,             -- ciudad|consulta_normalizada
  resultados TEXT NOT NULL,                -- JSON [{principal,secundario,lat,lng}]
  fuente     TEXT,                         -- tomtom | photon
  creado_en  TEXT DEFAULT (datetime('now'))
);

-- Directorio comunitario: direcciones confirmadas por reportantes (pin
-- ajustado a mano). Se ofrecen de primeras en el autocompletado, gratis.
CREATE TABLE IF NOT EXISTS direcciones_conocidas (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  departamento   TEXT NOT NULL,
  ciudad         TEXT NOT NULL,
  direccion      TEXT NOT NULL,            -- como la escribió la persona
  direccion_norm TEXT NOT NULL,            -- normalizada en minúsculas, para buscar
  lat            REAL NOT NULL,
  lng            REAL NOT NULL,
  veces          INTEGER DEFAULT 1,
  actualizado_en TEXT DEFAULT (datetime('now')),
  UNIQUE(ciudad, direccion_norm)
);
CREATE INDEX IF NOT EXISTS idx_dircon ON direcciones_conocidas(ciudad, direccion_norm);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        # migración: bases creadas antes del tipo 'hospital' tienen un CHECK
        # que lo rechaza; SQLite no permite alterar CHECKs → se reconstruye
        # la tabla una sola vez copiando los datos
        # migración: bases creadas antes del contador de confirmaciones
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reportes)")}
        if "confirmaciones" not in cols:
            conn.execute("ALTER TABLE reportes ADD COLUMN confirmaciones INTEGER DEFAULT 1")
        if "resuelto" not in cols:
            conn.execute("ALTER TABLE reportes ADD COLUMN resuelto INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE reportes ADD COLUMN resuelto_comentario TEXT")
            conn.execute("ALTER TABLE reportes ADD COLUMN resuelto_en TEXT")
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name='reportes'").fetchone()[0]
        if "'mascota'" not in sql:
            # por columnas NOMBRADAS: el orden físico de una base migrada con
            # ALTER no coincide con el del SCHEMA y un SELECT * posicional
            # cruzaría los datos
            columnas = ("id, tipo, departamento, ciudad, direccion, lat, lng,"
                        " ubicacion_ajustada, descripcion, extras, telefono_contacto,"
                        " fotos, estado, canal, confirmaciones, resuelto,"
                        " resuelto_comentario, resuelto_en, creado_en,"
                        " moderado_en, moderado_por")
            conn.executescript(
                "ALTER TABLE reportes RENAME TO reportes_v1;"
                + SCHEMA
                + f"INSERT INTO reportes ({columnas}) SELECT {columnas} FROM reportes_v1;"
                  "DROP TABLE reportes_v1;")
            # el RENAME se llevó los índices idx_rep_* y el DROP los eliminó:
            # segunda pasada del SCHEMA para recrearlos sobre la tabla nueva
            conn.executescript(SCHEMA)
        # migración: coordinación en el punto (fase "gente en el punto")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reportes)")}
        if "ayudando" not in cols:
            conn.execute("ALTER TABLE reportes ADD COLUMN ayudando INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE reportes ADD COLUMN faltan INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE reportes ADD COLUMN vigente_en TEXT")
        cols_av = {r[1] for r in conn.execute("PRAGMA table_info(avistamientos)")}
        if "evento" not in cols_av:
            conn.execute("ALTER TABLE avistamientos ADD COLUMN evento TEXT DEFAULT 'nota'")
        if "foto" not in cols_av:
            conn.execute("ALTER TABLE avistamientos ADD COLUMN foto TEXT")
        # el caché de geocoding no necesita vivir para siempre
        conn.execute("DELETE FROM geocache WHERE creado_en < datetime('now','-90 days')")


init_db()
auth.registrar_endpoints(app, db)

# El frontend carga el catálogo desde /static/catalogo.js: con 1.000+
# municipios ya no viaja inline en el HTML. Se regenera en cada arranque
# para que catalogo.py sea la única fuente de verdad.
_CATALOGO_JS = BASE_DIR / "static" / "catalogo.js"
_CATALOGO_JS.write_text(
    "// Generado por app.py desde catalogo.py — no editar a mano\n"
    "window.CATALOGO = " + json.dumps(
        {d: {c: [lat, lng] for c, (lat, lng) in cs.items()}
         for d, cs in catalogo.CATALOGO.items()}, ensure_ascii=False) + ";\n",
    encoding="utf-8")

if not os.environ.get("JWT_SECRET_KEY"):
    print("[AVISO] JWT_SECRET_KEY no está en .env — el login del panel admin fallará. "
          "Genera uno: python -c \"import secrets; print(secrets.token_hex(48))\"", flush=True)

# ─── CORS (solo /api) ─────────────────────────────────────────────────────────


@app.after_request
def cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# Content Security Policy: los scripts propios van inline (app sin build), así
# que script-src necesita 'unsafe-inline'; aun así la CSP bloquea scripts de
# otros orígenes y limita a dónde se puede exfiltrar (connect-src 'self'). Los
# tiles del mapa vienen de openstreetmap.org; el favicon es un data: URI.
_CSP = ("default-src 'self'; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def cabeceras_seguridad(resp):
    # Content-Security-Policy DESACTIVADA temporalmente: en la emergencia la
    # prioridad es que la app funcione (la CSP estuvo interfiriendo con las
    # fotos). Reactivar `_CSP` cuando la subida esté estable. Las otras
    # cabeceras no bloquean nada, se mantienen.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/<path:_>", methods=["OPTIONS"])
def cors_preflight(_):
    return "", 204


# ─── Rate limit en memoria ────────────────────────────────────────────────────
#
# Suficiente con un solo worker de gunicorn (convención de todos los servicios
# del homelab). No sustituye la revisión de seguridad pendiente: es la primera
# barrera contra spam masivo, no contra un atacante dedicado.

_hits: dict[str, deque] = defaultdict(deque)
_ultima_purga = [0.0]          # holder mutable: última barrida global de _hits
_VENTANA_MAX = 3600            # ninguna ventana de rate-limit supera 1 hora


def _purgar_hits(ahora: float) -> None:
    """Evita que _hits crezca sin límite en una emergencia larga: cada 5 min
    barre todas las claves, descarta marcas más viejas que la ventana máxima
    y elimina las colas vacías. Itera sobre una copia de las claves para no
    chocar con los otros hilos de gunicorn."""
    if ahora - _ultima_purga[0] < 300:
        return
    _ultima_purga[0] = ahora
    for clave in list(_hits.keys()):
        cola = _hits.get(clave)
        if cola is None:
            continue
        while cola and ahora - cola[0] > _VENTANA_MAX:
            cola.popleft()
        if not cola:
            _hits.pop(clave, None)


def _rate_ok(clave: str, maximo: int, ventana_seg: int) -> bool:
    ahora = time.monotonic()
    _purgar_hits(ahora)
    cola = _hits[clave]
    while cola and ahora - cola[0] > ventana_seg:
        cola.popleft()
    if len(cola) >= maximo:
        return False
    cola.append(ahora)
    return True


def _ip() -> str:
    # Se toma el ÚLTIMO valor de X-Forwarded-For, no el primero: Caddy añade la
    # IP real del cliente al final de la cadena. Si el cliente falsifica su
    # propio XFF, queda a la izquierda y Caddy pone la verdadera a la derecha.
    # Tomar el primero permitía spoofear la IP y así saltarse los rate-limits,
    # inflar el contador de confirmaciones y envenenar los logs de auditoría.
    # (Detrás de un único proxy de confianza; si algún día hay varios, ajustar.)
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote_addr or "?"


# ─── Validación por tipo ──────────────────────────────────────────────────────

def _texto(valor, maximo: int) -> str:
    return str(valor or "").strip()[:maximo]


def _lista_abierta(valores, conocidos: tuple[str, ...], maximo: int = 8) -> list[str]:
    """Normaliza una lista de etiquetas SIN descartar las desconocidas: pueden
    venir con errores de escritura (o del futuro ingest por WhatsApp) y en una
    emergencia es peor perder la información que tenerla imperfecta. Las
    conocidas van primero en su orden canónico; el resto se conserva como
    texto saneado, sin duplicados."""
    if not isinstance(valores, list):
        return []
    con = [v for v in valores if v in conocidos]
    otras = []
    for v in valores:
        t = _texto(v, 40)
        if t and v not in conocidos and t not in otras:
            otras.append(t)
    return (sorted(set(con), key=conocidos.index) + otras)[:maximo]


def validar_reporte(data: dict) -> tuple[dict | None, str | None]:
    """Devuelve (fila_lista_para_insertar, None) o (None, motivo de rechazo)."""
    if not isinstance(data, dict):
        return None, "Cuerpo inválido"

    rid = _texto(data.get("id"), 64)
    # SOLO UUID-like: sin '/', '.' ni '..' — el id nombra el archivo de la foto
    # ({id}.ext), así que un id con separadores permitiría escribir la foto
    # fuera de data/fotos/ (path traversal). El cliente usa crypto.randomUUID().
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid):
        return None, "id de reporte inválido"

    tipo = data.get("tipo")
    if tipo not in TIPOS:
        return None, "Tipo de reporte inválido"

    depto = _texto(data.get("departamento"), 60)
    ciudad = _texto(data.get("ciudad"), 60)
    if not catalogo.ciudad_valida(depto, ciudad):
        return None, "Ciudad o departamento fuera del catálogo"

    # Ubicación: si no llega lat/lng usable cae al centroide de la ciudad.
    # Si llega, se acepta solo dentro de una caja generosa de Colombia para
    # descartar coordenadas basura (0,0 o fuera del país).
    cen_lat, cen_lng = catalogo.centroide(depto, ciudad)
    try:
        lat, lng = float(data.get("lat")), float(data.get("lng"))
        if not (-5.0 <= lat <= 14.0 and -82.0 <= lng <= -66.0):
            raise ValueError
        ajustada = 1 if data.get("ubicacion_ajustada") else 0
    except (TypeError, ValueError):
        lat, lng, ajustada = cen_lat, cen_lng, 0

    extras_in = data.get("extras") or {}
    if not isinstance(extras_in, dict):
        return None, "extras debe ser un objeto"
    extras: dict = {}
    telefono = None

    if tipo == "dano":
        if extras_in.get("severidad") not in SEVERIDADES:
            return None, "La severidad del daño es obligatoria"
        extras["severidad"] = extras_in["severidad"]
        if extras_in.get("personas_atrapadas") is not None:
            extras["personas_atrapadas"] = bool(extras_in["personas_atrapadas"])
        # ayuda en el lugar: qué se necesita para atender el daño
        if extras_in.get("necesita_ayuda"):
            extras["necesita_ayuda"] = True
            tipos = _lista_abierta(extras_in.get("ayuda_tipos"), TIPOS_AYUDA)
            if tipos:
                extras["ayuda_tipos"] = tipos
            if extras_in.get("ayuda_detalle"):
                extras["ayuda_detalle"] = _texto(extras_in["ayuda_detalle"], 500)

    elif tipo == "desaparecido":
        nombre = _texto(extras_in.get("nombre"), 120)
        visto = _texto(extras_in.get("visto_ultima_vez"), 200)
        if not nombre or not visto:
            return None, "Nombre y último lugar/momento visto son obligatorios"
        extras["nombre"] = nombre
        extras["visto_ultima_vez"] = visto
        if extras_in.get("edad") not in (None, ""):
            try:
                edad = int(extras_in["edad"])
            except (TypeError, ValueError):
                return None, "Edad inválida"
            if not 0 <= edad <= 120:
                return None, "Edad inválida"
            extras["edad"] = edad
        if extras_in.get("descripcion_fisica"):
            extras["descripcion_fisica"] = _texto(extras_in["descripcion_fisica"], 500)
        telefono = _texto(data.get("telefono_contacto"), 30)
        if len("".join(c for c in telefono if c.isdigit())) < 7:
            return None, "El teléfono de contacto es obligatorio (mínimo 7 dígitos)"

    elif tipo == "mascota":
        # mascota perdida: mismo esquema que desaparecidos (foto + teléfono
        # protegido con "Tengo información")
        especie = _texto(extras_in.get("especie"), 30)
        desc_fisica = _texto(extras_in.get("descripcion_fisica"), 500)
        visto = _texto(extras_in.get("visto_ultima_vez"), 200)
        if not especie:
            return None, "Indica qué tipo de animal es"
        if not desc_fisica or not visto:
            return None, "La descripción y dónde se perdió son obligatorios"
        extras["especie"] = especie
        extras["descripcion_fisica"] = desc_fisica
        extras["visto_ultima_vez"] = visto
        # busco = se me perdió; encontrada = la tengo y busco a sus dueños
        extras["situacion"] = (extras_in.get("situacion")
                               if extras_in.get("situacion") in ("busco", "encontrada")
                               else "busco")
        if extras_in.get("nombre_mascota"):
            extras["nombre_mascota"] = _texto(extras_in["nombre_mascota"], 60)
        if extras_in.get("raza"):
            extras["raza"] = _texto(extras_in["raza"], 60)
        telefono = _texto(data.get("telefono_contacto"), 30)
        if len("".join(c for c in telefono if c.isdigit())) < 7:
            return None, "El teléfono de contacto es obligatorio (mínimo 7 dígitos)"

    elif tipo == "hospital":
        # paciente sin identificar reportado por un hospital: llegó solo,
        # inconsciente o sin datos de su familia. Reporte abierto pero con
        # datos de quien reporta (la moderación admin filtra falsos).
        hospital = _texto(extras_in.get("hospital"), 120)
        rep_nombre = _texto(extras_in.get("reportante_nombre"), 120)
        rep_cargo = _texto(extras_in.get("reportante_cargo"), 80)
        desc_fisica = _texto(extras_in.get("descripcion_fisica"), 500)
        if not hospital or not rep_nombre or not rep_cargo:
            return None, "Nombre del hospital y nombre y cargo de quien reporta son obligatorios"
        if not desc_fisica:
            return None, "La descripción física del paciente es obligatoria"
        extras["hospital"] = hospital
        extras["reportante_nombre"] = rep_nombre
        extras["reportante_cargo"] = rep_cargo
        extras["descripcion_fisica"] = desc_fisica
        if extras_in.get("edad_aprox") not in (None, ""):
            try:
                edad = int(extras_in["edad_aprox"])
            except (TypeError, ValueError):
                return None, "Edad aproximada inválida"
            if not 0 <= edad <= 120:
                return None, "Edad aproximada inválida"
            extras["edad_aprox"] = edad
        for campo, maximo in (("sexo", 20), ("estado_salud", 40),
                              ("senas_particulares", 300), ("ropa", 200),
                              ("fecha_ingreso", 60)):
            if extras_in.get(campo):
                extras[campo] = _texto(extras_in[campo], maximo)
        telefono = _texto(data.get("telefono_contacto"), 30)
        if len("".join(c for c in telefono if c.isdigit())) < 7:
            return None, "El teléfono del hospital es obligatorio (mínimo 7 dígitos)"

    elif tipo == "donacion":
        # subtipo dentro de extras: los reportes viejos sin él son 'acopio'
        subtipo = (extras_in.get("subtipo")
                   if extras_in.get("subtipo") in ("acopio", "refugio") else "acopio")
        extras["subtipo"] = subtipo
        if subtipo == "refugio":
            nombre = _texto(extras_in.get("nombre_punto"), 120)
            if not nombre:
                return None, "El nombre o referencia del refugio es obligatorio"
            extras["nombre_punto"] = nombre
            if extras_in.get("capacidad") not in (None, ""):
                try:
                    cap = int(extras_in["capacidad"])
                except (TypeError, ValueError):
                    return None, "Capacidad inválida"
                if not 0 <= cap <= 100000:
                    return None, "Capacidad inválida"
                extras["capacidad"] = cap
            servicios = _lista_abierta(extras_in.get("servicios"), SERVICIOS_REFUGIO)
            if servicios:
                extras["servicios"] = servicios
            if extras_in.get("admite_mascotas") is not None:
                extras["admite_mascotas"] = bool(extras_in["admite_mascotas"])
            if extras_in.get("horario"):
                extras["horario"] = _texto(extras_in["horario"], 120)
            # teléfono del refugio: público y OPCIONAL (va en extras, no en la
            # columna protegida). Si viene, se valida mínimo.
            tel = _texto(extras_in.get("telefono"), 30)
            if tel:
                if len("".join(c for c in tel if c.isdigit())) < 7:
                    return None, "El teléfono del refugio no parece válido"
                extras["telefono"] = tel
        else:  # acopio (donaciones) — como estaba
            if not isinstance(extras_in.get("necesidades"), list):
                return None, "necesidades debe ser una lista"
            necesidades = _lista_abierta(extras_in["necesidades"], NECESIDADES)
            if not necesidades:
                return None, "Indica al menos un insumo necesario"
            extras["necesidades"] = necesidades
            if extras_in.get("nombre_punto"):
                extras["nombre_punto"] = _texto(extras_in["nombre_punto"], 120)
            if extras_in.get("horario"):
                extras["horario"] = _texto(extras_in["horario"], 120)

    canal = data.get("canal") if data.get("canal") in ("web", "whatsapp") else "web"

    # coordinación en el punto: solo daños y ayudas, desde el mismo formulario
    def _contador(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 0
        return max(0, min(n, 9999))
    ayudando = _contador(data.get("ayudando")) if tipo in ("dano", "donacion") else 0
    faltan = _contador(data.get("faltan")) if tipo in ("dano", "donacion") else 0

    return {
        "id": rid, "tipo": tipo, "departamento": depto, "ciudad": ciudad,
        "direccion": _texto(data.get("direccion"), 200),
        "lat": round(lat, 6), "lng": round(lng, 6), "ubicacion_ajustada": ajustada,
        "descripcion": _texto(data.get("descripcion"), 1000),
        "extras": json.dumps(extras, ensure_ascii=False),
        "telefono_contacto": telefono, "canal": canal, "fotos": None,
        "ayudando": ayudando, "faltan": faltan,
    }, None


def _guardar_foto(fila: dict, foto) -> str | None:
    """Guarda la foto (data URL base64) en disco y anota el nombre en la fila.
    Devuelve un mensaje de error o None. Aplica a todos los tipos: persona
    (desaparecidos/pacientes), foto de la zona en daños y flyer del punto de
    donación."""
    if not foto:
        return None
    m = re.match(r"data:image/(jpeg|png|webp);base64,(.+)$", str(foto), re.S)
    if not m:
        return "Formato de foto inválido"
    try:
        crudo = base64.b64decode(m.group(2), validate=True)
    except Exception:
        return "Foto corrupta"
    if len(crudo) > 3_000_000:
        return "La foto supera 3 MB; intenta con una más liviana"
    # magia del archivo: no confiar en el mime declarado
    if not (crudo.startswith(b"\xff\xd8") or crudo.startswith(b"\x89PNG")
            or crudo[8:12] == b"WEBP"):
        return "El archivo no es una imagen válida"
    ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[m.group(1)]
    FOTOS_DIR.mkdir(parents=True, exist_ok=True)
    # el id ya viene validado (solo [A-Za-z0-9_-]); doble malla contra traversal
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", fila["id"]):
        return "id de reporte inválido"
    nombre = f"{fila['id']}.{ext}"
    (FOTOS_DIR / nombre).write_bytes(crudo)
    fila["fotos"] = nombre
    return None


# Columnas públicas: telefono_contacto queda EXCLUIDO a propósito — el SELECT
# es explícito para que nunca se filtre por un futuro SELECT *.
COLS_PUBLICAS = ("id, tipo, departamento, ciudad, direccion, lat, lng,"
                 " ubicacion_ajustada, descripcion, extras, fotos, canal,"
                 " confirmaciones, resuelto, resuelto_comentario, resuelto_en,"
                 " ayudando, faltan, vigente_en, creado_en")


def _fila_publica(f: sqlite3.Row) -> dict:
    d = dict(f)
    d["extras"] = json.loads(d.get("extras") or "{}")
    return d


# ─── Endpoints públicos ───────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin():
    return render_template("admin.html")


@app.get("/salud")
def salud():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM reportes").fetchone()[0]
    return jsonify({"ok": True, "reportes": total})


@app.get("/api/catalogo")
def api_catalogo():
    return jsonify(catalogo.como_json())


@app.post("/api/reportes")
def crear_reporte():
    if not _rate_ok(f"crear:{_ip()}", maximo=10, ventana_seg=60):
        return jsonify({"error": "Demasiados reportes seguidos; espera un momento"}), 429

    data = request.get_json(silent=True) or {}
    # honeypot: campo invisible en el formulario; los bots lo llenan
    if _texto(data.get("sitio_web"), 10):
        return jsonify({"ok": True}), 201

    fila, error = validar_reporte(data)
    if error:
        return jsonify({"error": error}), 400
    error = _guardar_foto(fila, data.get("foto"))
    if error:
        return jsonify({"error": error}), 400

    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO reportes (id, tipo, departamento, ciudad, direccion, lat, lng,"
                " ubicacion_ajustada, descripcion, extras, telefono_contacto, canal, fotos,"
                " ayudando, faltan)"
                " VALUES (:id, :tipo, :departamento, :ciudad, :direccion, :lat, :lng,"
                " :ubicacion_ajustada, :descripcion, :extras, :telefono_contacto, :canal, :fotos,"
                " :ayudando, :faltan)",
                fila)
            # directorio comunitario: solo direcciones con pin ajustado a mano
            # (las de centroide contaminarían el directorio con puntos genéricos)
            if fila["direccion"] and fila["ubicacion_ajustada"]:
                conn.execute(
                    "INSERT INTO direcciones_conocidas"
                    " (departamento, ciudad, direccion, direccion_norm, lat, lng)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(ciudad, direccion_norm) DO UPDATE SET"
                    " veces=veces+1, lat=excluded.lat, lng=excluded.lng,"
                    " actualizado_en=datetime('now')",
                    (fila["departamento"], fila["ciudad"], fila["direccion"],
                     _normalizar_direccion(fila["direccion"]).lower(),
                     fila["lat"], fila["lng"]))
            # quien reporta es la primera confirmación: su IP queda en el log
            # para que no pueda inflar su propio contador después
            conn.execute(
                "INSERT INTO confirmaciones_log (reporte_id, ip, user_agent) VALUES (?,?,?)"
                " ON CONFLICT(reporte_id, ip) DO NOTHING",
                (fila["id"], _ip(), _texto(request.headers.get("User-Agent"), 300)))
    except sqlite3.IntegrityError:
        # reintento de la cola offline: el servidor ya lo tiene → el cliente lo desencola
        return jsonify({"ok": True, "duplicado": True}), 409
    return jsonify({"ok": True, "id": fila["id"]}), 201


@app.get("/api/reportes")
def listar_reportes():
    filtros, params = ["estado='visible'"], []
    # tipo acepta lista separada por comas (ej. desaparecido,hospital: la
    # pestaña Desaparecidos muestra ambos)
    tipos = [t for t in (request.args.get("tipo") or "").split(",") if t in TIPOS]
    if tipos:
        filtros.append(f"tipo IN ({','.join('?' * len(tipos))})")
        params.extend(tipos)
    for campo in ("departamento", "ciudad"):
        if request.args.get(campo):
            filtros.append(f"{campo}=?")
            params.append(request.args[campo])
    if request.args.get("resuelto") in ("0", "1", "2"):
        filtros.append("resuelto=?")
        params.append(int(request.args["resuelto"]))
    if request.args.get("falta_gente") == "1":
        filtros.append("faltan > 0")
    # subtipo de ayuda (acopio | refugio) vía json_extract, parametrizado
    if request.args.get("subtipo") in ("acopio", "refugio"):
        if request.args["subtipo"] == "refugio":
            filtros.append("json_extract(extras,'$.subtipo')='refugio'")
        else:
            filtros.append("COALESCE(json_extract(extras,'$.subtipo'),'acopio')!='refugio'")
    if request.args.get("desde"):
        filtros.append("creado_en >= ?")
        params.append(request.args["desde"])
    if request.args.get("hasta"):
        filtros.append("creado_en <= ?")
        params.append(request.args["hasta"] + " 23:59:59")
    # última nota de la comunidad (avistamiento / actualización de zona) y su
    # hora, para mostrarla en el resumen y ordenar por actividad reciente
    # solo notas humanas para el resumen; los eventos automáticos (vigente,
    # gente) sí cuentan para ordenar por actividad, pero no como "última nota"
    sub_nota = ("(SELECT nota FROM avistamientos a WHERE a.reporte_id=reportes.id"
                " AND COALESCE(a.evento,'nota')='nota'"
                " ORDER BY a.creado_en DESC, a.id DESC LIMIT 1)")
    sub_nota_en = "(SELECT MAX(creado_en) FROM avistamientos a WHERE a.reporte_id=reportes.id)"
    with db() as conn:
        filas = conn.execute(
            f"SELECT {COLS_PUBLICAS}, {sub_nota} AS ultima_nota,"
            f" {sub_nota_en} AS ultima_nota_en"
            f" FROM reportes WHERE {' AND '.join(filtros)}"
            # ordena por la actividad más reciente: creación o última nota
            f" ORDER BY MAX(creado_en, COALESCE({sub_nota_en}, creado_en)) DESC"
            " LIMIT 2000", params).fetchall()
    return jsonify([_fila_publica(f) for f in filas])


@app.get("/api/reportes/<rid>")
def detalle_reporte(rid):
    with db() as conn:
        f = conn.execute(f"SELECT {COLS_PUBLICAS} FROM reportes"
                         " WHERE id=? AND estado='visible'", (rid,)).fetchone()
    if not f:
        return jsonify({"error": "Reporte no encontrado"}), 404
    return jsonify(_fila_publica(f))


@app.post("/api/reportes/<rid>/contacto")
def contacto_desaparecido(rid):
    """Entrega el teléfono de contacto de un desaparecido (familia) o de un
    paciente sin identificar (hospital) a quien declara tener información.
    Cada entrega queda registrada (auditoría antiacoso)."""
    if not _rate_ok(f"contacto:{_ip()}", maximo=5, ventana_seg=3600):
        return jsonify({"error": "Demasiadas consultas; intenta más tarde"}), 429

    data = request.get_json(silent=True) or {}
    with db() as conn:
        f = conn.execute("SELECT telefono_contacto FROM reportes"
                         " WHERE id=? AND tipo IN ('desaparecido','hospital','mascota')"
                         " AND estado='visible'",
                         (rid,)).fetchone()
        if not f or not f["telefono_contacto"]:
            return jsonify({"error": "Reporte no encontrado"}), 404
        conn.execute(
            "INSERT INTO accesos_telefono (reporte_id, nombre_solicitante, ip, user_agent)"
            " VALUES (?,?,?,?)",
            (rid, _texto(data.get("nombre_solicitante"), 120), _ip(),
             _texto(request.headers.get("User-Agent"), 300)))
    return jsonify({"telefono_contacto": f["telefono_contacto"]})


# ─── Autocompletado de direcciones (proxy: la key nunca llega al navegador) ──
#
# El navegador consulta aquí; este backend habla con TomTom (datos propios,
# mejor cobertura de predios en Colombia) usando TOMTOM_KEY del .env. Sin key,
# o si TomTom falla, cae a Photon/OSM. Respuesta unificada:
#   [{"principal": str, "secundario": str, "lat": float, "lng": float}]

_ABREV_DIR = [
    (re.compile(r"\b(cll|cl)\.?\s*(?=\d)", re.I), "calle "),
    (re.compile(r"\b(cra|kra|cr|kr)\.?\s*(?=\d)", re.I), "carrera "),
    (re.compile(r"\b(av|avda)\.?\s*(?=\d)", re.I), "avenida "),
    (re.compile(r"\b(dg|diag)\.?\s*(?=\d)", re.I), "diagonal "),
    (re.compile(r"\b(tv|transv)\.?\s*(?=\d)", re.I), "transversal "),
    (re.compile(r"\bn[oº°]\.?\s*(?=\d)", re.I), " "),
]


def _normalizar_direccion(q: str) -> str:
    """Expande el formato colombiano (Cra 5 #10-23) para los geocoders."""
    for patron, reemplazo in _ABREV_DIR:
        q = patron.sub(reemplazo, q)
    return re.sub(r"\s+", " ", q.replace("#", " ").replace("-", " ")).strip()


def _buscar_tomtom(q: str, ciudad: str, lat: float, lng: float) -> list[dict]:
    r = requests.get(
        f"https://api.tomtom.com/search/2/search/{requests.utils.quote(q + ', ' + ciudad)}.json",
        params={"key": TOMTOM_KEY, "limit": 6, "countrySet": "CO",
                "lat": lat, "lon": lng, "radius": 30000, "typeahead": "true"},
        timeout=4)
    r.raise_for_status()
    out = []
    for res in r.json().get("results", []):
        dire = res.get("address", {})
        pos = res.get("position", {})
        principal = (res.get("poi", {}).get("name")
                     or dire.get("freeformAddress", "").split(",")[0].strip())
        secundario = ", ".join(x for x in (
            dire.get("municipalitySubdivision"), dire.get("municipality"),
            dire.get("countrySubdivision")) if x)
        if principal and pos.get("lat") is not None:
            out.append({"principal": principal, "secundario": secundario,
                        "lat": pos["lat"], "lng": pos["lon"]})
    return out


def _buscar_photon(q: str, ciudad: str, lat: float, lng: float) -> list[dict]:
    d = 0.22  # bbox ~25 km: el sesgo por lat/lon de Photon no filtra por sí solo
    r = requests.get(
        "https://photon.komoot.io/api/",
        params={"q": f"{q}, {ciudad}", "limit": 8, "lat": lat, "lon": lng,
                "bbox": f"{lng - d},{lat - d},{lng + d},{lat + d}"},
        # Photon devuelve 403 al User-Agent genérico de requests; su política
        # pide identificarse
        headers={"User-Agent": "SOS-Terremoto-Colombia/1.0 (app ciudadana de emergencia)"},
        timeout=4)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None])
        principal = p.get("name") or " ".join(x for x in (p.get("street"), p.get("housenumber")) if x)
        secundario = ", ".join(x for x in (p.get("district"), p.get("city"), p.get("state")) if x)
        if principal and coords[0] is not None:
            out.append({"principal": principal, "secundario": secundario,
                        "lat": coords[1], "lng": coords[0]})
    # la ciudad elegida primero; municipios vecinos del bbox después
    out.sort(key=lambda s: 0 if ciudad in s["secundario"] else 1)
    return out[:6]


@app.get("/api/direcciones")
def buscar_direcciones():
    """Tres niveles, del más barato al más caro:
    1. direcciones_conocidas — confirmadas por reportantes (gratis, propio)
    2. geocache — consultas ya resueltas antes (gratis, propio)
    3. TomTom → Photon — solo si la consulta nunca se ha hecho"""
    if not _rate_ok(f"dir:{_ip()}", maximo=30, ventana_seg=60):
        return jsonify([]), 429
    q = _texto(request.args.get("q"), 120)
    depto = _texto(request.args.get("departamento"), 60)
    ciudad = _texto(request.args.get("ciudad"), 60)
    if len(q) < 3 or not catalogo.ciudad_valida(depto, ciudad):
        return jsonify([])
    lat, lng = catalogo.centroide(depto, ciudad)
    qn = _normalizar_direccion(q).lower()
    clave = f"{ciudad}|{qn}"

    with db() as conn:
        locales = [
            {"principal": f["direccion"], "secundario": f"Usada en reportes · {ciudad}",
             "lat": f["lat"], "lng": f["lng"]}
            for f in conn.execute(
                "SELECT direccion, lat, lng FROM direcciones_conocidas"
                " WHERE ciudad=? AND direccion_norm LIKE ?"
                " ORDER BY veces DESC, actualizado_en DESC LIMIT 3",
                (ciudad, qn + "%")).fetchall()]
        cacheada = conn.execute("SELECT resultados FROM geocache WHERE clave=?",
                                (clave,)).fetchone()

    if cacheada:
        externos = json.loads(cacheada["resultados"])
    else:
        externos, fuente = None, None
        if TOMTOM_KEY:
            try:
                externos, fuente = _buscar_tomtom(qn, ciudad, lat, lng), "tomtom"
            except requests.RequestException:
                externos = None  # TomTom caído o sin cuota: probar Photon
        if not externos:
            try:
                externos, fuente = _buscar_photon(qn, ciudad, lat, lng), "photon"
            except requests.RequestException:
                externos = None
        if externos is None:
            externos = []
        else:
            # también se cachean respuestas vacías: repetir una búsqueda sin
            # resultados gastaría cuota igual
            with db() as conn:
                conn.execute(
                    "INSERT INTO geocache (clave, resultados, fuente) VALUES (?,?,?)"
                    " ON CONFLICT(clave) DO UPDATE SET resultados=excluded.resultados,"
                    " fuente=excluded.fuente, creado_en=datetime('now')",
                    (clave, json.dumps(externos, ensure_ascii=False), fuente))

    # locales primero, externos después, sin puntos duplicados
    vistos = {(round(s["lat"], 5), round(s["lng"], 5)) for s in locales}
    for e in externos:
        marca = (round(e["lat"], 5), round(e["lng"], 5))
        if marca not in vistos:
            vistos.add(marca)
            locales.append(e)
    return jsonify(locales[:6])


@app.get("/api/reportes/<rid>/foto")
def foto_reporte(rid):
    """Foto de un desaparecido o paciente. Solo de reportes visibles: al
    ocultar/eliminar un reporte su foto deja de servirse."""
    with db() as conn:
        f = conn.execute("SELECT fotos FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
    if not f or not f["fotos"]:
        return jsonify({"error": "Sin foto"}), 404
    resp = send_from_directory(FOTOS_DIR, f["fotos"])
    # no-cache: al editar y resubir una foto, se ve la nueva de inmediato
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ─── Similares y confirmaciones (validación comunitaria) ─────────────────────
#
# Antes de crear un reporte, el frontend pregunta si ya existe algo parecido
# (mismo tipo y ciudad + cercanía o nombre similar). Si la persona reconoce el
# suyo entre los similares, en vez de duplicar CONFIRMA el existente: el
# contador `confirmaciones` es la validación comunitaria del punto.

def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower())
                   if unicodedata.category(c) != "Mn")


def _parecido(a: str, b: str) -> float:
    a, b = _sin_tildes(a.strip()), _sin_tildes(b.strip())
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _dist_m(lat1, lng1, lat2, lng2) -> float:
    # aproximación plana, suficiente a escala urbana
    return 111_320 * math.hypot(lat1 - lat2, (lng1 - lng2) * math.cos(math.radians(lat1)))


@app.get("/api/reportes/similares")
def reportes_similares():
    if not _rate_ok(f"sim:{_ip()}", maximo=20, ventana_seg=60):
        return jsonify([]), 429
    tipo = request.args.get("tipo")
    ciudad = _texto(request.args.get("ciudad"), 60)
    nombre = _texto(request.args.get("nombre"), 120)
    sexo = _texto(request.args.get("sexo"), 20)
    especie = _texto(request.args.get("especie"), 30)
    try:
        edad = int(request.args.get("edad"))
    except (TypeError, ValueError):
        edad = None
    try:
        lat, lng = float(request.args.get("lat")), float(request.args.get("lng"))
    except (TypeError, ValueError):
        lat = lng = None
    if tipo not in TIPOS or not ciudad:
        return jsonify([])

    with db() as conn:
        filas = conn.execute(
            f"SELECT {COLS_PUBLICAS} FROM reportes WHERE tipo=? AND ciudad=?"
            " AND estado='visible' ORDER BY creado_en DESC LIMIT 300",
            (tipo, ciudad)).fetchall()

    similares = []
    for f in filas:
        r = _fila_publica(f)
        ex = r["extras"]
        cerca = (lat is not None and _dist_m(lat, lng, r["lat"], r["lng"]) < 150)
        if tipo == "dano":
            es = cerca
        elif tipo == "donacion":
            es = cerca or _parecido(nombre, ex.get("nombre_punto", "")) >= 0.7
        elif tipo == "desaparecido":
            # solo por nombre: dos personas distintas pueden llamarse igual,
            # pero eso lo decide quien reporta viendo la tarjeta (y la foto)
            es = _parecido(nombre, ex.get("nombre", "")) >= 0.72
        elif tipo == "mascota":
            # los nombres de mascota se repiten mucho (Max, Luna): exige
            # además misma especie o cercanía del punto de pérdida
            mismo_animal = (not especie or not ex.get("especie")
                            or _parecido(especie, ex["especie"]) >= 0.8)
            es = mismo_animal and (_parecido(nombre, ex.get("nombre_mascota", "")) >= 0.8
                                   or (cerca and not nombre))
        else:  # hospital: mismo hospital + paciente compatible
            es = (_parecido(nombre, ex.get("hospital", "")) >= 0.75
                  and (not sexo or not ex.get("sexo") or sexo == ex["sexo"])
                  and (edad is None or ex.get("edad_aprox") is None
                       or abs(edad - ex["edad_aprox"]) <= 10))
        if es:
            similares.append(r)
        if len(similares) == 3:
            break
    return jsonify(similares)


@app.post("/api/reportes/<rid>/confirmar")
def confirmar_reporte(rid):
    """Suma una confirmación comunitaria. Una por IP y reporte: repetir no
    infla el contador (responde ok con el valor vigente)."""
    if not _rate_ok(f"conf:{_ip()}", maximo=10, ventana_seg=3600):
        return jsonify({"error": "Demasiadas confirmaciones; intenta más tarde"}), 429
    with db() as conn:
        f = conn.execute("SELECT confirmaciones FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f:
            return jsonify({"error": "Reporte no encontrado"}), 404
        nuevo = conn.execute(
            "INSERT INTO confirmaciones_log (reporte_id, ip, user_agent) VALUES (?,?,?)"
            " ON CONFLICT(reporte_id, ip) DO NOTHING",
            (rid, _ip(), _texto(request.headers.get("User-Agent"), 300))).rowcount
        if nuevo:
            conn.execute("UPDATE reportes SET confirmaciones=confirmaciones+1 WHERE id=?", (rid,))
        total = conn.execute("SELECT confirmaciones FROM reportes WHERE id=?", (rid,)).fetchone()[0]
    return jsonify({"ok": True, "confirmaciones": total})


@app.get("/api/reportes/<rid>/avistamientos")
def listar_avistamientos(rid):
    with db() as conn:
        filas = conn.execute(
            "SELECT id, nota, COALESCE(evento,'nota') AS evento, foto, creado_en"
            " FROM avistamientos WHERE reporte_id=?"
            " ORDER BY creado_en DESC, id DESC LIMIT 50", (rid,)).fetchall()
    return jsonify([dict(f) for f in filas])


@app.post("/api/reportes/<rid>/avistamientos")
def crear_avistamiento(rid):
    """Tracker comunitario. En mascotas y desaparecidos son avistamientos
    ("lo vi en el parque X a las 3pm"); en daños y ayudas son actualizaciones
    de la zona/punto ("ya removieron los escombros", "seguimos recibiendo")."""
    if not _rate_ok(f"avi:{_ip()}", maximo=10, ventana_seg=3600):
        return jsonify({"error": "Demasiados aportes; intenta más tarde"}), 429
    data = request.get_json(silent=True) or {}
    nota = _texto(data.get("nota"), 300)
    if len(nota) < 5:
        return jsonify({"error": "Cuéntanos qué viste, dónde y cuándo"}), 400
    # foto opcional adjunta a la nota: mismo pipeline que las fotos de reportes,
    # con nombre propio (av-<uuid>) para no pisar la foto del reporte
    foto_nombre = None
    if data.get("foto"):
        fila_foto = {"id": "av-" + uuid.uuid4().hex, "fotos": None}
        err = _guardar_foto(fila_foto, data["foto"])
        if err:
            return jsonify({"error": err}), 400
        foto_nombre = fila_foto["fotos"]
    with db() as conn:
        f = conn.execute("SELECT tipo, resuelto FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f or f["tipo"] not in ("mascota", "desaparecido", "dano", "donacion"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        conn.execute("INSERT INTO avistamientos (reporte_id, nota, foto, ip, user_agent)"
                     " VALUES (?,?,?,?,?)",
                     (rid, nota, foto_nombre, _ip(),
                      _texto(request.headers.get("User-Agent"), 300)))
        total = conn.execute("SELECT COUNT(*) FROM avistamientos WHERE reporte_id=?",
                             (rid,)).fetchone()[0]
    return jsonify({"ok": True, "avistamientos": total})


@app.get("/api/actualizaciones")
def actualizaciones():
    """Feed global del mapa: la actividad más reciente de todos los puntos
    (notas, confirmaciones de vigencia, gente, necesidades y reportes
    nuevos). Solo columnas públicas — jamás teléfonos."""
    with db() as conn:
        eventos = [dict(f) for f in conn.execute(
            "SELECT a.reporte_id, a.nota, COALESCE(a.evento,'nota') AS evento,"
            " a.creado_en, r.tipo, r.ciudad, r.direccion, r.extras"
            " FROM avistamientos a JOIN reportes r ON r.id=a.reporte_id"
            " WHERE r.estado='visible'"
            " ORDER BY a.creado_en DESC, a.id DESC LIMIT 20")]
        eventos += [dict(f) for f in conn.execute(
            "SELECT id AS reporte_id, NULL AS nota, 'creado' AS evento,"
            " creado_en, tipo, ciudad, direccion, extras"
            " FROM reportes WHERE estado='visible'"
            " ORDER BY creado_en DESC LIMIT 10")]
    for e in eventos:
        e["extras"] = json.loads(e.get("extras") or "{}")
    eventos.sort(key=lambda e: e["creado_en"], reverse=True)
    return jsonify(eventos[:20])


# presencia anónima en memoria: suficiente con 1 worker de gunicorn (deploy)
_presencia: dict[str, float] = {}


@app.post("/api/presencia")
def presencia():
    """Latido de presencia: el cliente hace ping cada minuto y esto devuelve
    cuántas IPs distintas dieron señales en los últimos 5 minutos."""
    ahora = time.monotonic()
    _presencia[_ip()] = ahora
    for ip, t in list(_presencia.items()):
        if ahora - t > 300:
            _presencia.pop(ip, None)
    return jsonify({"en_linea": len(_presencia)})


@app.get("/api/avistamientos/<int:aid>/foto")
def foto_avistamiento(aid):
    """Foto adjunta a una nota de la bitácora. Solo si el reporte padre sigue
    visible: ocultar el reporte oculta también sus fotos de bitácora."""
    with db() as conn:
        f = conn.execute(
            "SELECT a.foto FROM avistamientos a JOIN reportes r ON r.id=a.reporte_id"
            " WHERE a.id=? AND r.estado='visible'", (aid,)).fetchone()
    if not f or not f["foto"]:
        return jsonify({"error": "Sin foto"}), 404
    return send_from_directory(FOTOS_DIR, f["foto"])


@app.post("/api/reportes/<rid>/necesidades")
def actualizar_necesidades(rid):
    """'Qué se necesita' vivo (daños y ayudas): cualquiera agrega o quita
    necesidades según cambie la situación del punto, sin crear reportes
    nuevos. Cada cambio real queda en la bitácora y refresca la vigencia."""
    if not _rate_ok(f"nec:{_ip()}", maximo=20, ventana_seg=3600):
        return jsonify({"error": "Demasiados cambios; intenta más tarde"}), 429
    data = request.get_json(silent=True) or {}
    accion = data.get("accion")
    valor = _texto(data.get("valor"), 40)
    if accion not in ("agregar", "quitar") or len(valor) < 2:
        return jsonify({"error": "Cambio no válido"}), 400
    with db() as conn:
        f = conn.execute("SELECT tipo, extras FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f or f["tipo"] not in ("dano", "donacion"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        extras = json.loads(f["extras"] or "{}")
        lista = list(extras.get("necesidades") or [])
        cambiado = False
        if accion == "agregar":
            if len(lista) >= 20:
                return jsonify({"error": "Máximo 20 necesidades por punto"}), 400
            if valor not in lista:
                lista.append(valor)
                cambiado = True
        elif valor in lista:
            lista.remove(valor)
            cambiado = True
        elif f["tipo"] == "dano" and valor in (extras.get("ayuda_tipos") or []):
            # las necesidades marcadas al crear el daño también se pueden quitar
            extras["ayuda_tipos"] = [t for t in extras["ayuda_tipos"] if t != valor]
            cambiado = True
        if cambiado:
            extras["necesidades"] = lista
            if f["tipo"] == "dano":
                extras["necesita_ayuda"] = bool(lista or extras.get("ayuda_tipos"))
            conn.execute("UPDATE reportes SET extras=?, vigente_en=datetime('now')"
                         " WHERE id=?", (json.dumps(extras, ensure_ascii=False), rid))
            resumen = ", ".join((extras.get("ayuda_tipos") or []) + lista) or "nada por ahora"
            conn.execute("INSERT INTO avistamientos (reporte_id, nota, evento, ip, user_agent)"
                         " VALUES (?,?,'necesidades',?,?)",
                         (rid, f"Se necesita: {resumen}", _ip(),
                          _texto(request.headers.get("User-Agent"), 300)))
    return jsonify({"ok": True, "extras": extras})


@app.post("/api/reportes/<rid>/gente")
def actualizar_gente(rid):
    """Coordinación de voluntarios en el punto (daños y ayudas): cuántas
    personas están ayudando ahora y cuántas manos faltan. El cliente edita el
    contador libremente y envía UN solo valor final (nada de un POST por
    toque): una entrada de bitácora por cambio real. De aquí salen los avisos
    "no acudir, ya hay N" / "faltan N, ve" que reparten a la gente."""
    if not _rate_ok(f"gente:{_ip()}", maximo=30, ventana_seg=3600):
        return jsonify({"error": "Demasiados cambios; intenta más tarde"}), 429
    data = request.get_json(silent=True) or {}
    campo, valor = data.get("campo"), data.get("valor")
    if (campo not in ("ayudando", "faltan") or isinstance(valor, bool)
            or not isinstance(valor, int) or not 0 <= valor <= 9999):
        return jsonify({"error": "Cambio no válido"}), 400
    with db() as conn:
        f = conn.execute("SELECT tipo, ayudando, faltan FROM reportes"
                         " WHERE id=? AND estado='visible'", (rid,)).fetchone()
        if not f or f["tipo"] not in ("dano", "donacion"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        if valor != (f[campo] or 0):
            if campo == "ayudando":
                # la gente que llega cubre manos que faltaban (y la que se va
                # las vuelve a dejar pendientes): el total necesario se conserva
                delta = valor - (f["ayudando"] or 0)
                faltan = max(0, min(9999, (f["faltan"] or 0) - delta))
                conn.execute("UPDATE reportes SET ayudando=?, faltan=?,"
                             " vigente_en=datetime('now') WHERE id=?",
                             (valor, faltan, rid))
                texto = f"Personas ayudando ahora: {valor} · manos que faltan: {faltan}"
            else:
                conn.execute("UPDATE reportes SET faltan=?, vigente_en=datetime('now')"
                             " WHERE id=?", (valor, rid))
                texto = f"Manos que faltan: {valor}"
            conn.execute("INSERT INTO avistamientos (reporte_id, nota, evento, ip, user_agent)"
                         " VALUES (?,?,'gente',?,?)",
                         (rid, texto, _ip(), _texto(request.headers.get("User-Agent"), 300)))
        fila = conn.execute("SELECT ayudando, faltan, vigente_en FROM reportes WHERE id=?",
                            (rid,)).fetchone()
    return jsonify({"ok": True, **dict(fila)})


@app.post("/api/reportes/<rid>/vigente")
def marcar_vigente(rid):
    """'Sigo aquí, esto sigue vigente': confirmación de frescura con un toque.
    Actualiza vigente_en (de ahí sale el "hace X min" y el apagado visual de
    los puntos viejos) y deja constancia en la bitácora."""
    if not _rate_ok(f"vig:{_ip()}", maximo=10, ventana_seg=3600):
        return jsonify({"error": "Demasiadas confirmaciones; intenta más tarde"}), 429
    with db() as conn:
        f = conn.execute("SELECT tipo FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f or f["tipo"] not in ("dano", "donacion"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        conn.execute("UPDATE reportes SET vigente_en=datetime('now') WHERE id=?", (rid,))
        conn.execute("INSERT INTO avistamientos (reporte_id, nota, evento, ip, user_agent)"
                     " VALUES (?,?,'vigente',?,?)",
                     (rid, "Confirmado en el punto: sigue vigente.",
                      _ip(), _texto(request.headers.get("User-Agent"), 300)))
        vigente = conn.execute("SELECT vigente_en FROM reportes WHERE id=?",
                               (rid,)).fetchone()[0]
    return jsonify({"ok": True, "vigente_en": vigente})


@app.post("/api/reportes/<rid>/encontrado")
def marcar_encontrado(rid):
    """Cierra el ciclo con la buena noticia: marca un desaparecido, paciente
    o mascota como encontrado/reunido; con desenlace='fallecido' (solo
    personas) el cierre es el triste. El reporte NO se borra: queda visible
    con la insignia y el comentario, que es información para todos los que lo
    buscaban. Auditado y reversible desde el panel admin."""
    if not _rate_ok(f"enc:{_ip()}", maximo=5, ventana_seg=3600):
        return jsonify({"error": "Demasiadas marcas; intenta más tarde"}), 429
    data = request.get_json(silent=True) or {}
    comentario = _texto(data.get("comentario"), 500)
    fallecido = data.get("desenlace") == "fallecido"
    if len(comentario) < 5:
        return jsonify({"error": "Cuéntanos brevemente cómo o dónde apareció"}), 400
    with db() as conn:
        f = conn.execute("SELECT tipo, resuelto FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f or f["tipo"] not in ("desaparecido", "hospital", "mascota"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        if fallecido and f["tipo"] not in ("desaparecido", "hospital"):
            return jsonify({"error": "Reporte no encontrado"}), 404
        conn.execute("INSERT INTO resoluciones_log (reporte_id, comentario, ip, user_agent)"
                     " VALUES (?,?,?,?)",
                     (rid, comentario, _ip(), _texto(request.headers.get("User-Agent"), 300)))
        # si ya estaba marcado, el comentario nuevo se suma al log pero el
        # público conserva el primero (el admin ve todos)
        if not f["resuelto"]:
            conn.execute("UPDATE reportes SET resuelto=?, resuelto_comentario=?,"
                         " resuelto_en=datetime('now') WHERE id=?",
                         (2 if fallecido else 1, comentario, rid))
    return jsonify({"ok": True})


@app.post("/api/reportes/<rid>/editar")
def editar_reporte(rid):
    """Edición pública: quien reportó (o quien nota un error) puede corregir
    el nombre/dirección/descripción y RESUBIR la foto. En la emergencia prima
    poder mantener la información al día; el panel admin sigue pudiendo ocultar
    abusos. Solo campos de texto acotados + foto (todo saneado)."""
    if not _rate_ok(f"edit:{_ip()}", maximo=20, ventana_seg=3600):
        return jsonify({"error": "Demasiadas ediciones; intenta más tarde"}), 429
    data = request.get_json(silent=True) or {}
    with db() as conn:
        f = conn.execute("SELECT * FROM reportes WHERE id=? AND estado='visible'",
                         (rid,)).fetchone()
        if not f:
            return jsonify({"error": "Reporte no encontrado"}), 404

        campos = {}
        if "direccion" in data:
            campos["direccion"] = _texto(data.get("direccion"), 200)
        if "descripcion" in data:
            campos["descripcion"] = _texto(data.get("descripcion"), 1000)
        # nombre principal según el tipo (va en extras)
        if "nombre" in data:
            ex = json.loads(f["extras"] or "{}")
            clave = {"desaparecido": "nombre", "mascota": "nombre_mascota",
                     "hospital": "hospital", "donacion": "nombre_punto"}.get(f["tipo"])
            if clave:
                ex[clave] = _texto(data.get("nombre"), 120)
                campos["extras"] = json.dumps(ex, ensure_ascii=False)
        # foto nueva (reemplaza la anterior)
        if data.get("foto"):
            fila = {"id": rid, "tipo": f["tipo"], "fotos": f["fotos"]}
            err = _guardar_foto(fila, data.get("foto"))
            if err:
                return jsonify({"error": err}), 400
            campos["fotos"] = fila["fotos"]

        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            conn.execute(f"UPDATE reportes SET {sets} WHERE id=?",
                         (*campos.values(), rid))
    return jsonify({"ok": True})


@app.get("/api/metricas")
def metricas():
    with db() as conn:
        # los encontrados no cuentan como activos: la métrica mide lo pendiente
        por_tipo = {t: 0 for t in TIPOS}
        for f in conn.execute("SELECT tipo, COUNT(*) n FROM reportes"
                              " WHERE estado='visible' AND resuelto=0 GROUP BY tipo"):
            por_tipo[f["tipo"]] = f["n"]
        encontrados = conn.execute("SELECT COUNT(*) FROM reportes"
                                   " WHERE estado='visible' AND resuelto=1").fetchone()[0]
        # ayudas divididas por subtipo (json_extract, sin columna nueva)
        ayuda = conn.execute(
            "SELECT"
            " SUM(CASE WHEN json_extract(extras,'$.subtipo')='refugio' THEN 1 ELSE 0 END) refugios,"
            " SUM(CASE WHEN COALESCE(json_extract(extras,'$.subtipo'),'acopio')!='refugio' THEN 1 ELSE 0 END) acopios"
            " FROM reportes WHERE estado='visible' AND resuelto=0 AND tipo='donacion'").fetchone()
        por_ciudad = [dict(f) for f in conn.execute(
            "SELECT departamento, ciudad, COUNT(*) n FROM reportes"
            " WHERE estado='visible' GROUP BY departamento, ciudad ORDER BY n DESC")]
        total = conn.execute("SELECT COUNT(*) FROM reportes"
                             " WHERE estado='visible'").fetchone()[0]
        faltan_gente = conn.execute("SELECT COUNT(*) FROM reportes"
                                    " WHERE estado='visible' AND faltan>0").fetchone()[0]
    return jsonify({"por_tipo": por_tipo, "por_ciudad": por_ciudad,
                    "total": total, "faltan_gente": faltan_gente,
                    "encontrados": encontrados,
                    "acopios": ayuda["acopios"] or 0, "refugios": ayuda["refugios"] or 0})


@app.get("/static/<path:fname>")
def estaticos(fname):
    return send_from_directory(BASE_DIR / "static", fname)


# ─── Endpoints admin (en producción solo alcanzables por VPN) ─────────────────

def _filtros_admin() -> tuple[str, list]:
    filtros, params = ["1=1"], []
    if request.args.get("tipo") in TIPOS:
        filtros.append("tipo=?")
        params.append(request.args["tipo"])
    if request.args.get("estado") in ("visible", "oculto", "eliminado"):
        filtros.append("estado=?")
        params.append(request.args["estado"])
    for campo in ("departamento", "ciudad"):
        if request.args.get(campo):
            filtros.append(f"{campo}=?")
            params.append(request.args[campo])
    return " AND ".join(filtros), params


@app.get("/api/admin/reportes")
@auth.requiere_login(db)
def admin_reportes():
    where, params = _filtros_admin()
    with db() as conn:
        filas = conn.execute(f"SELECT * FROM reportes WHERE {where}"
                             " ORDER BY creado_en DESC LIMIT 5000", params).fetchall()
    out = []
    for f in filas:
        d = dict(f)
        d["extras"] = json.loads(d.get("extras") or "{}")
        out.append(d)
    return jsonify(out)


@app.patch("/api/admin/reportes/<rid>")
@auth.requiere_login(db)
def admin_moderar(rid):
    from flask import g
    cuerpo = request.get_json(silent=True) or {}
    if "resuelto" in cuerpo:
        # reabrir (o cerrar) un caso: revierte marcas de 'encontrado' falsas
        with db() as conn:
            n = conn.execute("UPDATE reportes SET resuelto=?, moderado_en=datetime('now'),"
                             " moderado_por=? WHERE id=? AND estado != 'eliminado'",
                             (1 if cuerpo["resuelto"] else 0, g.usuario["username"], rid)).rowcount
        return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)
    estado = cuerpo.get("estado")
    if estado not in ("visible", "oculto"):
        return jsonify({"error": "estado debe ser 'visible' u 'oculto'"}), 400
    with db() as conn:
        n = conn.execute("UPDATE reportes SET estado=?, moderado_en=datetime('now'),"
                         " moderado_por=? WHERE id=? AND estado != 'eliminado'",
                         (estado, g.usuario["username"], rid)).rowcount
    return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)


@app.delete("/api/admin/reportes/<rid>")
@auth.requiere_admin(db)
def admin_eliminar(rid):
    from flask import g
    # soft delete: el reporte sale de todo listado pero queda en la BD como evidencia
    with db() as conn:
        n = conn.execute("UPDATE reportes SET estado='eliminado', moderado_en=datetime('now'),"
                         " moderado_por=? WHERE id=?", (g.usuario["username"], rid)).rowcount
    return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)


@app.get("/api/admin/accesos-telefono")
@auth.requiere_login(db)
def admin_accesos():
    with db() as conn:
        filas = conn.execute(
            "SELECT a.*, r.extras AS reporte_extras FROM accesos_telefono a"
            " LEFT JOIN reportes r ON r.id = a.reporte_id"
            " ORDER BY a.creado_en DESC LIMIT 1000").fetchall()
    out = []
    for f in filas:
        d = dict(f)
        extras = json.loads(d.pop("reporte_extras") or "{}")
        # nombre del desaparecido, o del hospital si es un paciente sin identificar
        d["desaparecido"] = (extras.get("nombre") or extras.get("hospital")
                             or extras.get("nombre_mascota", ""))
        out.append(d)
    return jsonify(out)


@app.get("/api/admin/avistamientos")
@auth.requiere_login(db)
def admin_avistamientos():
    """Todas las notas comunitarias (avistamientos y actualizaciones de zona)
    con el reporte al que pertenecen, para moderar abusos."""
    with db() as conn:
        filas = conn.execute(
            "SELECT a.id, a.reporte_id, a.nota, a.ip, a.creado_en,"
            " r.tipo, r.ciudad, r.estado, r.extras"
            " FROM avistamientos a LEFT JOIN reportes r ON r.id = a.reporte_id"
            " ORDER BY a.creado_en DESC LIMIT 1000").fetchall()
    out = []
    for f in filas:
        d = dict(f)
        extras = json.loads(d.pop("extras") or "{}")
        d["reporte"] = (extras.get("nombre") or extras.get("nombre_mascota")
                        or extras.get("hospital")
                        or f"{d.get('tipo') or 'reporte'} · {d.get('ciudad') or ''}".strip(" ·"))
        out.append(d)
    return jsonify(out)


@app.patch("/api/admin/avistamientos/<int:aid>")
@auth.requiere_login(db)
def admin_editar_avistamiento(aid):
    nota = _texto((request.get_json(silent=True) or {}).get("nota"), 300)
    if not nota:
        return jsonify({"error": "La nota no puede quedar vacía"}), 400
    with db() as conn:
        n = conn.execute("UPDATE avistamientos SET nota=? WHERE id=?", (nota, aid)).rowcount
    return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)


@app.delete("/api/admin/avistamientos/<int:aid>")
@auth.requiere_login(db)
def admin_borrar_avistamiento(aid):
    with db() as conn:
        n = conn.execute("DELETE FROM avistamientos WHERE id=?", (aid,)).rowcount
    return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)


@app.get("/api/admin/exportar.csv")
@auth.requiere_login(db)
def admin_exportar():
    where, params = _filtros_admin()
    with db() as conn:
        filas = conn.execute(f"SELECT * FROM reportes WHERE {where}"
                             " ORDER BY creado_en DESC", params).fetchall()
    buf = io.StringIO()
    campos_extras = ["severidad", "personas_atrapadas", "necesita_ayuda",
                     "ayuda_tipos", "ayuda_detalle", "nombre", "edad",
                     "descripcion_fisica", "visto_ultima_vez", "nombre_punto",
                     "necesidades", "horario", "hospital", "reportante_nombre",
                     "reportante_cargo", "sexo", "edad_aprox", "estado_salud",
                     "senas_particulares", "ropa", "fecha_ingreso",
                     "nombre_mascota", "especie", "raza", "situacion",
                     "subtipo", "capacidad", "servicios", "admite_mascotas", "telefono"]
    w = csv.writer(buf, delimiter=";")  # ';' — Excel es-CO
    w.writerow(["id", "tipo", "departamento", "ciudad", "direccion", "lat", "lng",
                "ubicacion_ajustada", "descripcion", "telefono_contacto", "estado",
                "canal", "creado_en", "moderado_en", "moderado_por"] + campos_extras)
    for f in filas:
        extras = json.loads(f["extras"] or "{}")
        for lista in ("necesidades", "ayuda_tipos", "servicios"):
            if isinstance(extras.get(lista), list):
                extras[lista] = ", ".join(extras[lista])
        w.writerow([f["id"], f["tipo"], f["departamento"], f["ciudad"], f["direccion"],
                    f["lat"], f["lng"], f["ubicacion_ajustada"], f["descripcion"],
                    f["telefono_contacto"], f["estado"], f["canal"], f["creado_en"],
                    f["moderado_en"], f["moderado_por"]]
                   + [extras.get(c, "") for c in campos_extras])
    # BOM para que Excel abra el UTF-8 con tildes correctas
    return "\ufeff" + buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": "attachment; filename=reportes-sos.csv",
    }


# ─── Entrypoint dev ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8085)
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port, debug=False)
