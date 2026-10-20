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

## Qué se ve desde internet y qué no

- **Desde internet** (`https://tismaservidores.net`, Caddy en el 443): la API de los usuarios (Tisma inicia sesión,
  lamina y calcula) y la API de la página web (`/v1/site`, con su clave y, si se configura `TISMA_SITE_NETWORKS`,
  solo desde la IP del servidor de la página).
- **Solo desde la red interna:** el panel `/admin` y la API `/v1/admin`. Caddy los corta (404) y el maestro también los
  niega a cualquier dirección fuera de `TISMA_ADMIN_NETWORKS` (por defecto, las redes privadas 192.168.x.x, 10.x.x.x,
  172.16–31.x.x). Se entra con `http://<central>:8000/admin` desde una PC conectada a la red del rack (Wi-Fi o
  cable). El puerto 8000 no se abre en el router.

## Cuentas: correo confirmado y entrar con Google

- **Correo y contraseña:** al crear la cuenta, el central manda un código de 6 números al correo; hasta confirmarlo, la
  cuenta no inicia sesión. El código vence a los 15 minutos, sirve una vez y muere tras 5 intentos equivocados; se
  puede pedir otro (uno por minuto, 5 por hora por correo). «Olvidé mi contraseña» manda otro código para elegir una
  nueva y cierra las sesiones abiertas. Las respuestas de «mandar código» son iguales exista o no la cuenta (no se puede
  averiguar quién tiene cuenta).
- **El correo sale del servidor de correo del dominio** (`TISMA_SMTP_*`, por ejemplo el correo de tisma.com.uy que da
  el hosting, con `noreply@tisma.com.uy`). No conviene mandar desde las PC del rack: los proveedores de correo rechazan
  o mandan a no deseado lo que sale de una conexión hogareña. Para que lleguen bien, el dominio necesita registros
  SPF, DKIM y DMARC (los configura el proveedor del correo).
- **Entrar con Google:** con `TISMA_GOOGLE_CLIENT_IDS` (se crea en la consola de Google Cloud), «Mi cuenta» muestra
  «Continuar con Google» y la app del teléfono podrá usar el mismo `/v1/auth/google` con el SDK de Google. El central
  verifica el token como lo documenta Google (firma con las claves públicas de Google, emisor, destinatario y
  vencimiento). Google responde por la dirección solo si es Gmail o una cuenta de empresa (Google Workspace): solo
  entonces se vincula a una cuenta existente con ese correo; con otros correos se pide nuestro código.

## Cobro en el central

Los usuarios crean su cuenta y pagan en **`https://tismaservidores.net/cuenta`** (la página «Mi cuenta» del propio
maestro); tisma.com.uy y Tisma (Configuración > Cuenta de Tisma Cloud) tienen un enlace a ella. Así cada pago queda
registrado directamente en el central, sin pasar por el servidor externo de la página.

- **Mi cuenta:** crear cuenta (correo y contraseña), entrar, ver el plan, si está al día, las horas usadas del mes, los
  planes con su precio, pagar y el historial de pagos.
- **Pago:** el botón «Pagar» le pide al medio de pago una página de pago (la tarjeta o la cuenta se ingresan allí, nunca
  en el central) y el medio de pago le avisa al central cada cobro (`/v1/billing/webhook/<medio>`). Un pago aprobado
  extiende el plan un mes desde la fecha en que vencía (o desde hoy si ya había vencido). El importe lo fija el central
  con el precio del plan: si el aviso dice otro importe o moneda, no se aplica. Cada pago se aplica una sola vez.
- **Medio de pago:** todavía sin elegir. `TISMA_BILLING_PROVIDER=test` activa un medio **de prueba** (botones
  «Aprobar» y «Rechazar») para probar todo el circuito; **nunca en producción**. El medio real se agrega en
  `tisma_cloud/billing.py` (clase `Provider`: crear el pago y verificar sus avisos).
- **Pagos fuera de línea** (transferencia, efectivo): en el panel `/admin` > Usuarios > «Registrar pago».
- **Planes con precio:** `PUT /v1/admin/plans/<nombre>` (desde la red interna) o `/v1/site/plans/<nombre>` con
  `price_cents`, `currency` y `public: true` para que aparezca en Mi cuenta.

## Suscripciones con Google Play

Google cobra (tarjetas, saldo de Google Play, facturación del operador y los medios de cada país donde vende Play) y el
central solo verifica la compra y da el acceso. El plan comprado en la app **vale en toda la cuenta**: la app, Tisma de
escritorio y «Mi cuenta» en el navegador. Lo que Google **no** permite es pagar con Google Play desde una página web:
la biblioteca de facturación de Google Play es solo para apps de Android. En la página, el pago es con otro medio (o el
usuario se suscribe desde la app), y la página puede decir que existe la app (la política de Google Play solo limita lo
que se dice *dentro* de la app).

