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
import io
import json
import os
import re
import sqlite3
import time
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

TIPOS = ("dano", "desaparecido", "donacion", "hospital")
SEVERIDADES = ("leve", "moderado", "grave", "colapso")
NECESIDADES = ("agua", "alimentos", "medicamentos", "ropa", "cobijas", "aseo", "otros")
TIPOS_AYUDA = ("herramientas", "maquinaria", "personas")

# ─── SQLite ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS reportes (
  id                 TEXT PRIMARY KEY,          -- UUID generado en el cliente (idempotencia de la cola offline)
  tipo               TEXT NOT NULL CHECK (tipo IN ('dano','desaparecido','donacion','hospital')),
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
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                           " AND name='reportes'").fetchone()[0]
        if "'hospital'" not in sql:
            conn.executescript(
                "ALTER TABLE reportes RENAME TO reportes_v1;"
                + SCHEMA
                + "INSERT INTO reportes SELECT * FROM reportes_v1;"
                  "DROP TABLE reportes_v1;")
            # el RENAME se llevó los índices idx_rep_* y el DROP los eliminó:
            # segunda pasada del SCHEMA para recrearlos sobre la tabla nueva
            conn.executescript(SCHEMA)
        # el caché de geocoding no necesita vivir para siempre
        conn.execute("DELETE FROM geocache WHERE creado_en < datetime('now','-90 days')")


init_db()
auth.registrar_endpoints(app, db)

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


@app.route("/api/<path:_>", methods=["OPTIONS"])
def cors_preflight(_):
    return "", 204


# ─── Rate limit en memoria ────────────────────────────────────────────────────
#
# Suficiente con un solo worker de gunicorn (convención de todos los servicios
# del homelab). No sustituye la revisión de seguridad pendiente: es la primera
# barrera contra spam masivo, no contra un atacante dedicado.

_hits: dict[str, deque] = defaultdict(deque)


def _rate_ok(clave: str, maximo: int, ventana_seg: int) -> bool:
    ahora = time.monotonic()
    cola = _hits[clave]
    while cola and ahora - cola[0] > ventana_seg:
        cola.popleft()
    if len(cola) >= maximo:
        return False
    cola.append(ahora)
    return True


def _ip() -> str:
    # Caddy antepone X-Forwarded-For; en local es la IP directa
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


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
    if len(rid) < 8:
        return None, "Falta el id del reporte"

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

    return {
        "id": rid, "tipo": tipo, "departamento": depto, "ciudad": ciudad,
        "direccion": _texto(data.get("direccion"), 200),
        "lat": round(lat, 6), "lng": round(lng, 6), "ubicacion_ajustada": ajustada,
        "descripcion": _texto(data.get("descripcion"), 1000),
        "extras": json.dumps(extras, ensure_ascii=False),
        "telefono_contacto": telefono, "canal": canal, "fotos": None,
    }, None


def _guardar_foto(fila: dict, foto) -> str | None:
    """Guarda la foto (data URL base64) en disco y anota el nombre en la fila.
    Devuelve un mensaje de error o None. Solo aplica a personas (desaparecidos
    y pacientes de hospital); en otros tipos se ignora sin error."""
    if not foto or fila["tipo"] not in ("desaparecido", "hospital"):
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
    nombre = f"{fila['id']}.{ext}"
    (FOTOS_DIR / nombre).write_bytes(crudo)
    fila["fotos"] = nombre
    return None


# Columnas públicas: telefono_contacto queda EXCLUIDO a propósito — el SELECT
# es explícito para que nunca se filtre por un futuro SELECT *.
COLS_PUBLICAS = ("id, tipo, departamento, ciudad, direccion, lat, lng,"
                 " ubicacion_ajustada, descripcion, extras, fotos, canal, creado_en")


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
                " ubicacion_ajustada, descripcion, extras, telefono_contacto, canal, fotos)"
                " VALUES (:id, :tipo, :departamento, :ciudad, :direccion, :lat, :lng,"
                " :ubicacion_ajustada, :descripcion, :extras, :telefono_contacto, :canal, :fotos)",
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
    if request.args.get("desde"):
        filtros.append("creado_en >= ?")
        params.append(request.args["desde"])
    if request.args.get("hasta"):
        filtros.append("creado_en <= ?")
        params.append(request.args["hasta"] + " 23:59:59")
    with db() as conn:
        filas = conn.execute(
            f"SELECT {COLS_PUBLICAS} FROM reportes WHERE {' AND '.join(filtros)}"
            " ORDER BY creado_en DESC LIMIT 2000", params).fetchall()
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
                         " WHERE id=? AND tipo IN ('desaparecido','hospital')"
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
    return send_from_directory(FOTOS_DIR, f["fotos"])


@app.get("/api/metricas")
def metricas():
    with db() as conn:
        por_tipo = {t: 0 for t in TIPOS}
        for f in conn.execute("SELECT tipo, COUNT(*) n FROM reportes"
                              " WHERE estado='visible' GROUP BY tipo"):
            por_tipo[f["tipo"]] = f["n"]
        por_ciudad = [dict(f) for f in conn.execute(
            "SELECT departamento, ciudad, COUNT(*) n FROM reportes"
            " WHERE estado='visible' GROUP BY departamento, ciudad ORDER BY n DESC")]
        ult24 = conn.execute("SELECT COUNT(*) FROM reportes WHERE estado='visible'"
                             " AND creado_en >= datetime('now','-1 day')").fetchone()[0]
    return jsonify({"por_tipo": por_tipo, "por_ciudad": por_ciudad, "ultimas_24h": ult24})


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
    estado = (request.get_json(silent=True) or {}).get("estado")
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
        d["desaparecido"] = extras.get("nombre") or extras.get("hospital", "")
        out.append(d)
    return jsonify(out)


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
                     "senas_particulares", "ropa", "fecha_ingreso"]
    w = csv.writer(buf, delimiter=";")  # ';' — Excel es-CO
    w.writerow(["id", "tipo", "departamento", "ciudad", "direccion", "lat", "lng",
                "ubicacion_ajustada", "descripcion", "telefono_contacto", "estado",
                "canal", "creado_en", "moderado_en", "moderado_por"] + campos_extras)
    for f in filas:
        extras = json.loads(f["extras"] or "{}")
        for lista in ("necesidades", "ayuda_tipos"):
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
