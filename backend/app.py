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
import csv
import io
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

import auth
import catalogo

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ.get("SISMO_DB", BASE_DIR / "data" / "sismo.db"))

app = Flask(__name__)

TIPOS = ("dano", "desaparecido", "donacion")
SEVERIDADES = ("leve", "moderado", "grave", "colapso")
NECESIDADES = ("agua", "alimentos", "medicamentos", "ropa", "cobijas", "aseo", "otros")

# ─── SQLite ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS reportes (
  id                 TEXT PRIMARY KEY,          -- UUID generado en el cliente (idempotencia de la cola offline)
  tipo               TEXT NOT NULL CHECK (tipo IN ('dano','desaparecido','donacion')),
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

    elif tipo == "donacion":
        pedidas = extras_in.get("necesidades")
        if not isinstance(pedidas, list):
            return None, "necesidades debe ser una lista"
        necesidades = [n for n in pedidas if n in NECESIDADES]
        if not necesidades:
            return None, "Indica al menos un insumo necesario"
        extras["necesidades"] = sorted(set(necesidades), key=NECESIDADES.index)
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
        "telefono_contacto": telefono, "canal": canal,
    }, None


# Columnas públicas: telefono_contacto queda EXCLUIDO a propósito — el SELECT
# es explícito para que nunca se filtre por un futuro SELECT *.
COLS_PUBLICAS = ("id, tipo, departamento, ciudad, direccion, lat, lng,"
                 " ubicacion_ajustada, descripcion, extras, canal, creado_en")


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

    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO reportes (id, tipo, departamento, ciudad, direccion, lat, lng,"
                " ubicacion_ajustada, descripcion, extras, telefono_contacto, canal)"
                " VALUES (:id, :tipo, :departamento, :ciudad, :direccion, :lat, :lng,"
                " :ubicacion_ajustada, :descripcion, :extras, :telefono_contacto, :canal)",
                fila)
    except sqlite3.IntegrityError:
        # reintento de la cola offline: el servidor ya lo tiene → el cliente lo desencola
        return jsonify({"ok": True, "duplicado": True}), 409
    return jsonify({"ok": True, "id": fila["id"]}), 201


@app.get("/api/reportes")
def listar_reportes():
    filtros, params = ["estado='visible'"], []
    if request.args.get("tipo") in TIPOS:
        filtros.append("tipo=?")
        params.append(request.args["tipo"])
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
    """Entrega el teléfono de contacto de un desaparecido a quien declara
    tener información. Cada entrega queda registrada (auditoría antiacoso)."""
    if not _rate_ok(f"contacto:{_ip()}", maximo=5, ventana_seg=3600):
        return jsonify({"error": "Demasiadas consultas; intenta más tarde"}), 429

    data = request.get_json(silent=True) or {}
    with db() as conn:
        f = conn.execute("SELECT telefono_contacto FROM reportes"
                         " WHERE id=? AND tipo='desaparecido' AND estado='visible'",
                         (rid,)).fetchone()
        if not f or not f["telefono_contacto"]:
            return jsonify({"error": "Reporte no encontrado"}), 404
        conn.execute(
            "INSERT INTO accesos_telefono (reporte_id, nombre_solicitante, ip, user_agent)"
            " VALUES (?,?,?,?)",
            (rid, _texto(data.get("nombre_solicitante"), 120), _ip(),
             _texto(request.headers.get("User-Agent"), 300)))
    return jsonify({"telefono_contacto": f["telefono_contacto"]})


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
        d["desaparecido"] = extras.get("nombre", "")
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
    campos_extras = ["severidad", "personas_atrapadas", "nombre", "edad",
                     "descripcion_fisica", "visto_ultima_vez", "nombre_punto",
                     "necesidades", "horario"]
    w = csv.writer(buf, delimiter=";")  # ';' — Excel es-CO
    w.writerow(["id", "tipo", "departamento", "ciudad", "direccion", "lat", "lng",
                "ubicacion_ajustada", "descripcion", "telefono_contacto", "estado",
                "canal", "creado_en", "moderado_en", "moderado_por"] + campos_extras)
    for f in filas:
        extras = json.loads(f["extras"] or "{}")
        if isinstance(extras.get("necesidades"), list):
            extras["necesidades"] = ", ".join(extras["necesidades"])
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