Cómo funciona (según la documentación de Google: [backend](https://developer.android.com/google/play/billing/backend),
[ciclo de vida](https://developer.android.com/google/play/billing/lifecycle/subscriptions),
[notificaciones](https://developer.android.com/google/play/billing/getting-ready#configure-rtdn)):

1. La app pide `GET /v1/billing/play/account` (un id al azar de la cuenta, sin datos personales) y lo pasa a Google
   Play como `obfuscatedAccountId` al comprar. `GET /v1/plans` dice el producto de Play de cada plan (`play_product`).
2. Después de comprar, la app manda el token de compra a `POST /v1/billing/play/purchase`. El central le pregunta a
   Google (`purchases.subscriptionsv2.get`), comprueba que la compra es de esa cuenta y de uno de nuestros productos, la
   **confirma** (`acknowledge`; sin eso Google la reembolsa a los 3 días) y da acceso hasta `expiryTime` mientras el
   estado lo permite (activa, período de gracia, cancelada pero no vencida).
3. Google avisa cada cambio (renovación, cancelación, suspensión, reembolso, cambio de plan…) a
   `POST /v1/billing/play/notify` por Pub/Sub. El aviso se verifica (token firmado por Google para nuestra audiencia y
   nuestra cuenta de servicio) y el central vuelve a preguntarle a Google el estado: los avisos repetidos no cambian
   nada. Un cambio de plan o una resuscripción fuera de la app se vinculan a la cuenta por el token anterior o por el id
   de la cuenta.
4. Un reembolso o una suspensión quitan solo el acceso de Google Play: los pagos registrados aparte (transferencia en
   Montevideo, otro medio) siguen valiendo.

Configuración (una vez, con la cuenta de desarrollador de Google Play; Uruguay admite cuenta de comerciante y cobra en
USD según la [tabla de Google](https://support.google.com/googleplay/android-developer/answer/9306917)):

- Play Console: crear las suscripciones (ids como `tisma_basico`, `tisma_pro`) con su precio; Play ofrece precios por
  país.
- Google Cloud: habilitar la *Google Play Android Developer API*, crear una cuenta de servicio con su clave JSON e
  invitarla en Play Console > Usuarios y permisos con permiso para ver datos financieros y administrar pedidos y
  suscripciones. La clave va en el central fuera del repositorio (`TISMA_PLAY_CREDENTIALS`).
- Pub/Sub: un tema, darle a `google-play-developer-notifications@system.gserviceaccount.com` permiso de publicar, una
  suscripción **push** a `https://tismaservidores.net/v1/billing/play/notify` con autenticación (cuenta de servicio y
  audiencia, que van en `TISMA_PLAY_PUSH_ACCOUNT` y `TISMA_PLAY_PUSH_AUDIENCE`), y poner el tema en Play Console >
  Monetización > Notificaciones en tiempo real.
- `.env`: `TISMA_PLAY_PACKAGE` (el paquete de la app) y `TISMA_PLAY_PRODUCTS` (`producto:plan,...`).

El central llama a `androidpublisher.googleapis.com` y `oauth2.googleapis.com` por HTTPS: la salida a internet del
central tiene que permitirlas.

## Protección contra ataques (DDoS) y privacidad

Sin servicios externos: todo corre en el central, en cuatro capas.

1. **Firewall del central** (`deploy/firewall.nft`, nftables): bloquea 10 minutos una dirección que abre más de 20
   conexiones por segundo, limita a 50 las conexiones abiertas por dirección, corta el puerto 8000 desde afuera y limita
   el ping. Va antes de Docker, así que cubre los puertos publicados. Más `deploy/sysctl-tisma.conf` (SYN cookies contra
   inundaciones de conexiones a medio abrir). Probado: un ataque de 300 conexiones queda en 40 y la dirección bloqueada,
   y los usuarios normales siguen entrando.
2. **Caddy:** tiempos máximos para leer encabezados y cuerpos (los clientes lentos no ocupan conexiones), encabezados de
   16 KiB como máximo, proyectos de 210 MB como máximo, administración cortada desde afuera, encabezados de seguridad.
3. **Antes de la aplicación** (`tisma_cloud/guard.py`): las subidas sin token válido, sin tamaño o demasiado grandes se
   rechazan antes de leer un solo byte; pocas subidas a la vez (8 en total, 2 por cuenta); páginas sin cuenta limitadas a
   120 por minuto por dirección; una dirección con 30 rechazos en un minuto queda bloqueada 10 minutos. La red interna
   nunca se bloquea.
4. **La aplicación:** trabajos simultáneos y peticiones por minuto según el plan, intentos de inicio de sesión
   limitados, uvicorn con un máximo de 300 peticiones a la vez.

Límite honesto: un ataque más grande que la conexión a internet (32 Mbps de subida, 220 de bajada) la satura antes de
llegar al central; eso solo lo puede frenar el proveedor de internet. Todo lo demás (inundaciones de conexiones,
peticiones, subidas, fuerza bruta de contraseñas) se frena en el central.

**Privacidad:** el proyecto del usuario se borra del central apenas termina el trabajo (aprobado, fallado o cancelado);
el resultado se borra a las 72 h. Contraseñas con scrypt y tokens con SHA-256 (no se guardan en claro). Los datos de las
tarjetas nunca pasan por el central. Las páginas no cargan nada de otros sitios.

**Transferencias:** el G-code y los resultados del análisis viajan comprimidos (Caddy con zstd o gzip; Tisma los pide y
los descomprime): un G-code de 218 KB baja en 26 KB.

## Seguridad de los nodos

Un 3MF puede traer un script de post-procesado (`post_process`) que el laminador ejecuta al terminar: **el nodo lo
quita** antes de laminar. Además rechaza proyectos de otros laminadores (Bambu Studio, OrcaSlicer), archivos que
descomprimidos pasan de 4 GB y, por ahora, el laminado de resina. Cada trabajo usa un directorio de datos vacío y no
recibe entrada estándar. Con Docker el nodo corre sin privilegios y con el sistema de solo lectura.

## Instalar el maestro (i5)

1. `cp .env.example .env` y completar `POSTGRES_PASSWORD`, `TISMA_DOMAIN`, `TISMA_PUBLIC_URL` y `TISMA_SITE_KEY`.
2. Firewall y kernel (en el central, una vez; ajustar `LAN` en `firewall.nft` a la red del rack):
   `sudo cp deploy/firewall.nft /etc/nftables.d/tisma.nft && sudo nft -f /etc/nftables.d/tisma.nft` y
   `sudo cp deploy/sysctl-tisma.conf /etc/sysctl.d/90-tisma.conf && sudo sysctl --system`.
   Para que el firewall quede después de reiniciar, incluirlo desde `/etc/nftables.conf` y activar el servicio
   `nftables` (`sudo systemctl enable nftables`).
3. `docker compose --env-file .env -f deploy/compose.master.yaml up -d`
4. En el router, abrir solo el 443 (y el 80 para el certificado) hacia el maestro. El 8000 es para los nodos de la
   red local: no abrirlo hacia internet.
5. Planes y primer administrador:

```
docker compose --env-file .env -f deploy/compose.master.yaml exec api python -m tisma_cloud.admin plan-set basico --hours 10 --concurrent 2 --rpm 120 --priority 1
docker compose --env-file .env -f deploy/compose.master.yaml exec api python -m tisma_cloud.admin user-add tu@correo.com --plan basico --admin --password
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
| usuario | `GET /v1/billing/play/account`, `POST /v1/billing/play/purchase` `{purchase_token}` | suscripción de Google Play (app) |
| Google | `POST /v1/billing/play/notify` | avisos de Google Play (Pub/Sub, con token de Google) |
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

- Elegir el medio de pago para la página web (Google Play no cobra en páginas web) y agregarlo en `billing.py`. Para el
  exterior, un «merchant of record» que cobra y paga los impuestos de cada país: Lemon Squeezy paga a bancos de Uruguay
  según su [documentación](https://docs.lemonsqueezy.com/help/getting-started/supported-countries); Paddle está por
  confirmar. Para la región, Mercado Pago (suscripciones en Uruguay). La app de iPhone usaría la compra dentro de la app de Apple (otro verificador como el de
  Google Play).
- La app de Android (compra con la biblioteca de facturación de Google Play y `/v1/billing/play/*`).
- Entrar con Google desde Tisma de escritorio (hoy: correo y contraseña; Google en la página y en la app).
- Medir en tus equipos (laminado y FEA) para fijar las horas de cada plan y el umbral de las placas grandes.
- Laminado de resina en la nube.
- Materiales propios del usuario en el FEA en la nube (por ahora se usan los de la tabla incluida).
- Respaldo del maestro (keepalived) y copia de la base de datos (ver `docs/CLOUD_ARCHITECTURE.md`).
