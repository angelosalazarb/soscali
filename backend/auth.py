"""
Autenticación del panel admin — mismo esquema del CompraventaDashboard:
bcrypt para contraseñas + JWT HS256 (8 h) + roles admin/usuario +
`token_version` para invalidar sesiones al cambiar contraseña o desactivar.

El secreto vive SOLO en la variable de entorno JWT_SECRET_KEY (.env).
Generar uno:  python -c "import secrets; print(secrets.token_hex(48))"

Gestión de usuarios por CLI (también hay endpoints admin):
  python auth.py crear <username> [--admin]
  python auth.py password <username>
  python auth.py activar|desactivar <username>
  python auth.py listar
"""

from __future__ import annotations

import functools
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from flask import g, jsonify, request

ALG = "HS256"
HORAS_SESION = int(os.environ.get("ACCESS_TOKEN_EXPIRE_HOURS", "8"))

SCHEMA_USUARIOS = """
CREATE TABLE IF NOT EXISTS usuarios (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  credencial    TEXT NOT NULL DEFAULT 'usuario',   -- admin | usuario
  token_version INTEGER NOT NULL DEFAULT 0,
  activo        INTEGER NOT NULL DEFAULT 1,
  creado_en     TEXT DEFAULT (datetime('now'))
);
"""


def _secret() -> str:
    s = os.environ.get("JWT_SECRET_KEY", "")
    if not s:
        raise RuntimeError(
            "JWT_SECRET_KEY no está definido en .env — genera uno con: "
            "python -c \"import secrets; print(secrets.token_hex(48))\""
        )
    return s


# ─── Contraseñas ──────────────────────────────────────────────────────────────

def hash_password(plano: str) -> str:
    return bcrypt.hashpw(plano.encode(), bcrypt.gensalt()).decode()


def verify_password(plano: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plano.encode(), hashed.encode())
    except ValueError:
        return False


# ─── Tokens ───────────────────────────────────────────────────────────────────

def crear_token(u: sqlite3.Row) -> str:
    payload = {
        "sub": u["username"],
        "uid": u["id"],
        "cred": u["credencial"],
        "tv": u["token_version"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=HORAS_SESION),
    }
    return jwt.encode(payload, _secret(), algorithm=ALG)


