# Operar Tisma Cloud: la guía corta del dueño

Lo de todos los días, en una página. La primera instalación, paso por paso, está en `deploy/BETA.md`; los detalles, en
el `README.md` (cada sección se nombra abajo). Los comandos son los del programa `tisma-cloud` (`tisma-cloud.exe` en
Windows), corridos en la carpeta del paquete; todo se puede hacer también desde la ventana (doble clic en el programa).

## Qué hay en cada máquina

| Máquina | Carpeta (ejemplo) | Lo que tiene |
|---|---|---|
| Maestro | `C:\TismaCloud\` | `tisma-cloud.exe`, `tisma-engine\`, `tisma-profiles\`, `tisma-relay\`, `.env`, `tisma-cloud.toml`, `tisma.db`, `storage\`, `backups\`, `logs\` |
| Nodo | `C:\TismaCloud\` | `tisma-cloud.exe`, `tisma-engine\`, `tisma-profiles\`, `node.toml`, `logs\` (y `tisma-scan\` si escanea) |

Todas salen del mismo paquete: el artefacto `tisma-cloud-windows` (o `-linux`) del último CI verde de `main`.

## Levantar

```
tisma-cloud role master            # una vez; en un nodo: role node
tisma-cloud start                  # arranca el servicio en segundo plano
tisma-cloud status                 # en marcha o detenido, rol y puerto
tisma-cloud autostart on           # que arranque solo con la máquina (el primer start ya lo activa)
tisma-cloud firewall on            # solo el maestro, en Windows: el puerto para los nodos de la red local
tisma-cloud check                  # el Diagnóstico; con --fix arregla lo que se puede
```

La primera vez, en el maestro, antes de dar la dirección a nadie (el Diagnóstico marca lo que falta):

```
tisma-cloud config TISMA_SMTP_HOST smtp.tudominio.com   # el correo de los códigos y de los avisos
tisma-cloud config TISMA_SMTP_USER codigos@tudominio.com
tisma-cloud config TISMA_SMTP_PASSWORD ...
tisma-cloud config TISMA_SMTP_FROM "Tisma Cloud <codigos@tudominio.com>"
tisma-cloud config TISMA_ALERT_EMAILS tu@correo.com      # a quién llegan los avisos (si no, a los administradores)
tisma-cloud admin plan-set beta --hours 5 --concurrent 2 --classes all --public
tisma-cloud admin user-add tu@correo.com --plan beta --admin --password
tisma-cloud restart
```

El puerto del correo es 587 con STARTTLS; otro: `TISMA_SMTP_PORT` y `TISMA_SMTP_SECURITY` (`ssl` para el 465).

- El maestro también calcula (su propio nodo, «maestro») salvo `tisma-cloud config master_computes off`.
- Un nodo nuevo pide unirse solo: aceptalo en la ventana del maestro (Nodos) o con
  `tisma-cloud admin node-requests` y `tisma-cloud admin node-accept <id>` (el id que muestra la lista). Qué hace cada uno:
  `tisma-cloud admin node-set <nombre> --priorities slice,slice_large,fea` (README, «Instalar un nodo»).
- La biblioteca entre usuarios, en el maestro (README, «Biblioteca entre usuarios»): certificar su clave una vez
  (`tisma-cloud admin library-certify --key E:\clave.pem`, la clave del proyecto en el pendrive). Si se quiere el
  intermediario y el sembrador:

  ```
  tisma-cloud admin user-add sembrador@tudominio.com --plan beta --unlimited   # muestra su token una sola vez
  tisma-cloud config TISMA_SEED_TOKEN tu_...                                  # ese token
  tisma-cloud config TISMA_RELAY_ANNOUNCE /ip4/TU-IP-PÚBLICA/tcp/4001
  tisma-cloud config TISMA_SEED_ANNOUNCE /ip4/TU-IP-PÚBLICA/tcp/4002
  tisma-cloud config relay on
  tisma-cloud config seed on
  tisma-cloud restart
  tisma-cloud logs 30                # el intermediario escribe su dirección para TISMA_LIBRARY_RELAYS
  tisma-cloud config TISMA_LIBRARY_RELAYS /ip4/TU-IP-PÚBLICA/tcp/4001/p2p/...   # la de la línea del registro
  ```

  Mientras las apps no tengan red de pares, los modelos se bajan y se suben por HTTPS desde el sembrador
  (`p2p_library.md` 8 octies y 8 nonies): en el túnel de Cloudflare, la regla de `/v1/seed/` hacia
  `http://localhost:18100` va **antes** de la que manda todo al maestro (ya está en
  `deploy/cloudflared-config.example.yml`; un `config.yml` de antes del 16-11-2026 no la tiene, ver
  `deploy/cloudflare-tunnel-windows.md`). Sin esa regla, desde afuera no se baja ni se sube ningún modelo. El sembrador
  guarda todos los modelos publicados mientras entren en `TISMA_SEED_GB` (50); si no entran, lo dice en el registro
  («does not fit in TISMA_SEED_GB») y hay que darle más lugar. Cada cuenta sube hasta
  `TISMA_SEED_UPLOAD_MB_PER_HOUR` (2000) por hora.

  El sembrador necesita ClamAV instalado aparte (en Windows, el instalador de clamav.net y `freshclam` para las
  firmas; `TISMA_CLAMAV` con la ruta de `clamdscan` o `clamscan` si no está en el PATH). En el router, 4001 y 4002
  (TCP y UDP).
