# Tisma Cloud — servidor maestro y nodos de cómputo

Software **propietario** (ver `LICENSE`), separado de Tisma Slicer (AGPL). No contiene código de PrusaSlicer ni de
Tisma: ejecuta `tisma-slicer` y `tisma-fea` como programas aparte. La versión de Tisma que use el servicio tiene que
estar publicada con todos sus cambios (AGPL §13).

## Cómo funciona

```
Tisma / app / página web ──HTTPS──► MAESTRO (Caddy + API + PostgreSQL)
                                       │  cola en la base de datos
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
                NODO i5            NODO i7-1          NODO i7-2
     prioridades: fea_optimize,  slice,slice_large  slice,slice_large,fea
                 fea,slice_large,slice
```

- **El maestro** recibe todo lo que llega de internet, controla las cuentas y guarda los trabajos en una cola
  (PostgreSQL). No calcula nada.
- **Los nodos** se instalan aparte (solo el paquete del nodo y los programas de Tisma). Cada uno **pide** trabajo al
  maestro cuando tiene una ranura libre; si un nodo se apaga, los demás siguen y sus trabajos vuelven a la cola.
- **Prioridades por nodo:** cada nodo tiene una lista ordenada de clases de trabajo. Toma primero la primera clase
  que tenga trabajos esperando; si no hay, la siguiente. Una clase que no está en su lista nunca la toma. Las cambias
  desde el maestro, sin tocar el nodo:

  | Clase | Qué es |
  |---|---|
  | `slice` | Laminar una placa |
  | `slice_large` | Laminar una placa grande (más de `TISMA_LARGE_SLICE_TRIANGLES` triángulos, 300 000 por ahora) |
  | `fea` | Análisis estructural |
  | `fea_optimize` | Relleno mínimo / paredes de resina (muchos análisis) |

  Dentro de una clase salen primero los trabajos de los planes con más prioridad y después los más viejos.

## Cuentas, planes y pagos

- La **página web** crea los usuarios (correo y contraseña), elige su plan y registra cada pago moviendo la fecha
  «pagado hasta». Lo hace con la API `/v1/site` y la clave `TISMA_SITE_KEY`.
- Un **plan** tiene horas de cómputo por mes calendario (UTC), cuántos trabajos puede tener un usuario esperando o
  corriendo a la vez, cuántas peticiones por minuto puede hacer y su prioridad en la cola.
- Se cobra el **tiempo real** que el trabajo corrió en un nodo (los que fallan no se cobran; uno cancelado mientras
  corría sí). Un trabajo empieza si quedan horas, aunque termine pasándose un poco.
- Un usuario puede enviar trabajos si su cuenta está habilitada, está **al día** (hasta `TISMA_GRACE_DAYS` días después
  de «pagado hasta»), le quedan horas y tiene menos trabajos activos que su límite. Si no, la API responde 402
  (pago u horas) o 429 (límite de trabajos o de peticiones).
- Desde Tisma el usuario entra con su correo y contraseña; el maestro le da un token por dispositivo. Cambiar la
  contraseña cierra todas las sesiones.
- Protección: límite de peticiones por minuto por usuario (según su plan), por nodo y de intentos de inicio de sesión
  (por dirección y por correo). Las contraseñas se guardan con scrypt; los tokens, como SHA-256.

## Seguridad de los nodos

Un 3MF puede traer un script de post-procesado (`post_process`) que el laminador ejecuta al terminar: **el nodo lo
quita** antes de laminar. Además rechaza proyectos de otros laminadores (Bambu Studio, OrcaSlicer), archivos que
descomprimidos pasan de 4 GB y, por ahora, el laminado de resina. Cada trabajo usa un directorio de datos vacío y no
recibe entrada estándar. Con Docker el nodo corre sin privilegios y con el sistema de solo lectura.

## Instalar el maestro (i5)

1. `cp .env.example .env` y completar `POSTGRES_PASSWORD`, `TISMA_DOMAIN` y `TISMA_SITE_KEY`.
2. `docker compose -f deploy/compose.master.yaml up -d`
3. En el router, abrir solo el 443 (y el 80 para el certificado) hacia el maestro. El 8000 es para los nodos de la
   red local: no abrirlo hacia internet.
4. Planes y primer administrador:

```
docker compose -f deploy/compose.master.yaml exec api python -m tisma_cloud.admin plan-set basico --hours 10 --concurrent 2 --rpm 120 --priority 1
docker compose -f deploy/compose.master.yaml exec api python -m tisma_cloud.admin user-add tu@correo.com --plan basico --admin
```

Sin Docker: `pip install ".[master]"`, `TISMA_DATABASE_URL=postgresql+psycopg://...` y
`uvicorn tisma_cloud.main:app --host 0.0.0.0 --port 8000`.

## Instalar un nodo