def _validar_request(db) -> tuple[dict | None, str | None]:
    """Devuelve (usuario, None) o (None, motivo)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "Falta el token"
    try:
        payload = jwt.decode(header[7:], _secret(), algorithms=[ALG])
    except jwt.ExpiredSignatureError:
        return None, "Sesión expirada"
    except jwt.InvalidTokenError:
        return None, "Token inválido"
    with db() as conn:
        u = conn.execute("SELECT * FROM usuarios WHERE id=?", (payload.get("uid"),)).fetchone()
    if not u or not u["activo"]:
        return None, "Usuario inactivo"
    if u["token_version"] != payload.get("tv"):
        return None, "Sesión revocada"
    return {"id": u["id"], "username": u["username"], "credencial": u["credencial"]}, None


# ─── Decoradores ──────────────────────────────────────────────────────────────

def requiere_login(db):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            usuario, motivo = _validar_request(db)
            if not usuario:
                return jsonify({"error": motivo}), 401
            g.usuario = usuario
            return fn(*args, **kwargs)
        return wrapper
    return deco


def requiere_admin(db):
    def deco(fn):
        @functools.wraps(fn)
        @requiere_login(db)
        def wrapper(*args, **kwargs):
            if g.usuario["credencial"] != "admin":
                return jsonify({"error": "Requiere rol admin"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


# ─── Endpoints ────────────────────────────────────────────────────────────────

def registrar_endpoints(app, db):
    """Registra /api/auth/* sobre la app Flask. `db` = factory de conexiones."""

    with db() as conn:
        conn.executescript(SCHEMA_USUARIOS)

    @app.post("/api/auth/login")
    def login():
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip().lower()
        password = str(data.get("password") or "")
        with db() as conn:
            u = conn.execute("SELECT * FROM usuarios WHERE username=?", (username,)).fetchone()
        if not u or not u["activo"] or not verify_password(password, u["password_hash"]):
            return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
        return jsonify({"token": crear_token(u), "username": u["username"],
                        "credencial": u["credencial"]})

    @app.get("/api/auth/me")
    @requiere_login(db)
    def me():
        return jsonify(g.usuario)

    @app.get("/api/auth/usuarios")
    @requiere_admin(db)
    def listar_usuarios():
        with db() as conn:
            filas = conn.execute(
                "SELECT id, username, credencial, activo, creado_en FROM usuarios ORDER BY id"
            ).fetchall()
        return jsonify([dict(f) for f in filas])

    @app.post("/api/auth/usuarios")
    @requiere_admin(db)
    def crear_usuario():
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip().lower()
        password = str(data.get("password") or "")
        credencial = data.get("credencial", "usuario")
        if not username or len(password) < 8 or credencial not in ("admin", "usuario"):
            return jsonify({"error": "username, contraseña (mín. 8) y credencial válida son requeridos"}), 400
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO usuarios (username, password_hash, credencial) VALUES (?,?,?)",
                    (username, hash_password(password), credencial))
        except sqlite3.IntegrityError:
            return jsonify({"error": "Ese usuario ya existe"}), 409
        return jsonify({"ok": True}), 201

    @app.post("/api/auth/usuarios/<int:uid>/password")
    @requiere_login(db)
    def cambiar_password(uid):
        if g.usuario["credencial"] != "admin" and g.usuario["id"] != uid:
            return jsonify({"error": "Solo puedes cambiar tu propia contraseña"}), 403
        data = request.get_json(silent=True) or {}
        password = str(data.get("password") or "")
        if len(password) < 8:
            return jsonify({"error": "Contraseña de mínimo 8 caracteres"}), 400
        with db() as conn:
            # token_version +1 → todas las sesiones de ese usuario quedan revocadas
            n = conn.execute(
                "UPDATE usuarios SET password_hash=?, token_version=token_version+1 WHERE id=?",
                (hash_password(password), uid)).rowcount
        return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)

    @app.post("/api/auth/usuarios/<int:uid>/activo")
    @requiere_admin(db)
    def cambiar_activo(uid):
        data = request.get_json(silent=True) or {}
        activo = 1 if data.get("activo") else 0
        with db() as conn:
            n = conn.execute(
                "UPDATE usuarios SET activo=?, token_version=token_version+1 WHERE id=?",
                (activo, uid)).rowcount
        return (jsonify({"ok": True}), 200) if n else (jsonify({"error": "No existe"}), 404)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import getpass

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
    db_path = Path(os.environ.get("SISMO_DB", Path(__file__).parent / "data" / "sismo.db"))

    parser = argparse.ArgumentParser(description="Gestión de usuarios del panel admin")
    parser.add_argument("accion", choices=["crear", "listar", "password", "activar", "desactivar"])
    parser.add_argument("username", nargs="?", default="")
    parser.add_argument("--admin", action="store_true", help="con la acción 'crear': rol admin")
    parser.add_argument("--password", default="", help="contraseña (si no, se pide en pantalla)")
    args = parser.parse_args()

    # data/ está en .gitignore: si el CLI corre antes que la app (p. ej. crear
    # el primer admin en un despliegue nuevo), la carpeta aún no existe
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_USUARIOS)
    username = args.username.strip().lower()

    if args.accion == "listar":
        for f in conn.execute("SELECT id, username, credencial, activo, creado_en FROM usuarios"):
            estado = "activo" if f["activo"] else "INACTIVO"
            print(f"{f['id']:>3}  {f['username']:<20} {f['credencial']:<8} {estado}  ({f['creado_en']})")
    elif args.accion == "crear":
        assert username, "falta el username"
        pw = args.password or getpass.getpass(f"Contraseña para {username}: ")
        assert len(pw) >= 8, "mínimo 8 caracteres"
        conn.execute("INSERT INTO usuarios (username, password_hash, credencial) VALUES (?,?,?)",
                     (username, hash_password(pw), "admin" if args.admin else "usuario"))
        conn.commit()
        print(f"Usuario '{username}' creado ({'admin' if args.admin else 'usuario'}).")
    elif args.accion == "password":
        assert username, "falta el username"
        pw = args.password or getpass.getpass(f"Nueva contraseña para {username}: ")
        assert len(pw) >= 8, "mínimo 8 caracteres"
        n = conn.execute("UPDATE usuarios SET password_hash=?, token_version=token_version+1"
                         " WHERE username=?", (hash_password(pw), username)).rowcount
        conn.commit()
        print("Contraseña actualizada." if n else "Ese usuario no existe.")
    else:
        activo = 1 if args.accion == "activar" else 0
        n = conn.execute("UPDATE usuarios SET activo=?, token_version=token_version+1"
                         " WHERE username=?", (activo, username)).rowcount
        conn.commit()
        print(f"Usuario {args.accion}do." if n else "Ese usuario no existe.")
    conn.close()