- Desde afuera: `python deploy/smoke.py --url https://tismaservidores.net --token tu_...` (README, «Instalar el
  maestro»).

## Mirar que todo ande

```
tisma-cloud status
tisma-cloud logs 100               # las últimas líneas del registro (logs\tisma-cloud.log)
tisma-cloud check
tisma-cloud admin queue            # trabajos en espera y corriendo, por clase
tisma-cloud admin metrics          # el último día: bien y fallidos, espera, errores más comunes, cuentas, nodos
tisma-cloud admin metrics --days 7 # la semana
tisma-cloud admin node-list        # los nodos y si están conectados
tisma-cloud panel                  # el panel de control (/admin) en una ventana
```

El panel muestra lo mismo arriba, en «Últimas 24 horas». Lo que ve la gente está en
`https://tismaservidores.net/estado` (funcionando, con demoras o caído; lo mismo leen la app y el programa en
`/v1/status`). Durante la beta, mirar cada día que «Fallan» quede bajo
(el error más común dice por qué) y que la espera no crezca.

Los avisos llegan por correo a `TISMA_ALERT_EMAILS` (o a los administradores) y se ven en rojo arriba del panel
(README, «Avisos de problemas»): un nodo que no responde hace `TISMA_ALERT_NODE_MINUTES` (5), un trabajo que espera más
de `TISMA_ALERT_QUEUE_MINUTES` (30), menos de `TISMA_ALERT_DISK_FREE_GB` (5) libres, una copia de seguridad que falló y
ClamAV sin responder, y el índice de la biblioteca cerca de lo que aceptan las apps (80 %). Cuando se resuelve,
llega otro correo.

## Respaldar

