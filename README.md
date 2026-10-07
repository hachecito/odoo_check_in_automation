# odoo-check-in

Ficha la entrada y la salida de asistencia en **Odoo 19** automáticamente, con Playwright y el botón del systray. Está pensado para ejecutarse desde un cron o desde [Hermes Agent](https://hermes-agent.nousresearch.com/), que además avisa por Telegram.

- Salta **fines de semana**, **festivos** de la empresa y **ausencias aprobadas** (vacaciones, etc.), consultando a Odoo en cada ejecución.
- Solo usa **usuario y contraseña**: no necesita API key ni XML-RPC/JSON-RPC, que se eliminarán en Odoo 22. Inicia sesión por `/web/login` y consulta los datos con la sesión del navegador.
- Es seguro ante dobles ejecuciones: comprueba el estado actual, aplica un margen mínimo entre fichajes y ofrece la opción `--expect in|out`.
- Verifica en `hr.attendance` que el fichaje quedó registrado.
- Los selectores no dependen del idioma de la interfaz.

## Requisitos
- Python 3.10+
- Un usuario de Odoo 19 con empleado asociado y el módulo de Asistencias.

## Instalación
```bash
git clone <URL_DEL_REPO> odoo-check-in
cd odoo-check-in
python -m venv venv
# Windows: venv\Scripts\activate   |   Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env    # rellenar las credenciales
```

## Configuración (`.env`)
| Variable | Descripción | Por defecto |
|---|---|---|
| `ODOO_URL` | URL de la instancia | — |
| `ODOO_DB` | Base de datos (solo si hay varias) | vacío |
| `ODOO_LOGIN` / `ODOO_PASSWORD` | Credenciales del usuario | — |
| `TZ_NAME` | Zona horaria para calcular "hoy" | `Europe/Madrid` |
| `JITTER_MAX_MINUTES` | Espera aleatoria máxima antes de fichar | `0` |
| `MIN_TOGGLE_MINUTES` | Hace skip si el último fichaje es más reciente que este margen | `60` |
| `POST_LOGIN_PAUSE_SECONDS` | Pausa tras el login | `3` |
| `SYSTRAY_SELECTOR` | Selector alternativo del botón del systray | independiente del idioma |

## Uso
```bash
python odoo_checkin.py                     # toggle automático (entrada o salida según el estado)
python odoo_checkin.py --expect in         # solo ficha si toca la entrada
python odoo_checkin.py --expect out        # solo ficha si toca la salida
python odoo_checkin.py --dry-run           # indica qué haría, sin hacer clic
python odoo_checkin.py --date 2026-12-25   # simula otro día (fuerza --dry-run)
python odoo_checkin.py --list-offdays 90   # lista ausencias y festivos de los próximos 90 días
python odoo_checkin.py --headed --no-jitter  # depurar viendo el navegador
```

## Salida
Una línea JSON y un código de salida:

| Código | `status` | Ejemplo |
|---|---|---|
| `0` | `done` | `{"status": "done", "reason": "check_in", "at": "09:27"}` |
| `10` | `skipped` | `weekend`, `holiday:<nombre>`, `leave:<tipo>`, `recent_toggle`, `unexpected_state`, `dry_run` |
| `1` | `error` | `{"status": "error", "reason": "exception", "detail": "..."}`. Además guarda una captura en `last_error.png`. |

## Uso con Hermes Agent
La skill [`skills/odoo-attendance/SKILL.md`](skills/odoo-attendance/SKILL.md) le indica a Hermes cómo ejecutar el script, cómo interpretar el resultado, cuándo notificar por Telegram y cómo reintentar sin duplicar fichajes.

1. Instalar el script como se indica arriba y comprobarlo con `--dry-run`.
2. Instalar la skill **en el perfil del bot**. En Hermes las skills son por perfil y cada bot es un perfil.
   - Desde el repo: `hermes skills install <usuario>/<repo>/skills/odoo-attendance`
   - O copiando la carpeta `skills/odoo-attendance/` en el directorio `skills/` del perfil (por ejemplo, `~/.hermes/profiles/<bot>/skills/`).
3. Editar la línea **CMD** del `SKILL.md` instalado con la ruta del repo.
4. Pedirle al bot que cree las tareas programadas. La skill define `odoo-entrada` (09:25) y `odoo-salida` (17:30), de lunes a viernes, en hora local.

## Aviso
El registro de jornada debe reflejar las horas realmente trabajadas. Conviene comprobar que la política de su empresa permite automatizar el fichaje. Se usa bajo su propia responsabilidad.
