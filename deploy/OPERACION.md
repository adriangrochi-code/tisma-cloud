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
tisma-cloud autostart on           # que arranque solo con la máquina
tisma-cloud firewall on            # solo el maestro, en Windows: el puerto para los nodos de la red local
tisma-cloud check                  # el Diagnóstico; con --fix arregla lo que se puede
```

- El maestro también calcula (su propio nodo, «maestro») salvo `tisma-cloud config master_computes off`.
- Un nodo nuevo pide unirse solo: aceptalo en la ventana del maestro (Nodos) o con
  `tisma-cloud admin node-requests` y `tisma-cloud admin node-accept <id>` (el id que muestra la lista). Qué hace cada uno:
  `tisma-cloud admin node-set <nombre> --priorities slice,slice_large,fea` (README, «Instalar un nodo»).
- La biblioteca entre usuarios, en el maestro (README, «Biblioteca entre usuarios»): certificar su clave una vez
  (`tisma-cloud admin library-certify --key E:\clave.pem`, la clave del proyecto en el pendrive), y si se quiere el intermediario y el sembrador:
  `tisma-cloud config relay on`, `tisma-cloud config seed on`, sus ajustes en `.env` y `tisma-cloud restart`. En el
  router, 4001 y 4002 (TCP y UDP).
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

El panel muestra lo mismo arriba, en «Últimas 24 horas». Durante la beta, mirar cada día que «Fallan» quede bajo
(el error más común dice por qué) y que la espera no crezca.

Los avisos llegan por correo a `TISMA_ALERT_EMAILS` (o a los administradores) y se ven en rojo arriba del panel
(README, «Avisos de problemas»): un nodo que no responde hace `TISMA_ALERT_NODE_MINUTES` (5), un trabajo que espera más
de `TISMA_ALERT_QUEUE_MINUTES` (30), menos de `TISMA_ALERT_DISK_FREE_GB` (5) libres, una copia de seguridad que falló y
ClamAV sin responder. Cuando se resuelve, llega otro correo.

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

En otra máquina: el paquete, `.env`, `storage\data.key` en su lugar y después lo de arriba. El CI lo prueba en cada
cambio (`deploy/restore_drill.py`).

## Actualizar

Primero el maestro, después los nodos (un nodo viejo sigue trabajando con un maestro nuevo):

```
tisma-cloud stop
tisma-cloud backup                 # solo en el maestro
(poner el paquete nuevo encima del viejo, en la misma carpeta)
tisma-cloud start
tisma-cloud check
```

La configuración, la base y `storage\` no se tocan; el maestro pone la base al día solo al arrancar. El CI lo prueba
desde la versión del 24-10-2026 (`deploy/upgrade_drill.py`). Un `node.toml` de antes del 11-11-2026, con
`tisma_slicer` y `tisma_fea`, sigue sirviendo: esos ajustes no se usan, y el servicio encuentra solo
`tisma-engine\` y `tisma-profiles\` del paquete nuevo.

## Apagar

`tisma-cloud stop` en cada máquina. Para que no vuelva a arrancar con la máquina: `tisma-cloud autostart off`. Los
trabajos que estaban corriendo vuelven a la cola y los toma un nodo cuando haya uno.

## Si algo falla

| Qué pasa | Qué mirar |
|---|---|
| El servicio no arranca | `tisma-cloud logs 100`; `tisma-cloud check` |
| Los nodos no llegan al maestro | `tisma-cloud firewall on` en el maestro; `master_url` del nodo (`tisma-cloud config`) |
| Un nodo no toma trabajos | Diagnóstico del nodo: «Programa tisma-engine»; menos de 2 GB libres lo pausan |
| Un proyecto de otro laminador sale con otros ajustes | Diagnóstico del nodo: «Tablas de traducción» |
| Nadie puede confirmar su cuenta | Diagnóstico del maestro: «Correo de los códigos» (`TISMA_SMTP_*` en `.env`) |
| La biblioteca no responde (503) | Diagnóstico: «Biblioteca entre usuarios» (la clave sin certificar) |
| El sembrador no revisa modelos | Diagnóstico: «Sembrador de la biblioteca» (ClamAV, `TISMA_SEED_TOKEN`) |

Los paquetes corren solo en las máquinas propias del dueño: no se le dan a nadie.