- El maestro copia la base solo, una vez por día (README, «Copias de seguridad»), en `backups\`. Una copia ahora:
  `tisma-cloud backup`.
- Fuera de la máquina, cada tanto: la carpeta `backups\`.
- Una sola vez, en el pendrive cifrado (BETA.md, paso 7): `storage\data.key` (sin ella las copias no se leen),
  `storage\library.key`, `.env` y los archivos del túnel de Cloudflare.

## Restaurar

```
tisma-cloud stop
tisma-cloud restore backups\daily\tisma-AAAAMMDD-HHMMSS.db   # la base de antes queda como tisma.db.before-restore
tisma-cloud start
```

En otra máquina: el paquete, `.env`, `storage\data.key` en su lugar y después lo de arriba. Una copia de antes de
cambiar la clave (`rotate-key`, abajo) se lee con la clave de entonces: `storage\data.key.old-...` en lugar de
`data.key`. El CI lo prueba en cada
cambio (`deploy/restore_drill.py`).

## Cambiar la clave de los datos personales

Solo si se pudo haber filtrado (README, «Datos personales cifrados en la base»):

```
tisma-cloud stop
tisma-cloud rotate-key             # hace una copia antes; la clave vieja queda como data.key.old-<fecha>
tisma-cloud start
```

Después: la clave nueva al pendrive, y la vieja junto a las copias de antes (sin ella no se leen).

## Actualizar

Primero el maestro, después los nodos (un nodo viejo sigue trabajando con un maestro nuevo):

```
tisma-cloud stop
tisma-cloud backup                 # solo en el maestro
(poner el paquete nuevo encima del viejo, en la misma carpeta)
tisma-cloud start
tisma-cloud check
```

`stop` espera a que terminen todos los procesos del servicio (unos segundos), así que el paquete se puede copiar
apenas dice «Detenido». La configuración, la base y `storage\` no se tocan; el maestro pone la base al día solo al
arrancar. El CI lo prueba desde la versión del 24-10-2026 (`deploy/upgrade_drill.py`). Un `node.toml` de antes del
11-11-2026, con `tisma_slicer` y `tisma_fea`, sigue sirviendo: esos ajustes no se usan, y el servicio encuentra solo
`tisma-engine\` y `tisma-profiles\` del paquete nuevo.

## Apagar

`tisma-cloud stop` en cada máquina. Para que no vuelva a arrancar con la máquina: `tisma-cloud autostart off`. Los
trabajos que estaban corriendo vuelven a la cola y los toma un nodo cuando haya uno.

### El registro

`logs\tisma-cloud.log`, en la carpeta del servicio de cada máquina, tiene una línea por cada cosa que pasa, con la hora
en UTC y la parte que la escribió: `[servicio]` (cada arranque, reinicio y parada, y quién lo pidió: el botón de la
ventana, el comando, la tarea programada o systemd al iniciar la máquina), `[maestro]` (cada nodo que se conecta o
vuelve, cada trabajo recibido, dado a un nodo, entregado o fallido), `[nodo]` (cada conexión al maestro, cada reintento
con su espera y su error exacto, por ejemplo «tismaservidores.net no existe en el DNS», y cada trabajo),
`[nodo del maestro]`, `[intermediario]` y `[sembrador]`. Por ejemplo:

```
2026-11-23T19:10:47Z [servicio] inicio pedido por el botón «Reiniciar» de la ventana (reinicio)
2026-11-23T19:10:49Z [maestro] INFO Node n1 connected again (last heard 11 s before)
2026-11-23T19:11:12Z [maestro] INFO Job fd595681-... done: result delivered by node n2
```

Al llegar a 10 MB pasa a `tisma-cloud.log.1` (se guardan 5: 60 MB como mucho). No tiene claves, contraseñas ni tokens
(se reemplazan por `***`). Se abre con «Abrir registro» en la ventana (Registro y consola) y en el panel (abajo de todo),
o con `tisma-cloud logs 200`. Si el servicio mismo se cae, Python deja el detalle en `logs\tisma-cloud-fallos.log`.

## Si algo falla

| Qué pasa | Qué mirar |
|---|---|
| El servicio no arranca | Lo dice al iniciar («No arrancó: …»); `tisma-cloud logs 100`; `tisma-cloud check` |
| El maestro dice «No responde: el puerto N está ocupado» | Otro programa (u otro Tisma Cloud que quedó) usa el puerto: cerralo o `tisma-cloud config port N` |
| Los nodos no llegan al maestro | `tisma-cloud firewall on` en el maestro; `master_url` del nodo (`tisma-cloud config`) |
| Un nodo no toma trabajos | Diagnóstico del nodo: «Programa tisma-engine»; menos de 2 GB libres lo pausan |
| Un proyecto de otro laminador sale con otros ajustes | Diagnóstico del nodo: «Tablas de traducción» |
| Nadie puede confirmar su cuenta | Diagnóstico del maestro: «Correo de los códigos» (`TISMA_SMTP_*` en `.env`) |
| La biblioteca no responde (503) | Diagnóstico: «Biblioteca entre usuarios» (la clave sin certificar) |
| El sembrador no revisa modelos | Diagnóstico: «Sembrador de la biblioteca» (ClamAV, `TISMA_SEED_TOKEN`) |
| Las apps no bajan ni suben modelos | Diagnóstico: «Biblioteca por HTTPS» (el sembrador en marcha, y la regla de `/v1/seed/` en el túnel) |

### El nodo no vuelve

Después de apagar la PC del nodo de golpe, un corte de luz o de red, o un reinicio del maestro, el nodo vuelve solo:
arranca con la máquina (el primer `start` de cada carpeta activa «Inicio con la máquina»; `tisma-cloud autostart off`
lo apaga; es uno por máquina: si otra carpeta de la misma PC ya lo tiene, esta no se lo quita y lo dice, y `tisma-cloud autostart on` en esta se lo pasa) y se reconecta apenas llega al maestro, probando de nuevo a los 2, 4, 8… hasta 60 s. El trabajo que
calculaba vuelve a la cola y lo termina un nodo; si fue el maestro el que se reinició, el nodo lo sigue y lo entrega al
volver. En el maestro, Nodos dice «Reconectado hace N s» (y `tisma-cloud admin node-list`, «reconnected»). El CI lo
prueba en cada cambio (`deploy/reconnect_drill.py`). Si no vuelve:

1. **En la PC del nodo**, `tisma-cloud status` dice qué vio el nodo la última vez, y `tisma-cloud check` (o la ventana)
   lo prueba en el momento:

   | Dice | Qué hacer |
   |---|---|
   | «Detenido» | `tisma-cloud start`; para que arranque solo, `tisma-cloud autostart on` |
   | «Nada atiende en IP:puerto» | el maestro está detenido, o el puerto no es ese: en el maestro, `tisma-cloud status` |
   | «El maestro no contesta en IP:puerto» | el puerto está bloqueado: en el maestro, `tisma-cloud firewall on`; o esa no es la dirección del maestro en esta red (`master_url` del nodo) |
   | «No se encuentra …» / «Esta máquina no llega a …» | la dirección del maestro está mal escrita, o esta PC no tiene red |
   | «El maestro no reconoce el token» | quitaron el nodo en el maestro: el nodo pide unirse solo; aceptalo en el maestro (Nodos) |
   | «deshabilitado» | en el maestro, Nodos: «Habilitar / deshabilitar» |
   | «responde otro programa» | la dirección o el puerto son de otro programa, no del maestro |
   | «Versión: … y este nodo la …» | sigue andando; actualizá primero el maestro y después el nodo |

2. **En el maestro**, Nodos muestra al lado de cada nodo qué pasa: «Nunca se conectó», «Sin señal hace N min» (la PC
   está apagada, sin red, o su servicio detenido), «Dos programas responden con su token» o una versión distinta.

3. Una versión de antes del 16-11-2026 podía quedarse sin arrancar después de un corte: decía «En marcha» pero el nodo
   no corría, porque el archivo `tisma-cloud.pid` que quedó nombraba a otro programa. Si pasa con esa versión: borrar
   `tisma-cloud.pid` de la carpeta del nodo y `tisma-cloud start`. Desde esa fecha se reconoce solo.

Los paquetes corren solo en las máquinas propias del dueño: no se le dan a nadie.