En el maestro: `python -m tisma_cloud.admin node-add i7-1 --priorities slice,slice_large,fea` (muestra el token una
sola vez).

En la máquina del nodo:

- **Sin Docker** (Linux o Windows): instalar Tisma Slicer y Python 3.11+, `pip install .`, copiar
  `deploy/node.example.toml` a `node.toml`, completar las rutas de `tisma-slicer` (en Windows
  `tisma-slicer-console.exe`) y `tisma-fea`, y correr `TISMA_NODE_TOKEN=tn_... python -m tisma_cloud.node --config node.toml`.
- **Con Docker** (Linux): copiar los programas de una compilación de Tisma para Linux a `deploy/tisma/`, crear
  `deploy/node.toml` y correr `TISMA_NODE_TOKEN=tn_... docker compose -f deploy/compose.node.yaml up -d`.

Sugerencia para tus equipos (a ajustar cuando se midan):

| Equipo | Prioridades | `slots` |
|---|---|---|
| i5-9400F + GTX 1060 (también maestro) | `fea_optimize,fea,slice_large,slice` | 2 |
| i7 3.ª gen. 16 GB (×2) | `slice,slice_large,fea` | 1 |

## Administración

**Panel web:** `http://<central>:8000/admin` (o `https://<tu dominio>/admin`). Se entra con un usuario administrador
con contraseña (`python -m tisma_cloud.admin user-add tu@correo.com --plan basico --admin --password`). Muestra cada
servidor (en línea o sin señal, procesador, memoria, GPU, qué está haciendo y su progreso), la cola por tipo de
trabajo con los servidores que lo atienden, y los trabajos en curso y los últimos terminados. Las prioridades de cada
servidor se marcan y ordenan con ↑ ↓ y se guardan con «Guardar prioridades»; valen desde el próximo trabajo. Se
actualiza cada 5 segundos.

Por comandos:

```
python -m tisma_cloud.admin node-list                     # nodos, prioridades, trabajos corriendo, última señal
python -m tisma_cloud.admin node-set i7-1 --priorities fea,slice
python -m tisma_cloud.admin node-set i7-2 --disable
python -m tisma_cloud.admin queue                         # trabajos esperando y corriendo por clase
python -m tisma_cloud.admin user-list                     # plan, si está al día, horas usadas
python -m tisma_cloud.admin user-set ana@correo.com --paid-until 2026-12-31
```

La misma información está en la API de administración (`/v1/admin/...`, con el token de un administrador).

## API

| Quién | Método y ruta | Qué hace |
|---|---|---|
| todos | `GET /v1/health`, `GET /v1/plans` | estado, planes |
| usuario | `POST /v1/auth/login` `{email, password, device}` | token del dispositivo |
| usuario | `POST /v1/auth/logout`, `GET /v1/me` | cerrar sesión; plan, al día, horas, trabajos activos |
| usuario | `POST /v1/jobs` (multipart: `kind` slice\|fea, `params` JSON, `file` 3MF) | enviar un trabajo |
| usuario | `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result`, `POST /v1/jobs/{id}/cancel` | seguir, descargar, cancelar |
| página web | `PUT /v1/site/plans/{nombre}`, `PUT /v1/site/users/{id_web}`, `GET /v1/site/users/{id_web}` | planes, usuarios, pagos |
| nodo | `POST /v1/node/claim`, `/heartbeat`, `GET /v1/node/jobs/{id}/input`, `POST .../progress`, `.../result`, `.../fail` | trabajar |
| administrador | `/v1/admin/nodes`, `/v1/admin/users`, `/v1/admin/plans/{nombre}`, `/v1/admin/queue` | administrar |

Parámetros de `fea`: `mode` (`analyze`, `lightest-infill`, `resin-structure`), `quality` (`fast`, `normal`, `high`,
`ultra`), `object`, `instance`, `solid`, `zones`, `ribs`. La documentación interactiva queda en `/docs`.

Para agregar otro tipo de trabajo: una clase en `jobs.py` (con sus parámetros) y un ejecutor en `node/runners.py`.

## Tests

```
pip install ".[master,test]"
pytest                                                    # SQLite
TISMA_TEST_DATABASE_URL=postgresql+psycopg://... pytest   # PostgreSQL (incluye la concurrencia de la cola)
TISMA_SLICER_BIN=.../tisma-slicer TISMA_FEA_BIN=.../tisma-fea pytest   # con los programas reales
```

## Pendiente

- Medir en tus equipos (laminado y FEA) para fijar las horas de cada plan y el umbral de las placas grandes.
- Laminado de resina en la nube.
- Materiales propios del usuario en el FEA en la nube (por ahora se usan los de la tabla incluida).
- Respaldo del maestro (keepalived) y copia de la base de datos (ver `docs/CLOUD_ARCHITECTURE.md`).
