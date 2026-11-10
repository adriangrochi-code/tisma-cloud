# Tisma Cloud — servidor maestro y nodos de cómputo

Software **propietario** (ver `LICENSE`), separado de Tisma Slicer (AGPL). No contiene código de PrusaSlicer ni de
Tisma: ejecuta `tisma-slicer` y `tisma-fea`, o el slicer nuevo `tisma-engine`, como programas aparte. La versión de Tisma que use el servicio tiene que
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
  maestro cuando tiene una ranura libre; si un nodo se apaga, los demás siguen y sus trabajos vuelven a la cola. Un
  problema del propio nodo (disco lleno, un error del programa del nodo) también devuelve el trabajo a la cola, hasta el
  límite de intentos; con menos de 2 GB libres en su carpeta de trabajo, el nodo no toma trabajos y lo dice en su registro.
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

### Cuentas internas (sin cobro, sin límite)

Para el equipo: una cuenta **interna** lamina y calcula sin límite de horas, de trabajos a la vez ni de peticiones, y
no necesita pagar. No es administradora (no entra al panel ni recibe los avisos). El tiempo que usa se sigue contando
(para saber cuánto cómputo se va en uso propio).

- **Por correo o dominio:** `TISMA_INTERNAL_EMAILS=equipo@tisma.com.uy,@tisma.com.uy` (separados por comas; `@dominio`
  vale para todas las direcciones del dominio). Una cuenta con esas direcciones es interna desde que se crea (registro,
  Google, página web o `admin user-add`) y las que ya existían pasan a serlo al arrancar el maestro.
- **Una por una:** en el panel `/admin`, botón «Hacer interna» / «Quitar sin límite» de cada usuario, o
  `tisma-cloud admin user-set ana@correo.com --unlimited` (`--limited` la vuelve normal). Sacar una dirección de
  `TISMA_INTERNAL_EMAILS` no la vuelve normal: hay que hacerlo así.
- Solo vale con el correo **confirmado**: alguien que se registra con una dirección del equipo sin poder leer su correo
  no queda sin límite.
- Tisma y la app ven `"unlimited": true` en `/v1/me` (y `paid: true`).

## Qué se ve desde internet y qué no

- **Desde internet** (`https://tismaservidores.net`, Caddy en el 443): la API de los usuarios (Tisma inicia sesión,
  lamina y calcula) y la API de la página web (`/v1/site`, con su clave y, si se configura `TISMA_SITE_NETWORKS`,
  solo desde la IP del servidor de la página).
- **Solo desde la red interna:** el panel `/admin` y la API `/v1/admin`. Caddy los corta (404) y el maestro también los
  niega a cualquier dirección fuera de `TISMA_ADMIN_NETWORKS` (por defecto, las redes privadas 192.168.x.x, 10.x.x.x,
  172.16–31.x.x). Se entra con `http://<central>:8000/admin` desde una PC conectada a la red del rack (Wi-Fi o
  cable). El puerto 8000 no se abre en el router.
- **La dirección del usuario** detrás de Caddy o del túnel viene en un encabezado (`TISMA_CLIENT_IP_HEADER`:
  `X-Forwarded-For` o `CF-Connecting-IP`). El maestro lo lee solo en conexiones desde `TISMA_TRUSTED_PROXIES` (por
  defecto, las mismas redes privadas y la propia máquina); una conexión desde internet directo al puerto 8000 se juzga
  por su propia dirección, aunque traiga el encabezado.

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
   120 por minuto por dirección; una dirección con 30 rechazos en un minuto queda bloqueada 10 minutos. Cualquier otro
   pedido (el JSON de la API, los avisos de los cobros) tiene como mucho `TISMA_MAX_BODY_KB` (1024 KB), con su
   `Content-Length` o mandado por partes: más es 413. Sin eso, con el túnel de Cloudflare (que no pasa por Caddy), un
   pedido sin cuenta de cientos de MB a `/v1/auth/login` llenaba la memoria. La red interna nunca se bloquea.
4. **La aplicación:** trabajos simultáneos y peticiones por minuto según el plan, intentos de inicio de sesión
   limitados, uvicorn con un máximo de 300 peticiones a la vez.

Límite honesto: un ataque más grande que la conexión a internet (32 Mbps de subida, 220 de bajada) la satura antes de
llegar al central; eso solo lo puede frenar el proveedor de internet. Todo lo demás (inundaciones de conexiones,
peticiones, subidas, fuerza bruta de contraseñas) se frena en el central.

**Privacidad:** ver la sección siguiente.

**Transferencias:** el G-code y los resultados del análisis viajan comprimidos (Caddy con zstd o gzip; Tisma los pide y
los descomprime): un G-code de 218 KB baja en 26 KB.

## Seguridad de las cuentas y privacidad

El objetivo es guardar lo mínimo para dar el servicio, y que lo guardado sirva de poco a quien lo robe.

**Contraseñas y sesiones**

