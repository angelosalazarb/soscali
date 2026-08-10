# Despliegue — SOS Terremoto (VM en la nube)

App de reporte ciudadano post-sismo. Se despliega en una **VM en la nube**
(DigitalOcean / Hetzner / Oracle, Ubuntu 22.04+) — no en el homelab, para que
siga viva ante cortes de energía. El panel de moderación solo es alcanzable
por **VPN (Tailscale)**.

Repositorio: `https://github.com/angelosalazarb/soscali`

## 1. Preparar la VM

```bash
# como root en la VM
adduser --system --group --home /opt/soscali sos
apt update && apt install -y python3-venv git caddy

# Tailscale (VPN para el panel admin)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up          # autenticar contra tu tailnet
tailscale ip -4       # anotar la IP 100.x.y.z para el Caddyfile
```

## 2. Clonar y crear el entorno

```bash
git clone https://github.com/angelosalazarb/soscali /opt/soscali
chown -R sos:sos /opt/soscali
cd /opt/soscali/backend
sudo -u sos python3 -m venv .venv
sudo -u sos .venv/bin/pip install -r requirements.txt
```

## 3. Configuración y usuarios

```bash
cd /opt/soscali/backend
sudo -u sos cp .env.example .env
# generar el secreto JWT y pegarlo en .env:
python3 -c "import secrets; print(secrets.token_hex(48))"
sudo -u sos nano .env && chmod 600 .env

# data/ está en .gitignore: el clon no la trae, la crea la app al arrancar.
# Crear el usuario del panel:
sudo -u sos .venv/bin/python auth.py crear admin --admin
```

## 4. Servicio systemd

```bash
cp deploy/sos-terremoto.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sos-terremoto
curl -s http://localhost:8085/salud    # → {"ok": true, "reportes": N}
```

## 5. Caddy + DNS + VPN

1. Pegar los dos site blocks de `deploy/Caddyfile.example` en
   `/etc/caddy/Caddyfile`, reemplazando el dominio y la IP Tailscale real.
2. Crear el registro A del dominio apuntando a la IP pública de la VM.
3. `systemctl reload caddy`.
4. Verificar:
   - `https://<dominio>/` carga la app y `https://<dominio>/admin` → **403**.
   - Desde un equipo del tailnet: `http://100.x.y.z/admin` carga el login.

## 6. Actualizar en producción

```bash
cd /opt/soscali && sudo -u sos git pull
systemctl restart sos-terremoto
```
`.env` y `backend/data/` están en `.gitignore`: un pull nunca los toca.
Si cambió el `.service`: copiar de nuevo a `/etc/systemd/system/` +
`systemctl daemon-reload` (un pull NO actualiza systemd).

## Operación

- **Logs:** `journalctl -u sos-terremoto -f`
- **Respaldo BD:** `sqlite3 /opt/soscali/backend/data/sismo.db ".backup /root/backups/sismo-$(date +%F).db"` (cron diario recomendado).
- **Backend caído:** la web pública guarda los reportes en la cola offline del
  navegador de cada usuario y los reenvía sola al volver el servicio.

## Checklist antes de publicar el dominio

- [ ] `JWT_SECRET_KEY` generado (48 bytes) y `.env` con `chmod 600`
- [ ] Usuario admin creado con contraseña fuerte
- [ ] `https://<dominio>/admin` responde 403 desde internet
- [ ] Panel accesible solo vía IP Tailscale
- [ ] Respaldo automático de `sismo.db` configurado
- [ ] **Revisión de seguridad completa de la app (pendiente registrado)**