- Contraseñas con **scrypt N=2^17, r=8, p=1** (128 MiB y medio segundo por intento), el primer ajuste que recomienda
  el [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
  Las hechas antes (2^14) siguen sirviendo y se rehacen con los parámetros nuevos la próxima vez que la persona entra.
  Como mucho 2 a la vez, para que una ola de intentos no se coma la memoria (el resto espera hasta 10 s o recibe 503).
- Los tokens (sesiones de cada dispositivo, nodos) son de 256 bits al azar y se guardan como SHA-256: la base robada no
  sirve para entrar. Una sesión que no se usa en `TISMA_SESSION_DAYS` (90) días se cierra sola.
- La cuenta ve sus sesiones abiertas y cierra cualquiera, o todas las demás; cambiar o recuperar la contraseña cierra
  las otras. Los intentos de entrar, de códigos y de registro están limitados por dirección y por correo.
- Adivinar la contraseña de una cuenta desde muchas direcciones: como mucho **20 contraseñas equivocadas por hora** a
  una cuenta, vengan de donde vengan (las correctas no cuentan; con la cuenta trabada, la persona entra eligiendo una
  contraseña nueva con un código por correo). Los códigos por correo son de 6 cifras, valen 15 minutos y 5 intentos,
  y llegan como mucho **10 por día** a una dirección: 50 intentos de adivinar un código por día.
- Todas las respuestas llevan `X-Content-Type-Options: nosniff`: un navegador nunca toma una descarga por una página.
- El registro responde lo mismo si el correo ya tiene cuenta (al dueño le llega un aviso): la página no sirve para
  averiguar quién es cliente. Entrar con un correo desconocido tarda lo mismo que con uno conocido.
- Las páginas solo ejecutan su propio script (CSP con su SHA-256, sin `unsafe-inline`).

**Qué se guarda y cuánto tiempo**

| Dato | Para qué | Cuánto |
|---|---|---|
| Correo, hash de la contraseña, plan, pagado hasta | la cuenta | mientras exista la cuenta |
| Pagos (importe, fecha, referencia del medio de pago) | contabilidad | siempre; al borrar la cuenta quedan sin correo ni datos de la persona |
| Sesiones (nombre del dispositivo, fechas) | entrar sin la contraseña | hasta cerrarlas o 90 días sin uso |
| Proyecto subido | laminarlo o analizarlo | se borra al terminar el trabajo |
| Resultado (G-code, análisis) | descargarlo | 72 h (`TISMA_RETENTION_H`) |
| Lista de trabajos (nombre del archivo, tiempos) | ver los trabajos | 90 días (`TISMA_JOB_HISTORY_DAYS`); las horas usadas quedan en el uso del mes |
| Impresoras remotas y su último estado | verlas desde el teléfono | hasta quitar el puente o 180 días sin verlo |
| Archivo a imprimir de una orden remota | mandarlo a la impresora | hasta que el puente lo toma (10 min como mucho) |
| Imágenes de las cámaras | verlas en vivo | solo en memoria, 30 s; nunca en disco |
| Direcciones IP | frenar ataques | solo en memoria (límites y bloqueos); no hay registro de accesos (`TISMA_ACCESS_LOG=1` lo prende) |
| Problemas informados (textos y parte del registro, sin datos personales) | arreglar Tisma | 365 días (`TISMA_REPORT_DAYS`); se borran con la cuenta |

**Datos personales cifrados en la base** (`crypto.py`): el correo, los nombres de las sesiones, de los puentes, de las
impresoras y de los archivos, las notas de los pagos y los datos de las compras de Google Play se guardan cifrados con
AES-256-GCM (de [cryptography](https://cryptography.io/en/latest/hazmat/primitives/aead/), con un número al azar
distinto en cada valor, así que dos iguales no se ven iguales, y un valor cambiado no se lee). Una copia de la base
sola (una copia de seguridad, un `pg_dump`, el disco de la base) no muestra a quién pertenece cada cuenta ni qué
imprime. El correo se busca por su índice: un HMAC-SHA256 del correo con otra clave derivada de la misma; sin la clave
no se puede ni probar si un correo está en la base. Cuesta unos 5 µs por valor (un procesador de 2,1 GHz): nada al
lado de una consulta. Los datos de una versión anterior (en claro) se cifran solos al arrancar el maestro: 8000 cuentas
con sus puentes (24 001 valores) en 1,6 s en PostgreSQL 16.

La clave es `data.key` en la carpeta de almacenamiento (`TISMA_STORAGE_DIR`; en Docker, el volumen `storage`), hecha
en el primer arranque y legible solo por el servicio, o `TISMA_DATA_KEY` (32 bytes en base64) si se prefiere darla como
secreto. **Guardarla aparte de las copias de la base** (ver «Copias de seguridad»): sin ella no se pueden leer los
datos ni entrar con un correo, y junto a la copia no protege nada. Si el maestro arranca con otra clave que la de la
base, no arranca (en vez de escribir datos con la equivocada). `tisma-cloud-admin` y el programa de gestión usan la
misma clave: hay que correrlos con la misma `TISMA_STORAGE_DIR` que el servicio.

Nunca pasan por el central los datos de las tarjetas (se cargan en la página del medio de pago). Las páginas no cargan
nada de otros sitios (salvo el botón de Google si está activado).

**Lo que la persona puede hacer**: descargar una copia de todo lo que guardamos de su cuenta (`Descargar mis datos`, en
JSON) y borrar la cuenta (con la contraseña, o con un código por correo si entra con Google). Al borrar se van las
sesiones, trabajos y sus archivos, impresoras remotas y órdenes, compras de la tienda y el uso; quedan los pagos sin
nada personal.

**Archivos peligrosos** (`threats.py`): todo lo que se sube se revisa antes de guardarlo, y los administradores
reciben una alerta con lo rechazado en la última hora (solo cuántos y de qué tipo).

- **Proyectos (3MF)**: solo se aceptan si son eso. Se rechazan rutas que salen de la carpeta, archivos cifrados, «zip
  bombs» (más de 1000 veces su tamaño al descomprimir), programas adentro (por extensión o por contenido: Windows,
  Linux, macOS, scripts) y XML con declaraciones de entidades (XXE, «billion laughs»), que Tisma nunca escribe.
  Además el nodo quita los scripts de post-procesado y lamina en un entorno aislado (ver «Seguridad de los nodos»).
- **G-code para imprimir a distancia**: se rechaza el que cambia la impresora en vez de imprimir y que ningún perfil de
  impresora usa: `M997` (actualizar el firmware), `M28`/`M29`/`M30`/`M32` (escribir, borrar o ejecutar archivos de la
  tarjeta), `SAVE_CONFIG`, `FORCE_MOVE` (Klipper) y `RUN_SHELL_COMMAND` (ejecuta programas en la computadora de la
  impresora). Fuentes: [Marlin](https://marlinfw.org/docs/gcode/), [Klipper](https://www.klipper3d.org/G-Codes.html),
  [G-Code Shell Command](https://docs.kalico.gg/G-Code_Shell_Command.html). Los que algunos fabricantes usan en su
  G-code inicial (`M500`, `M502`, `SET_KINEMATIC_POSITION`, `RESTART`) no se rechazan: la orden vuelve con
  `warnings` para que el teléfono los muestre. El G-code binario (`.bgcode`) va comprimido y solo lo revisa el antivirus.
  Un G-code de más de 32 MB se lee en partes a la vez, una por proceso (hasta 4): 210 MB en 0,81 s en vez de 2,56 s
  (4 núcleos de 2,1 GHz), sin ocupar el proceso que atiende a los usuarios. No vale la pena hacerlo en la placa de
  video: el archivo tarda más en llegar por internet (200 MB a 220 Mbit/s, unos 7 s) que en revisarse.
- **Antivirus** (opcional): con `TISMA_CLAMD` cada proyecto, G-code y resultado pasa por
  [ClamAV](https://docs.clamav.net/) (servicio aparte, `docker compose --profile antivirus up -d`; pide 3 GiB de
  memoria). Si ClamAV no contesta, no se acepta nada sin revisar (503) y se avisa a los administradores. Probado con
  ClamAV 1.5.4 y sus firmas: encuentra el archivo de prueba EICAR dentro de un 3MF comprimido. Cuesta: un proyecto de
  2,4 MB (18 MB de XML adentro) tarda de 2,4 a 3,5 s la primera vez (en un Xeon de 2,1 GHz; el mismo archivo de nuevo,
  17 ms), en el proceso de ClamAV, no en el del maestro.

**Lo que depende de la instalación** (no lo puede hacer el programa):

- Disco cifrado en el central (LUKS en Linux, BitLocker en Windows): si alguien se lleva el disco, la base y las
  copias no se leen.
- Copias de seguridad en otro disco o máquina, también cifrado (`BACKUP_DIR`, sección «Copias de seguridad»).
- La red entre el central y los nodos va sin cifrar (puerto 8000 en la red local): los proyectos viajan en claro dentro
  de esa red. Si los nodos están en otro lugar, unirlos con una VPN (WireGuard).
- Mantener el sistema y Docker al día.

## Seguridad de los nodos

Un 3MF puede traer un script de post-procesado (`post_process`) que el laminador ejecuta al terminar: **el nodo lo
quita** antes de laminar. Además rechaza proyectos de otros laminadores (Bambu Studio, OrcaSlicer), archivos que
descomprimidos pasan de 4 GB y, por ahora, el laminado de resina. Cada trabajo usa un directorio de datos vacío y no
recibe entrada estándar. Con Docker el nodo corre sin privilegios y con el sistema de solo lectura.

## Instalar el maestro (i5)

**Para la beta en el i5 con Windows y el túnel de Cloudflare:** la lista en orden está en `deploy/BETA.md`, y
`deploy/smoke.py` prueba el maestro desde internet (HTTPS, planes, administración oculta y un trabajo de punta a punta).
Lo de abajo es la instalación con Docker en Linux.

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

`plan-set` también elige qué tipos de cómputo incluye el plan (`--classes slice,slice_large,orient`; `all`: todos; sin la
opción, un plan que ya existe los conserva) y si aparece en la lista pública de planes, `GET /v1/plans` (`--public` o
`--no-public`). Tisma y el teléfono leen de esa lista qué permite cada plan; lo que el plan no incluye se rechaza con 402.

Sin Docker: `pip install ".[master]"`, `TISMA_DATABASE_URL=postgresql+psycopg://...` y
`uvicorn tisma_cloud.main:app --host 0.0.0.0 --port 8000`.

**Sin IP fija, en Windows:** `deploy/cloudflare-tunnel-windows.md` pone el maestro en `tismaservidores.net` con un
túnel gratuito de Cloudflare, sin abrir puertos ni DNS dinámico.

### Programa tisma-cloud (Windows y Linux, sin Docker ni Python)

El programa **`tisma-cloud`** (`tisma-cloud.exe` en Windows) se arma a pedido: pestaña *Actions* › *Cloud server* ›
*Run workflow*. Con el repositorio privado queda como artefactos `tisma-cloud-windows` y `tisma-cloud-linux` (7 días);
con el repositorio público se arma y se prueba igual, pero no se sube (el paquete trae Tisma Slicer y tisma-engine y es
solo para las máquinas del dueño). Es el mismo programa para todas las máquinas: en cada una se elige si es el **maestro** (el servidor central)
o un **nodo de cómputo** (esclavo).

**Trae todo lo que necesita:** el paquete es `dist/tisma-cloud.exe` y, al lado, la carpeta `dist/tisma-slicer/` con
los programas que corren los trabajos (`tisma-slicer-console.exe` y `tisma-fea.exe` en Windows; en Linux el AppImage
de Tisma Slicer ya extraído, `usr/bin/tisma-slicer` y `usr/bin/tisma-fea`), de la última compilación de `main` que
pasó el CI y todavía tiene su paquete (GitHub los guarda 30 días; si no hay ninguna, la última de `dev`). `SOURCE.txt`
dice de qué compilación es. No hace falta instalar Tisma Slicer ni nada más en ninguna máquina: se descomprime el paquete entero y
`tisma-cloud` encuentra los programas solo (la carpeta `tisma-slicer` tiene que quedar junto a `tisma-cloud.exe`).
En Linux los programas usan GTK 3, WebKitGTK 4.1 y hidapi del sistema, como el AppImage (Ubuntu 24.04:
`sudo apt install libgtk-3-0t64 libwebkit2gtk-4.1-0 libhidapi-hidraw0`). La carpeta lleva la licencia de Tisma Slicer (AGPL-3.0) y en
`SOURCE.txt` el código fuente exacto de esa compilación.

**El motor nuevo (tisma-engine):** el paquete trae también `dist/tisma-engine/`, el último *nightly* del motor que
reemplaza a Tisma Slicer. El nodo, y el del maestro, lo encuentran solos. Cada proyecto lo lamina el programa que lee
sus ajustes:
- un proyecto del Tisma nuevo (con `Metadata/tisma_project.json`), con tisma-engine;
- uno de Tisma Slicer, con Tisma Slicer;
- un modelo sin ajustes, con tisma-engine. Si el motor falla por algo que no es del modelo (códigos 1, 2, 3, 6 o un
  cierre inesperado), lo intenta Tisma Slicer, y el trabajo lo dice (`"fallback_from"` en el resumen). Hoy el motor
  lamina solo proyectos guardados por Tisma: a un modelo suelto le responde el código 2, así que esos los lamina
  Tisma Slicer (medido el 06-11 con el *nightly* en Windows y Linux).

Un proyecto nunca se lamina con el programa equivocado: el otro tomaría solo las piezas y laminaría con sus propios
ajustes por defecto, sin avisar. Un nodo al que le falta el programa del proyecto lo rechaza con un código claro
(`needs_engine` o `needs_slicer`). El resumen de cada laminado dice qué programa lo hizo (`"slicer"`).

Se maneja de dos formas, con lo mismo:

- **En la pantalla del servidor:** doble clic en `tisma-cloud.exe` abre la ventana de Tisma Cloud: rol de la máquina,
  configuración (la del maestro o la del nodo; en un nodo, «Buscar Tisma Slicer» encuentra los programas), Iniciar /
  Detener / Reiniciar, «Iniciar con la máquina», en Windows «Abrir el puerto en el firewall», el **Diagnóstico** (lo que
  la máquina necesita para dar servicio, con «Arreglar» para lo que se puede arreglar solo), el registro y, en el
  maestro, la administración:
  - **Nodos: los que piden unirse y qué hace cada uno.** Arriba, las máquinas de la red que piden ser nodos
    (nombre, dirección, procesador y memoria) con «Aceptar» y «Rechazar» (ver «Instalar un nodo»). Abajo, la lista de
    los nodos con su estado; «Qué hace…» marca los tipos de trabajo de cada nodo (laminar, laminar placas grandes,
    análisis estructural, relleno mínimo / resina) y su orden: toma primero los de arriba. Se usa desde su próximo
    trabajo. También habilitar / deshabilitar, «Eliminar» (sus trabajos en curso vuelven a la cola) y «Agregar nodo a
    mano…» para una máquina que no puede pedir unirse (muestra su token para copiarlo).
  - **Panel de control:** el panel del maestro (servidores en línea, qué hace cada uno, cola por tipo, trabajos,
    usuarios y pagos) en su propia ventana, ya con la sesión iniciada. Ahí cada usuario tiene «Hacer interna»: cuenta
    sin cobro y sin límite (ver «Cuentas internas»). La primera vez pide crear el administrador (correo y contraseña).
    En Windows usa el motor Edge del sistema (WebView2); si falta (Windows Server 2019 y 2022 no lo traen) explica
    cómo instalarlo o cómo abrir el panel desde otra PC; en Linux abre el navegador.
  - Usuarios, cola, planes y cualquier comando de administración, y «Cuenta interna…» para un correo.
- **Por SSH:** los mismos pasos con comandos, o con el menú de `tisma-cloud console`:

```
tisma-cloud role master                   # o: role node
tisma-cloud config                        # ver la configuración
tisma-cloud config port 8000              # cambiar una (port, las TISMA_* del maestro, las claves del nodo)
tisma-cloud config master_url http://192.168.1.10:8000   # en un nodo
tisma-cloud start | stop | restart | status
tisma-cloud logs 100
tisma-cloud autostart on                  # que arranque con la máquina
tisma-cloud firewall on                   # Windows: abrir el puerto del maestro en el firewall
tisma-cloud check                         # diagnóstico; check --fix arregla lo que se puede
tisma-cloud admin node-requests          # las máquinas que piden ser nodos
tisma-cloud admin node-accept ID         # aceptar una (node-reject ID para rechazarla)
tisma-cloud admin node-del i7-1          # eliminar un nodo
tisma-cloud admin user-add tu@correo.com --plan basico --admin --password
tisma-cloud admin node-set i7-1 --priorities fea,slice   # qué hace un nodo, en orden
tisma-cloud admin user-set equipo@tisma.com.uy --unlimited   # cuenta interna: sin cobro, sin límite
tisma-cloud console                       # menú con todo lo anterior (opción 10: qué hace cada nodo)
```

Todo queda en la carpeta del programa (o en `TISMA_HOME`): `tisma-cloud.toml` (rol y puerto), `.env` (la
configuración del maestro: al menos `TISMA_DATABASE_URL`, PostgreSQL o `sqlite:///./tisma.db` para probar, y
`TISMA_STORAGE_DIR`), `node.toml` (la del nodo) y `logs/tisma-cloud.log`. Las variables ya definidas en el sistema
tienen prioridad sobre `.env`.

**Si el servicio se cae:** `tisma-cloud` corre como un pequeño supervisor que arranca el maestro o el nodo y lo vuelve a
arrancar si termina con un error (a los 5 s, y si sigue fallando cada vez más espaciado, hasta un minuto). Queda
anotado en el registro.

**Iniciar con la máquina:** en Windows crea la tarea programada «Tisma Cloud» (al encender, como SYSTEM, aunque nadie
inicie sesión; sin límite de tiempo, con prioridad normal y reintento cada minuto si falla): hay que abrir el programa o
la consola SSH como administrador. En Linux crea el servicio `tisma-cloud` de systemd (del sistema como root, del
usuario si no). Con la tarea creada, `start` en Windows la ejecuta, así el servicio sigue andando al cerrar la sesión
SSH. Las tareas creadas por versiones anteriores (con `schtasks /SC ONSTART`) se detenían a los 3 días, que es lo que
pone Windows por defecto: el diagnóstico lo marca y «Arreglar» la crea de nuevo.

**SSH en Windows** (servidor OpenSSH incluido en Windows 10 1809 / Server 2019 y posteriores, según la guía de
Microsoft «Get started with OpenSSH for Windows»), en PowerShell como administrador:

```
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
```

Abrir el puerto 22 solo en la red local (no en el router). Después, desde otra PC: `ssh usuario@IP-del-servidor` y
`cd` a la carpeta de `tisma-cloud.exe`.

#### Windows Server

Probado por el CI en Windows Server 2022 (los equipos de GitHub Actions): tarea de arranque como SYSTEM, regla del
firewall, supervisor que revive el maestro, arrancar y detener. Pasos en el servidor, con una cuenta de administrador:

1. Descomprimir el paquete entero (el artefacto `tisma-cloud-windows`: `tisma-cloud.exe` con la carpeta
   `tisma-slicer` al lado) en una carpeta propia, por ejemplo `C:\TismaCloud\` (ahí quedan su configuración, la base
   SQLite y los archivos; sin `tisma-slicer` el maestro sirve pero no calcula). Abrirlo con clic derecho > *Ejecutar como administrador*.
2. Elegir **Maestro**, revisar la configuración (puerto, base de datos, carpeta de archivos) e **Iniciar**.
3. Marcar **Iniciar con la máquina** y **Abrir el puerto en el firewall** (Windows Server bloquea por defecto las
   conexiones entrantes: sin la regla, los nodos y los clientes no llegan).
4. Pestaña **Diagnóstico**: todo en ✔ o en «!» (avisos). Lo marcado con ✖ se arregla con «Arreglar».
5. **Panel de control:** necesita *Microsoft Edge WebView2 Runtime*, que Windows Server 2019 y 2022 no traen (el
   Diagnóstico dice si está). Se instala gratis desde
   <https://developer.microsoft.com/microsoft-edge/webview2/> (instalador «Evergreen Standalone», x64). Sin él, el
   panel se abre desde el navegador de otra PC de la red: `http://IP-del-servidor:8000/admin`, con el correo y la
   contraseña del administrador (el Internet Explorer de Windows Server no sirve para el panel).
6. En cada **nodo** con Windows: descomprimir el paquete entero (con la carpeta `tisma-slicer`), abrir
   `tisma-cloud.exe` como administrador, elegir **Nodo**, poner la dirección del maestro, dejar el token vacío e
   **Iniciar**: el nodo pide unirse y en el maestro aparece en **Nodos** para aceptarlo (ver «Instalar un nodo»). Los
   programas `tisma-slicer-console.exe` y `tisma-fea.exe` se encuentran solos en la carpeta `tisma-slicer` del paquete
   (o, si no está, en la instalación de Tisma Slicer: «Buscar Tisma Slicer»).
   **Iniciar con la máquina** y **Diagnóstico** igual que en el maestro (un nodo no necesita abrir puertos: él llama
   al maestro).

En Windows Server Core (sin escritorio) no hay ventana: todo se hace por SSH con los comandos o `tisma-cloud console`.

**HTTPS en Windows:** Caddy tiene versión para Windows y puede correr como servicio de Windows (ver «Running Caddy» en
<https://caddyserver.com/docs/running>); usa el mismo `deploy/Caddyfile`. Para internet hay que abrir en el firewall
los puertos 80 y 443 de Caddy, y dejar el del maestro solo para la red local. La base de datos puede ser SQLite para
empezar, o PostgreSQL (instalador oficial para Windows) con `TISMA_DATABASE_URL`.

Los comandos sueltos de antes siguen: `tisma-cloud master [--port P] [--env .env]`, `tisma-cloud node [--config node.toml]`. Para armar el
programa a mano: `pip install ".[master,exe]"` y `pyinstaller tisma-cloud.spec` (queda en `dist/`).

## Escaneos 3D (Tisma Scanner)

La app Tisma Scanner (repositorio `openscanner`) manda sus escaneos (fotos y sensores, de decenas de MB a varios GB)
a Tisma Cloud con la cuenta de la persona, y un nodo con placa de video arma la pieza (fotogrametría con COLMAP y
OpenMVS). Es el tipo de cálculo `scan`, con su propio plan: solo lo puede mandar una cuenta cuyo plan lo incluya.

**El plan:** por ejemplo
`tisma-cloud admin plan-set escaneo --hours 10 --concurrent 1 --classes scan --public` (las horas, el precio y los
nombres los decide la administración). Para sumarlo a un plan que ya existe, `--classes` con `scan` agregado.

**La app** entra con su cuenta (`POST /v1/auth/login`) y manda el escaneo **en partes** de 16 MB
(`TISMA_SCAN_CHUNK_MB`), porque Cloudflare corta los pedidos de más de 100 MB y el teléfono puede perder la conexión:

```
POST   /v1/scans                     {"size": bytes, "name": "pieza.zip"} -> {"id", "chunk_size", "received"}
PUT    /v1/scans/{id}/data?offset=N  una parte, con Content-Length (409 con "received" si hay que seguir desde otro lugar)
GET    /v1/scans/{id}                lo que llegó ("received") y el trabajo cuando terminó de llegar ("job_id")
POST   /v1/scans/{id}/complete       revisa el zip y crea el trabajo: el mismo objeto de GET /v1/jobs/{id}
DELETE /v1/scans/{id}                abandona un escaneo a medio mandar
```

Después sigue como cualquier trabajo: `GET /v1/jobs/{id}` (avance, `summary` con las medidas del informe, o `error`,
`summary.code` y `summary.error_es`), `POST /v1/jobs/{id}/cancel` y `GET /v1/jobs/{id}/result`: un zip con
`pieza.stl` (en mm), `pieza_informe.json` y, si se hicieron, `pieza.ply`, `textura.zip` y `log.txt`.

- El plan se revisa al empezar (402 antes de subir gigas) y otra vez al completar.
- Como mucho `TISMA_MAX_SCAN_GB` (8) por escaneo y `TISMA_MAX_SCAN_UNZIPPED_GB` (12) descomprimido; tres escaneos a
  medio mandar por cuenta; los que nadie toca en un día se borran.
- El zip tiene que ser de Tisma Scanner (`session.json` y fotos JPEG) y sin rutas peligrosas (`..`, absolutas).
- **La posición absoluta nunca sale del teléfono** (decisión del dueño, 04-11-2026): la app no manda `gps.csv`
  (latitud, longitud, altura), solo las poses relativas entre fotos de `frames.csv`. Un escaneo que la traiga (una
  versión vieja de la app) se rechaza con 422 y se borra; el servidor no la guarda ni la necesita.
- El escaneo se borra cuando el trabajo termina; el resultado queda como los demás (`TISMA_RETENTION_H`).

**El nodo** necesita `tisma_scan` en su `node.toml` (el programa de `openscanner`, o su `tools/tisma_scan.py`, que
corre con el Python de la máquina) y `scan` en sus prioridades (`tisma-cloud admin node-set gpu-1 --priorities
scan,fea`): un escaneo ocupa la placa de video, así que cuenta como un análisis (`fea_slots`). Puede durar horas:
`scan_timeout_s` (6 h). Un nodo solo para escaneos no necesita `tisma_slicer` ni `tisma_fea`. El contrato de la línea
de comandos está en `tisma_cloud/node/runners.py`, `run_scan()`:

```
tisma_scan <scan.zip> -o <carpeta> --summary <resumen.json> [--threads <n>] [--no-gpu]
```

con `progress N` en la salida, los archivos de arriba en `<carpeta>`, y en `<resumen.json>` lo que muestra la app o,
si falla, `error`, `code` y `error_es` con código de salida distinto de 0.

### Nodo de escaneo en Windows (por ejemplo el i5 con la GTX 1060)

Un escaneo necesita, además del nodo de Tisma Cloud, tres programas, y vienen juntos en un paquete: el artefacto
**`TismaScan-windows`** del CI de `openscanner` («TismaServer.exe (Windows)»), que es la carpeta `tisma-scan`:

| Programa | En `tisma-scan` | Para qué | Licencia |
|---|---|---|---|
| `tisma_scan.exe` | `tisma_scan.exe` | El pipeline del escaneo, con sus módulos de Python adentro | Del dueño (repositorio `openscanner`) |
| COLMAP 3.9.1 | `colmap\` (`COLMAP-3.9.1-windows-cuda.zip` de <https://github.com/colmap/colmap/releases/tag/3.9.1>, sin cambios) | Fotogrametría; con CUDA y placa NVIDIA también la nube densa | BSD-3-Clause |
| OpenMVS 2.4.0 | `openmvs\` (`OpenMVS_Windows_x64.zip` de <https://github.com/cdcseacave/openMVS/releases/tag/v2.4.0>, sin cambios) | Nube densa, malla y textura en el procesador | AGPL-3.0 (se usa sin cambios, como programa aparte) |

Las licencias van en `tisma-scan\licenses` (también las de las ~40 librerías que traen COLMAP y OpenMVS,
en `BUNDLED_DLLS.txt` y `third-party\`) y el origen de cada uno en `LEEME.txt`. **El paquete corre solo en las máquinas propias y no
se le da a nadie** (decisión del dueño, 2026-11-04): algunas de esas librerías son GPL o LGPL, y dárselo a otra
persona obligaría a entregar también su código fuente (ver `THIRD_PARTY_LICENSES.md`). Es la versión 3.9.1 de COLMAP a
propósito: la 4.x cambió opciones de la línea de comandos que usa el pipeline. El CI de `openscanner` arma el
paquete y lo prueba en Windows (`tisma_scan.exe --check` y un escaneo sintético completo hasta `pieza.stl`, en una
máquina sin placa NVIDIA: la caja de 40 × 30 × 20 mm salió de 42,4 × 32,5 × 20,6 mm, cerrada, en 16 minutos; corrida
41903680499 de `scanner/next`). **Todavía sin probar en un Windows real ni con una placa NVIDIA.**

1. Descomprimir `TismaScan-windows` en la carpeta de `tisma-cloud.exe`, de modo que quede `C:\TismaCloud\tisma-scan\`
   junto a `tisma-cloud.exe` y `tisma-slicer\`. No hace falta ninguna variable de entorno: `tisma-cloud` encuentra
   `tisma-scan\tisma_scan.exe` solo (al hacer el nodo, al iniciar y con «Buscar Tisma Slicer»), y `tisma_scan.exe`
   encuentra su COLMAP y su OpenMVS al lado. Si `TISMA_COLMAP` o `TISMA_OPENMVS` están definidas en el sistema, se
   usan esas.
2. En un nodo: reiniciar el servicio (o `tisma-cloud config tisma_scan C:\TismaCloud\tisma-scan\tisma_scan.exe`). En
   el maestro que calcula, su nodo «maestro» también lo toma al iniciar.
3. Con COLMAP o OpenMVS en otra carpeta: `TISMA_COLMAP=C:\COLMAP\bin\colmap.exe` y `TISMA_OPENMVS=C:\OpenMVS`
   como variables del sistema (Panel de control › Sistema › Variables de entorno, para que las vea el servicio).
   `tisma_scan` le pone a COLMAP lo que prepararía su `COLMAP.bat` (las librerías de `lib` y sus complementos de Qt).
   El `.bat` no sirve para arrancarlo: cmd.exe corta en las comas los parámetros de la cámara. Si alguien lo pone igual
   en `TISMA_COLMAP`, `tisma_scan` usa el `bin\colmap.exe` que tiene al lado.
4. `tisma-cloud check`: «Escaneos 3D» tiene que decir «Listo». Si no, dice qué falta (y el nodo no toma escaneos
   hasta que esté todo: nunca le falla un escaneo a una persona por un programa que falta).
5. En el maestro: `tisma-cloud admin node-set <nodo> --priorities scan,fea,...` (un escaneo usa la placa de video,
   así que cuenta como un análisis: uno a la vez con `fea_slots = 1`).

Sin placa NVIDIA también funciona, más lento: COLMAP sin CUDA hace la fotogrametría en el procesador (`tisma_scan`
lo configura solo) y la nube densa la hace OpenMVS. Probado de punta a punta así en el CI de `openscanner`
(«Escaneo completo en el procesador», un escaneo sintético hasta `pieza.stl`).

## Lista de versiones de Tisma

El maestro sirve la lista firmada de versiones del programa y de la app (`app_updates.md` del motor, regla 13) como
segunda dirección, sin sesión: `GET /v1/updates` (`updates.json`) y `GET /v1/updates.sig` (su firma Ed25519, 64 bytes).
Las baja cada hora de la release `updates` del motor en GitHub (`TISMA_UPDATES_URL`; vacía: no se sirve), las guarda
en `storage/updates/` y las devuelve byte por byte: la firma es sobre los bytes exactos del JSON, así que el maestro
nunca lo vuelve a escribir. No comprueba la firma (eso lo hacen los programas, con la clave del proyecto); solo cambia
la lista guardada por una descarga entera (un JSON y una firma de 64 bytes). Hasta la primera descarga responde 503.

## Biblioteca entre usuarios (red de pares)

Los modelos que la gente publica viajan de una app a otra; el central **no guarda ni sirve los archivos** (ficha
`p2p_library.md` del motor). Guarda solo:

- **el índice**: por modelo, nombre, autor (un id de cuenta que no es el correo), licencia, miniatura, tamaño, el
  resumen del análisis y la huella SHA-256 de cada archivo, firmados por la clave de la app que lo publicó. Sin G-code:
  se publican el proyecto, la receta (perfil de impresión y de filamento), los análisis y fotos, y cada uno lamina en
  su equipo o en la nube;
- **la lista de bloqueo**: huellas retiradas, cuentas suspendidas, claves de apps, claves de biblioteca revocadas
  (`TISMA_LIBRARY_REVOKED_KEYS`) y, por 24 h como mucho, direcciones IP. Una IP sale en la lista solo como huella
  (SHA-256 con una sal que cambia cada día) y al vencer se borra de la base.

Las dos se bajan sin sesión con su firma Ed25519 aparte (64 bytes): `GET /v1/library/index.json` y `.sig`,
`GET /v1/library/blocklist.json` y `.sig`. Las firma la **clave de la biblioteca**, `storage\library.key`, que el central
hace la primera vez. La clave del proyecto vive solo en el USB del dueño, que certifica la de la biblioteca **una sola
vez**:

```
tisma-cloud admin library-key                      # en el maestro: la clave de la biblioteca (base64)
python deploy/certify_library_key.py --key E:\tisma\clave.pem --library-key <base64> --public-key <64 hex>
                                                   # en la computadora del dueño, con el USB (openssl, como sign_release.py)
tisma-cloud admin library-cert <certificado>       # en el maestro, con TISMA_LIBRARY_PROJECT_KEY (64 hex) puesto
```

Hasta tener el certificado la biblioteca no se sirve (503); `TISMA_LIBRARY_UNCERTIFIED=on` la sirve igual, solo para
probar (las apps no usan una lista sin certificado). `GET /v1/library/key` da la clave y su certificado. Publicar
(`POST /v1/library/models`) manda solo los datos y las huellas, con la miniatura WebP (64 KB como mucho). «Denunciar»
(`POST /v1/library/reports`) llega al panel; ahí una persona retira el modelo (sus huellas pasan a la lista) o descarta
la denuncia, y bloquea cuentas, claves o direcciones (`/v1/admin/library/...`, solo red local). `library.key` va
**aparte**, como `data.key`: si se pierde o la roban, se certifica una clave nueva y la vieja va a
`TISMA_LIBRARY_REVOKED_KEYS`.

**Pares e intermediarios** (libp2p, acordado con el motor el 10-11-2026): cada app anuncia al central qué modelos
comparte y sus direcciones, firmado con su clave (`POST /v1/library/peers/announce`, cada 10 min). Eso queda **solo
en memoria** 30 min. `GET /v1/library/peers?model=` da con quién conectarse: la dirección propia solo entre dos apps
en «Directo»; si no, la dirección a través de un intermediario de Tisma (`TISMA_LIBRARY_RELAYS`, sus multiaddr). Un
intermediario acepta solo con un permiso del central (`POST /v1/library/relay-ticket`, firmado con la clave de la
biblioteca, 1 h) que trae la cuenta y el tope por hora del panel. Una IP bloqueada no anuncia ni recibe permisos.

**Piezas escaneadas en la cuenta** (regla 7 bis): «Imprimir» en la app del escáner guarda la pieza de un escaneo
terminado en la cuenta, privada (`POST /v1/files` con el trabajo; se toma del resultado, que dura `TISMA_RETENTION_H`).
Tisma la lista en «Mi biblioteca» (`GET /v1/files`) y la baja para laminar; «Publicar» la manda a la biblioteca con
`from_file`. Cada cuenta guarda hasta `TISMA_ACCOUNT_FILES_GB` (5 GB), en `storage\files`; se borran con la cuenta.

## Avisos de problemas

El central revisa cada minuto y avisa por correo a los administradores (las cuentas con `--admin`, o las direcciones
de `TISMA_ALERT_EMAILS`) cuando:

- un nodo que ya estuvo conectado no responde hace más de `TISMA_ALERT_NODE_MINUTES` (5);
- un trabajo espera en la cola más de `TISMA_ALERT_QUEUE_MINUTES` (30): ningún nodo hace ese tipo de trabajo, o están
  todos ocupados o caídos;
- quedan menos de `TISMA_ALERT_DISK_FREE_GB` (5) GB libres en el disco del central.

Si el disco del central se llena del todo, lo que no entra (un proyecto, un escaneo, un resultado) se responde con 507 y
«el servidor no tiene lugar para archivos ahora» en vez de un error del servidor; un escaneo que no entraría se rechaza antes
de mandarlo, y un nodo vuelve a mandar su resultado más tarde.

Cada problema manda un correo cuando empieza, un recordatorio cada `TISMA_ALERT_REPEAT_H` horas (12) mientras sigue, y
otro cuando se resuelve. Los activos aparecen en rojo arriba del panel `/admin` (y los resueltos de la semana). Los
correos usan el mismo servidor SMTP que los códigos de las cuentas.

## Copias de seguridad

**Con el programa `tisma-cloud` (Windows o Linux, sin Docker):** el maestro copia su base de datos solo, una vez por
día después de las `TISMA_BACKUP_HOUR` (UTC, por defecto 3; si estaba apagado a esa hora, al arrancar). Con SQLite, la
base del programa por defecto, las copias van a `backups\daily\` al lado de la carpeta `storage` (o a
`TISMA_BACKUP_DIR`); cada una se revisa (`PRAGMA integrity_check`) antes de darla por buena, se guardan las últimas
`TISMA_BACKUP_DAYS` (14) y una por semana (domingo) en `backups\weekly\` durante `TISMA_BACKUP_WEEKS` semanas (8).
Con PostgreSQL hace falta poner `TISMA_BACKUP_DIR` y tener `pg_dump` y `pg_restore` instalados. `TISMA_BACKUP_DIR=off`
las apaga. Si una copia falla, llega el aviso por correo y queda en rojo en `/admin`; el Diagnóstico dice cuándo fue la
última.

```
tisma-cloud backup                                     # una copia ahora
tisma-cloud stop
tisma-cloud restore backups\daily\tisma-AAAAMMDD-HHMMSS.db   # la base que reemplaza queda como tisma.db.before-restore
tisma-cloud start
```

Copiar también la carpeta `backups` fuera de la máquina (otro disco, un nodo, un pendrive), y guardar aparte, una vez,
`storage\data.key` (la clave de los datos personales, que a propósito no va en las copias) y el archivo `.env`.

**Con Docker:**

El servicio `backup` de `compose.master.yaml` copia la base de datos (cuentas, pagos, planes, trabajos) todos los días a
las `BACKUP_HOUR` (UTC, por defecto 3) en `BACKUP_DIR` (por defecto `deploy/backups`), con `pg_dump`. Cada copia se
verifica antes de darla por buena (`pg_restore --list`); se guardan las últimas `BACKUP_DAYS` diarias (14) y una por
semana durante `BACKUP_WEEKS` semanas (8). Los proyectos y resultados de los trabajos no se copian: se borran solos a
los pocos días.

- **Fuera del central:** una copia en el mismo disco no sirve si el disco se rompe. Poner `BACKUP_DIR` en otro disco o
  copiarla a otra máquina, por ejemplo desde un nodo, todas las noches:
  `rsync -a usuario@central:/ruta/de/backups/ /srv/tisma-backups/` (cron).
- **Copia ahora:** `docker compose --env-file .env -f deploy/compose.master.yaml exec backup backup.sh now`
- **Restaurar** (reemplaza el contenido de la base de datos, en una sola transacción):

```
docker compose --env-file .env -f deploy/compose.master.yaml stop api
docker compose --env-file .env -f deploy/compose.master.yaml exec backup restore.sh /backups/daily/tisma-AAAAMMDD-HHMM.dump
docker compose --env-file .env -f deploy/compose.master.yaml start api
```

- **También guardar aparte** el archivo `.env` y la clave de la cuenta de servicio de Google Play: no están en la base de
  datos.
- **La clave de los datos personales** (`data.key`) no va en las copias, a propósito: una copia robada no sirve sin
  ella. Guardarla una vez en otro lugar (un gestor de contraseñas, un pendrive guardado):
  `docker compose --env-file .env -f deploy/compose.master.yaml exec api cat /data/data.key`.
  Para restaurar en otra máquina, ponerla de nuevo en el volumen `storage` o en `TISMA_DATA_KEY` antes de arrancar.

### Simulacro de restaurar (`deploy/restore_drill.py`)

Los pasos de arriba con los comandos de `tisma-cloud`, como después de perder el disco: un maestro con cuentas y trabajos
terminados, su copia, y en una máquina vacía la copia más `data.key`, `tisma-cloud restore` e iniciar. Comprueba que
los tokens de las personas siguen andando con su correo, que están todas las cuentas, sus trabajos y sus horas usadas,
y que **sin `data.key` el maestro no arranca** y dice por qué (no sirve datos que no puede leer). Lo corre el CI en
Windows y Linux con el programa del paquete. Medido (SQLite, 300 cuentas): la copia 0,35 s (424 KB), restaurarla
0,08 s y el maestro nuevo responde 1,3 s después de iniciarlo (Linux); con el `.exe` en Windows Server 2022 (30
cuentas), restaurar 3 s y responder 7,8 s después de iniciarlo. Lo que más tarda es copiar los archivos a la otra
máquina.

```
python deploy/restore_drill.py --users 300 --exe C:\TismaCloud\tisma-cloud.exe --folder D:\simulacro
```

Restaurar también `storage\library.key` y `storage\library.cert` (la biblioteca entre usuarios). Sin ellos el maestro
hace una clave nueva, lo dice en el registro y la biblioteca responde 503 hasta certificarla otra vez (README,
«Biblioteca entre usuarios»).

### El maestro también calcula

Con una sola máquina (o para aprovechar la del maestro) el maestro también corre trabajos: es la opción **«El maestro
también calcula»** de la ventana (`tisma-cloud config master_computes true|false`), activada salvo que se apague. El
servicio registra en la base del maestro un nodo **«maestro»** con su token, escribe `master-node.toml` (conectado a
`http://127.0.0.1:PUERTO`, con los programas de la carpeta `tisma-slicer` del paquete) y lo corre junto al maestro,
cada uno reiniciado aparte si falla. Qué hace se cambia como en cualquier nodo («Qué hace…»); los trabajos a la vez,
en `master-node.toml`. Si faltan los programas (la carpeta `tisma-slicer` no quedó junto al programa), el maestro
atiende sin calcular y el Diagnóstico lo dice.

### IP pública que cambia: dirección automática y DNS dinámico

En Uruguay las conexiones de casa y de oficina tienen la IP pública dinámica. Con **`TISMA_PUBLIC_URL` vacía** el
maestro usa como dirección pública `http://<IP pública>:<puerto>`: cada 5 minutos pregunta su IPv4 a
`TISMA_PUBLIC_IP_URL` ([ipify](https://www.ipify.org/), `https://api.ipify.org`, que responde la IP en texto). Para
que los clientes y los nodos de afuera tengan un nombre fijo, un **DNS dinámico** como [No-IP](https://www.noip.com/):

1. Crear la cuenta y un nombre (por ejemplo `tisma.ddns.net`) en No-IP.
2. En el maestro: `TISMA_DDNS_HOST=tisma.ddns.net`, `TISMA_DDNS_USER=` (el correo de la cuenta) y
   `TISMA_DDNS_PASSWORD=` (en la ventana: «DNS dinámico»). La dirección pública pasa a ser
   `http://tisma.ddns.net:<puerto>`.
3. En el router: abrir el puerto del maestro hacia esta máquina; los nodos de otras redes usan
   `master_url = "http://tisma.ddns.net:8000"`.

El maestro actualiza el nombre con el protocolo de No-IP ([pedido](https://www.noip.com/integrate/request),
[respuestas](https://www.noip.com/integrate/response)) **solo cuando cambia la IP** (No-IP bloquea a los clientes que
actualizan sin cambio) y recuerda la última IP enviada en `public-address.json` de la carpeta de archivos. `good` y
`nochg` son éxito; `nohost`, `badauth`, `badagent`, `!donator` y `abuse` detienen las actualizaciones hasta que se
corrija la configuración; `911` se reintenta a los 30 minutos. El Diagnóstico muestra la dirección y el estado del DNS
dinámico. Otro servicio del mismo protocolo (dyndns2) se usa con `TISMA_DDNS_URL`.

Sin dominio propio la dirección automática es HTTP, sin cifrar: para HTTPS hace falta un nombre (el del DNS dinámico
sirve) y Caddy delante del maestro (ver «Instalar el maestro»).

## Actualizar Tisma Cloud

El maestro y cada nodo se actualizan igual: `tisma-cloud stop`, una copia de seguridad del maestro (`tisma-cloud
backup`), poner el paquete nuevo en la misma carpeta encima del viejo (el `.exe`, `tisma-slicer\`, `tisma-engine\` y,
en un nodo de escaneo, `tisma-scan\`) y `tisma-cloud start`. La configuración (`tisma-cloud.toml`, `.env`,
`node.toml`), la base de datos y `storage\data.key` no se tocan. Al arrancar, el maestro pone la base al día solo
(tablas y columnas nuevas, `db.py`; nunca borra una). Conviene actualizar primero el maestro y después los nodos; un
nodo viejo sigue trabajando con un maestro nuevo mientras tanto.

**Simulacro de actualizar (`deploy/upgrade_drill.py`):** instala la versión del 24-10-2026 (la primera con los
comandos del servicio) desde la historia de este repositorio, le carga cuentas, trabajos terminados, un nodo
registrado y un `node.toml`, y arranca la versión actual sobre la misma carpeta. Comprueba que están todas las
cuentas, que los tokens viejos siguen entrando con su correo, que se conservan los trabajos y las horas usadas, que el
token del nodo viejo sigue sirviendo, que un trabajo nuevo va de punta a punta y que el `node.toml` viejo se lee. Lo
corre el CI con SQLite y con PostgreSQL. Medido (20 cuentas): la versión nueva responde 1,3 s (SQLite) y 1,6 s
(PostgreSQL) después de iniciarla sobre la base vieja.

```
python deploy/upgrade_drill.py --old-ref a7a9805 [--database-url postgresql+psycopg://...]
```

`/v1/updates` es otra cosa: la lista firmada de versiones de los programas de Tisma (la app y el de escritorio), que el
maestro sirve como segunda dirección; no actualiza el servidor.

## Instalar un nodo

Los nodos están en la misma red que el maestro. Un nodo **sin token** pide unirse solo:

1. En la máquina del nodo: rol **Nodo**, la dirección del maestro (`http://IP-del-maestro:8000`), el token vacío e
   **Iniciar**. El nodo le pide al maestro ser un nodo con el nombre de la máquina (o el de «Nombre con el que pide
   unirse») y espera la respuesta; el Diagnóstico del nodo dice «Pidió unirse al maestro».
2. En el maestro, **Nodos** muestra la máquina en «Piden unirse» (también el panel web y `admin node-requests`).
   «Aceptar» crea el nodo, con todos los tipos de trabajo (después se eligen con «Qué hace…»); el nodo toma su token
   en unos segundos, lo guarda en su `node.toml` y empieza a trabajar. «Rechazar» detiene el servicio del nodo.
3. **Eliminar** un nodo devuelve a la cola los trabajos que estaba haciendo. Si la máquina sigue encendida, el maestro
   ya no la reconoce y vuelve a pedir unirse (rechazarla la detiene); deshabilitar un nodo, en cambio, lo deja
   esperando sin pedir nada.

Pedir unirse solo se acepta desde la red local (`TISMA_ADMIN_NETWORKS`): desde internet esa dirección no existe, como
la administración. Cada pedido tiene un secreto que solo conoce la máquina que lo hizo (el token se entrega una sola
vez, a ella), hay como mucho 50 pedidos esperando y los que nadie responde se olvidan al día.

Un nodo fuera de la red, o hecho a mano: en el maestro «Agregar nodo a mano…» o
`python -m tisma_cloud.admin node-add i7-1 --priorities slice,slice_large,fea` (muestra el token una sola vez), y ese
token en el `node.toml` del nodo.

En la máquina del nodo:

- **Con el programa `tisma-cloud`** (Windows o Linux): ver «Programa tisma-cloud» y «Windows Server» arriba.
- **Sin Docker** (Linux o Windows): instalar Tisma Slicer y Python 3.11+, `pip install .`, copiar
  `deploy/node.example.toml` a `node.toml`, completar las rutas de `tisma-slicer` (en Windows
  `tisma-slicer-console.exe`) y `tisma-fea`, y correr `TISMA_NODE_TOKEN=tn_... python -m tisma_cloud.node --config node.toml`.
- **Con Docker** (Linux): copiar los programas de una compilación de Tisma para Linux a `deploy/tisma/`, crear
  `deploy/node.toml` y correr `TISMA_NODE_TOKEN=tn_... docker compose -f deploy/compose.node.yaml up -d`.

**Con el slicer nuevo (`tisma-engine`):** poner su ruta en `tisma_engine` del `node.toml` (o en la ventana, «Programa
tisma-engine»). Con eso el nodo corre todos los trabajos con el motor y no necesita `tisma-slicer` ni `tisma-fea`; sin
ella, sigue usando los programas de Tisma Slicer. El nodo le pasa el proyecto, la salida y un resumen JSON, con
`--sandbox` (sin scripts de posproceso ni complementos), y lee el avance de las líneas `progress N`: el contrato está en
`docs/spec/features/cloud.md` del motor (reglas 18 a 21). El resultado de un análisis del motor es un archivo `.tea`.

Sugerencia para tus equipos:

| Equipo | Prioridades | `slots` |
|---|---|---|
| i5-9400F + GTX 1060 (también maestro) | `fea_optimize,fea,slice_large,slice` | 0 (automático: 3, 6 hilos) |
| i7 3.ª gen. 16 GB (×2) | `slice,slice_large,fea` | 0 (automático: 4, 8 hilos) |

## Capacidad: más usuarios con menos espera

Medido con `tisma-slicer` en 4 núcleos (Xeon 2,1 GHz, 16 GB):

- Un trabajo de laminado deja ocupados unos 3 de los 4 núcleos (partes del laminado no se reparten), y arrancar el
  programa cuesta 0,14 s: no hace falta dejarlo abierto entre trabajos. Placas de pocos miles de triángulos tardan
  menos de 1 s; un Benchy de 225 000 triángulos, de 6 a 13 s según su tamaño, con 0,3 a 0,7 GB de memoria.
- De punta a punta (maestro, un nodo y 12 placas mezcladas enviadas juntas):

  | Nodo | Todo listo en | Tiempo hasta el resultado (promedio) | (mediana) |
  |---|---|---|---|
  | 1 trabajo a la vez, cola por orden de llegada (antes) | 54,1 s | 33,5 s | 30,4 s |
  | 1 a la vez, los cortos primero | 53,0 s | 15,6 s | 3,9 s |
  | `slots = 0` (2 a la vez) y los cortos primero (ahora) | 44,0 s | 13,5 s | 3,4 s |

Por eso:

- **La cola toma primero los trabajos cortos.** Cada trabajo tiene su lugar en la cola: cuándo llegó más su duración
  estimada (por sus triángulos; los análisis, por su calidad). Uno corto pasa a uno largo que llegó un poco antes, y
  uno largo nunca espera más de `TISMA_QUEUE_SIZE_WAIT_MAX_S` (600 s) por encima del orden de llegada. La prioridad
  del plan sigue yendo primero.
- **Cada nodo corre varios trabajos a la vez**: `slots = 0` (automático) es uno cada dos núcleos y 2 GB de memoria.
  En un nodo con `tisma_engine` es uno por núcleo y 1 GB de memoria: cada laminado del motor ocupa hoy un núcleo, y
  medido en 4 núcleos (Benchy de 225 000 triángulos), dos a la vez dan 1113 laminados por hora y cuatro a la vez,
  1737 (con `tisma-slicer` y sus capas por defecto, el mejor caso es 1462). El laminado más grande de la prueba usó
  0,3 GB.
- **Un análisis estructural a la vez por nodo** (`fea_slots = 1`): usan mucha memoria y la placa de video; mientras
  corre uno, los otros lugares del nodo siguen laminando.

### Medido con carga (`deploy/loadtest.py`)

El maestro real con SQLite (como lo instala el programa), en una máquina de 4 núcleos: usuarios que mandan 5 trabajos
cada uno y los consultan cada 2 s, como Tisma y la app, y nodos simulados que los toman y devuelven el resultado al
momento (mide la API, no el cálculo).

| Usuarios | Trabajos | Nodos | Terminados | Fallidos | Consulta (mediana / p95) | Mandar (mediana / p95) |
|---|---|---|---|---|---|---|
| 50 (antes del arreglo) | 250 | 4 | 232 | 18 | 4 / 7 ms | 36 / 317 ms |
| 100 | 500 | 8 | 500 | 0 | 4 / 8 ms | 30 / 444 ms |

Los 18 fallidos eran resultados de los nodos rechazados (429) porque usaban los mismos 8 lugares de subida que los
usuarios: ahora un resultado nunca espera esos lugares, y el nodo reintenta mandarlo si el maestro está ocupado o
reiniciando (hasta 15 min) en vez de dar el trabajo por fallido. Las subidas de los usuarios por encima de 8 a la vez
reciben 429 con `Retry-After` y se reintentan.

**Con escaneos 3D** (`--scans 1 --scan-mb 40`): 50 usuarios que, al mismo tiempo, suben cada uno un escaneo de 40 MB en
partes de 16 MB (como Tisma Scanner) y mandan 4 trabajos de laminado; 8 nodos. Los 250 trabajos (50 escaneos y 200
laminados) terminaron y ninguno falló: 2 GB subidos en 30 s, en la misma máquina. Crear la subida tardó 0,65 / 3,4 s
(mediana / p95), cada parte 0,49 / 2,9 s, cerrar el escaneo 46 / 214 ms. 94 de 244 partes recibieron 429 por los lugares
de subida y se mandaron de nuevo después de `Retry-After`, como hace la app. Las consultas siguieron en 5 / 100 ms.
Por internet manda el ancho de banda de subida de cada persona, no el maestro.

```
TISMA_DATABASE_URL=... TISMA_STORAGE_DIR=... python deploy/loadtest.py --url http://127.0.0.1:8000 --users 100 --nodes 8
TISMA_DATABASE_URL=... TISMA_STORAGE_DIR=... python deploy/loadtest.py --url http://127.0.0.1:8000 --users 50 --jobs 4 --nodes 8 --scans 1 --scan-mb 40
```

### Puentes conectados (impresoras remotas)

Cada puente (Tisma abierto en una PC) tiene siempre abierta una espera de órdenes. Medido en la misma máquina (4
núcleos, maestro, PostgreSQL 16 y los puentes simulados juntos; un usuario haciendo 20 consultas por segundo y el
teléfono mandando órdenes mientras tanto):

| Versión | Puentes | CPU del maestro | Consulta normal (mediana / p95) | Orden hasta el puente (mediana / p95) |
|---|---|---|---|---|
| Antes: cada espera mira la base cada 1 s | 250 | 112 % (saturado) | 1029 / 1450 ms | 1491 / 2335 ms |
| Antes, con el límite de 300 conexiones | 1000 | — | 93 % de las consultas rechazadas (503) | — |
| Antes, con el límite subido | 1000 | 107 % (saturado) | 18 311 ms | 12 645 ms |
| Ahora: aviso en memoria, la base solo cuando hay algo | 250 | 34 % | 13 / 24 ms | 28 / 55 ms |
| Ahora | 2000 | 40 % | 13 / 44 ms | 32 / 109 ms |

Con 4000 y 8000 puentes el maestro usó 44 % y 65 % de un núcleo y 225 y 318 MB de memoria, pero los puentes simulados
ocupaban casi los 4 núcleos de la máquina de prueba y las demoras medidas son del simulador: falta medirlo en el i5
con los puentes simulados en otra máquina.

Por eso:

- **Una espera no pregunta nada a la base mientras no haya novedades** (`wakeup.py`, `presence.py`): una orden nueva
  despierta en el momento la espera de su puente; de quién es cada puente, cuándo se lo vio y su último estado están en
  memoria y se escriben juntos cada 5 s; el estado que solo cambia lecturas (temperaturas, avance) se escribe cada 60 s.
  Las cuentas de los tokens se recuerdan 60 s. Es un solo proceso de maestro (como se instala); otro proceso sobre la
  misma base vería a los puentes unos segundos tarde.
- **Hasta `TISMA_MAX_CONNECTIONS` conexiones a la vez** (4000; antes 300): cada puente y cada nodo esperando ocupa una.
  `tisma-cloud master` sube el límite de archivos abiertos del proceso; con Docker lo da `ulimits` en
  `compose.master.yaml`.
- Lo que más cuesta ahora es cuando muchos puentes llegan juntos (al reiniciar el maestro, todos vuelven a la vez):
  cada uno escribe una vez en la base al llegar.

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
python -m tisma_cloud.admin node-del i7-2                 # eliminar (sus trabajos en curso vuelven a la cola)
python -m tisma_cloud.admin node-requests                 # máquinas que piden ser nodos
python -m tisma_cloud.admin node-accept ID --name i7-3    # aceptar una (node-reject ID: rechazarla)
python -m tisma_cloud.admin queue                         # trabajos esperando y corriendo por clase
python -m tisma_cloud.admin user-list                     # plan, si está al día, horas usadas
python -m tisma_cloud.admin user-set ana@correo.com --paid-until 2026-12-31
python -m tisma_cloud.admin user-set equipo@tisma.com.uy --unlimited   # cuenta interna (ver «Cuentas internas»)
```

La misma información está en la API de administración (`/v1/admin/...`, con el token de un administrador).

## Problemas informados (catálogo para programar)

Tisma (Más > Ayuda > Informar de un problema) y la app mandan los problemas que encuentra la gente a
`POST /v1/reports`: qué pasó, los pasos, qué se esperaba, la versión, el sistema y, si la persona quiere, el registro
de Tisma. Con cuenta o sin ella (sin cuenta, 5 por hora por dirección, `TISMA_ANONYMOUS_REPORTS_PER_HOUR`; con cuenta,
20, `TISMA_REPORTS_PER_HOUR`; 512 KB como mucho).

- **Nada personal** (`reports.py`, `redact`): antes de guardarlos se quitan de los textos y del registro los correos,
  las direcciones IP y MAC, el usuario del sistema en las rutas (`C:\Users\<user>`, `/home/<user>`), los tokens, las
  claves, los códigos de acceso y los números de serie de las impresoras; del registro quedan las líneas de error con
  dos alrededor y las últimas 40 (20 000 caracteres). Además se guardan cifrados, como los demás datos personales. La
  dirección de quien informa no se guarda.
- **Agrupados por problema**: los informes con la misma primera línea de error del registro (sin números, rutas ni
  horas), o sin registro con las mismas palabras en el título, son el mismo problema (`signature`). Quien informa ve
  cuántos informaron lo mismo.
- **Tipo de error, palabras clave y resumen** (`triage.py`, en el maestro, sin servicios de afuera): cada informe se
  clasifica por las palabras que usa, en español, inglés o portugués (cierre o cuelgue, laminado, soportes, impresora y
  conexión, cámara, calibración, resina, ingeniería, cuenta y nube, vista 3D, interfaz, perfiles y ajustes,
  rendimiento, otro; el equipo lo puede cambiar a mano y queda). Sus palabras clave son las que más lo distinguen de
  los demás informes (TF-IDF), y su resumen junta el tipo, el título, las frases que más dicen, el primer error del
  registro y la impresora: sirve cuando el informe está mal explicado.
- **Traducción automática** (opcional, `translate.py`): con `TISMA_TRANSLATE_URL` los informes en otro idioma se
  traducen al del equipo (`TISMA_TRANSLATE_TO`, `es`) con [LibreTranslate](https://docs.libretranslate.com) corriendo
  en el mismo servidor (`docker compose --profile translate up -d`, servicio `libretranslate`; unos 700 MB de modelos
  y 0,4 a 0,8 s por frase en el procesador): nada sale del servidor. Se guardan el original y la traducción (cifrados),
  y se busca en los dos. Un traductor general no conoce las palabras de la impresión 3D («slicing» puede salir «cortar»):
  el título original va siempre al lado del traducido.
- **Catálogo**: el panel (sección «Problemas informados») muestra los problemas abiertos, el más informado primero, y
  se filtra por tipo de error, estado, app, versión, tipo de informe (error, cierre, sugerencia), impresora, sistema,
  idioma, fechas, solo con cuenta o premiados, y se busca por palabras (en cualquier idioma, sin importar mayúsculas ni
  tildes; tocar una palabra clave la busca) o por campos: `impresora:kobra`, `version:0.9`, `sistema:windows`,
  `idioma:pt`, `error:out_of_range`, `tipo:soportes`, `titulo:…`, `app:android` (entre comillas para varias palabras:
  `impresora:"bambu lab"`); se ve agrupado por problema o la lista de todos los informes
  con su texto original y traducido. Descarga el catálogo con los mismos filtros en Markdown (una sección por problema:
  el tipo, el resumen, las palabras clave, los pasos, lo esperado, lo que pasó, su traducción y el registro) o en
  JSON lines (un problema por línea), para dárselo al programador o a la IA que programa. Cada problema se marca
  confirmado, arreglado o que no se arregla (todos sus informes juntos), y sale del catálogo al arreglarlo. Por comandos:

```
python -m tisma_cloud.admin reports --out problemas.md            # los abiertos (nuevos y confirmados)
python -m tisma_cloud.admin reports --format jsonl --app android
```

- **Premio a quien informa**: cuando el equipo ve que un informe sirvió, lo premia a mano («Premiar» en el panel,
  `POST /v1/admin/report-groups/{signature}/reward` para el primero que lo informó con cuenta, o
  `POST /v1/admin/reports/{id}/reward`): su plan queda pago `TISMA_REPORT_REWARD_DAYS` días más (7; el administrador
  puede dar otros, hasta 90), sumados al final de lo ya pago, y le llega un correo. Una vez por informe, solo con
  cuenta y como mucho `TISMA_REPORT_REWARDS_PER_YEAR` (4) por cuenta en un año: informar no es una forma de tener el
  plan gratis. Quien informó lo ve en sus informes (`reward_days`). Si la página web fija `paid_until` con
  `PUT /v1/site/users`, la fecha que manda reemplaza los días premiados: la página tiene que sumar sobre la del servicio.

## API

| Quién | Método y ruta | Qué hace |
|---|---|---|
| todos | `GET /v1/health`, `GET /v1/plans` | estado, planes |
| usuario | `POST /v1/auth/login` `{email, password, device}` | token del dispositivo |
| usuario | `POST /v1/auth/logout`, `GET /v1/me` | cerrar sesión; plan, al día, horas, trabajos activos |
| usuario | `GET /v1/me/sessions`, `DELETE /v1/me/sessions/{id}`, `POST /v1/me/sessions/close-others` | dispositivos con sesión abierta; cerrar uno o todos los demás |
| usuario | `POST /v1/me/password` `{current_password, new_password}` | cambiar la contraseña (cierra las otras sesiones) |
| usuario | `GET /v1/me/export` | una copia (JSON) de todo lo que el servicio guarda de la cuenta |
| todos | `POST /v1/reports` `{app, kind, title, description, steps, expected, actual, app_version, os, log}` | informar un problema (con o sin token); responde su `signature` y cuántos `similar` hay |
| usuario | `GET /v1/me/reports` | los problemas informados con la cuenta y su estado |
| administrador | `GET /v1/admin/reports`, `GET /v1/admin/reports/list`, `GET /v1/admin/reports/catalog?format=md\|jsonl` (filtros `status`, `category`, `app`, `kind`, `version`, `q`), `GET\|PATCH /v1/admin/reports/{id}`, `PATCH /v1/admin/report-groups/{signature}` `{status, note, category}` | problemas agrupados o todos, catálogo, estado, tipo de error |
| administrador | `POST /v1/admin/reports/{id}/reward`, `POST /v1/admin/report-groups/{signature}/reward` `{days}` | premiar un informe útil con días del plan |
| usuario | `POST /v1/me/delete/code`, `POST /v1/me/delete` `{password}` o `{code}` | borrar la cuenta y sus datos |
| usuario | `GET /v1/billing/play/account`, `POST /v1/billing/play/purchase` `{purchase_token}` | suscripción de Google Play (app) |
| Google | `POST /v1/billing/play/notify` | avisos de Google Play (Pub/Sub, con token de Google) |
| usuario | `POST /v1/jobs` (multipart: `kind` slice\|fea, `params` JSON, `file` 3MF) | enviar un trabajo |
| usuario | `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result`, `POST /v1/jobs/{id}/cancel` | seguir, descargar, cancelar |
| página web | `PUT /v1/site/plans/{nombre}`, `PUT /v1/site/users/{id_web}`, `GET /v1/site/users/{id_web}` | planes, usuarios, pagos |
| nodo | `POST /v1/node/claim`, `/heartbeat`, `GET /v1/node/jobs/{id}/input`, `POST .../progress`, `.../result`, `.../fail` | trabajar |
| máquina de la red | `POST /v1/node/join` `{name, info}`, `GET /v1/node/join/{id}` (cabecera `X-Join-Secret`) | pedir ser un nodo y recibir su token una vez aceptada (solo red local) |
| administrador | `/v1/admin/nodes`, `DELETE /v1/admin/nodes/{nombre}`, `/v1/admin/node-requests` (`POST .../{id}/accept`, `.../reject`), `/v1/admin/users`, `/v1/admin/plans/{nombre}`, `/v1/admin/queue` | administrar |
| puente (PC) | `PUT /v1/bridges/{id}` `{name, printers}`, `GET /v1/bridges/{id}/commands?wait=20` | impresoras y su estado; esperar órdenes (las dos respuestas traen `state_s`: cada cuánto mandar el estado) |
| puente (PC) | `GET /v1/bridges/{id}/commands/{orden}/file`, `POST /v1/bridges/{id}/commands/{orden}` `{ok, error}`, `POST /v1/bridges/{id}/printers/{imp}/camera` (JPEG) | archivo a imprimir, resultado, imagen de la cámara |
| usuario | `GET /v1/remote/printers`, `DELETE /v1/remote/bridges/{id}` | impresoras remotas, quitar un puente |
| usuario | `POST /v1/remote/bridges/{id}/printers/{imp}/print` (multipart `file`, G-code), `.../pause`, `.../resume`, `.../stop`, `GET /v1/remote/commands/{orden}`, `GET .../camera` | imprimir y controlar desde afuera |

Parámetros de `fea`: `mode` (`analyze`, `lightest-infill`, `resin-structure`), `quality` (`fast`, `normal`, `high`,
`ultra`), `object`, `instance`, `solid`, `zones`, `ribs`. La documentación interactiva queda en `/docs`, solo desde la red local, como `/admin` (lista todas las rutas, también las de la administración y los nodos).

Para agregar otro tipo de trabajo: una clase en `jobs.py` (con sus parámetros) y un ejecutor en `node/runners.py`.

## Impresoras remotas (como Bambu Handy)

Tisma abierto en una PC de la casa hace de **puente**: manda a la cuenta sus impresoras de la red local y su estado, y
espera órdenes (`GET .../commands`, que espera hasta 20 s). El teléfono las ve desde cualquier lado y manda un G-code a
imprimir, pausa, reanuda o cancela; la cámara se pide desde el teléfono y el puente manda imágenes solo mientras alguien
mira. No hace falta abrir puertos en la casa: el puente siempre se conecta hacia afuera.

- El puente manda su estado cada `state_s` segundos (viene en cada respuesta): **3 s mientras alguien mira** sus
  impresoras o una cámara, **30 s** el resto del tiempo. Cuando el teléfono empieza a mirar, la espera de órdenes del
  puente termina en el momento (sin órdenes) para que mande su estado ya.
- Las imágenes de las cámaras quedan **solo en memoria** unos segundos: nunca se escriben en disco ni entran en las
  copias de seguridad.

- Una orden que el puente no toma en `TISMA_REMOTE_COMMAND_MINUTES` (10) minutos falla y su archivo se borra: una
  impresión nunca arranca mucho después de pedirla. Con el puente apagado no se aceptan órdenes.
- El archivo a imprimir se borra cuando el puente cuenta cómo terminó. Solo G-code (`.gcode`, `.bgcode`); el puente lo
  empaqueta para las Bambu Lab.
- Estas llamadas tienen su propio límite por cuenta, `TISMA_REMOTE_REQUESTS_PER_MINUTE` (600), aparte del plan.
- Cada cuenta ve y controla solo sus puentes (hasta 10).
- **Plan**: cada plan dice cuántas impresoras de la cuenta se usan a distancia (`remote_printers`) y cuánto cuesta por
  mes cada una más (`extra_printer_cents`, 0 = no vende más). Las de más se eligen al pagar en la página de la cuenta
  y valen mientras ese pago está al día; el pago siguiente decide cuántas quedan. Sin el plan pago no se usa ninguna
  (un plan gratis da las suyas). Las que pasan del plan se listan con `locked: true` y sus órdenes y su cámara
  responden 402. Cuentan las primeras impresoras de los puentes más viejos. Por ejemplo, el plan pedido de 2 USD con
  3 impresoras y 1 USD cada una más, y el de 10 USD que las incluye:

  ```
  python -m tisma_cloud.admin plan-set remoto --hours 0 --concurrent 1 --remote-printers 3 --extra-printer-cents 100
  python -m tisma_cloud.admin plan-set completo --hours 20 --concurrent 2 --remote-printers 3 --extra-printer-cents 100
  ```

  (el precio y si se muestra en la página van por `PUT /v1/site/plans/{nombre}` con `price_cents` 200 y 1000).
  Las suscripciones de Google Play no venden impresoras de más todavía.

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
- Un segundo maestro de respaldo que tome el lugar del primero si se cae (hoy: el maestro se reinicia solo y la base
  de datos tiene copias diarias, «Copias de seguridad»; volver a levantarlo en otra máquina es a mano).
