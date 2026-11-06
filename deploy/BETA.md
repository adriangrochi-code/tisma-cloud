# Puesta en marcha de la beta (el i5 con Windows)

Lista en orden para dejar andando el maestro en el i5 con el túnel de Cloudflare, los nodos y los escaneos. Cada paso
apunta a la parte del README o de la guía que lo explica en detalle. Lo marcado **(probado)** lo prueba el CI; lo demás
se prueba recién en la máquina real.

## 0. Antes de empezar

- [ ] El dominio (`tismaservidores.net`) en Cloudflare: `cloudflare-tunnel-windows.md`, paso 1.
- [ ] Una cuenta de correo del dominio para mandar los códigos (servidor SMTP, usuario y contraseña). Sin ella nadie
      puede confirmar su cuenta.
- [ ] Un segundo disco o una carpeta que no esté en el disco del sistema, para las copias de la base de datos.
- [ ] Un pendrive cifrado (u otro lugar fuera de la máquina) para lo secreto: ver el paso 7.

## 1. El paquete

- [ ] Bajar el artefacto `tisma-cloud-windows` del último CI verde de `main` (pestaña *Actions* de `tisma-cloud`) y
      descomprimirlo entero en `C:\TismaCloud\`. Queda `tisma-cloud.exe` con la carpeta `tisma-slicer\` al lado.
      **(probado: el CI arranca el paquete y lamina y analiza un proyecto real con él)**
- [ ] Si el i5 también va a hacer escaneos 3D: el artefacto `TismaScan-windows` del CI de `openscanner`, descomprimido
      en la misma carpeta, de modo que quede `C:\TismaCloud\tisma-scan\`. **(probado en Windows sin NVIDIA: un
      escaneo sintético completo)**. Los paquetes corren solo en las máquinas propias; no se le dan a nadie
      (`THIRD_PARTY_LICENSES.md`).

## 2. El maestro

Clic derecho en `tisma-cloud.exe` › *Ejecutar como administrador* › **Maestro** (README, «Windows Server»).

- [ ] Configuración, en la ventana o con `tisma-cloud config CLAVE valor`:
  - `TISMA_PUBLIC_URL https://tismaservidores.net`
  - `TISMA_CLIENT_IP_HEADER cf-connecting-ip` (**obligatorio con el túnel**: ver la guía del túnel, paso 6)
  - `TISMA_SMTP_HOST`, `TISMA_SMTP_PORT`, `TISMA_SMTP_SECURITY`, `TISMA_SMTP_USER`, `TISMA_SMTP_PASSWORD`,
    `TISMA_SMTP_FROM` (README, «Cuentas: correo confirmado»)
  - `TISMA_BACKUP_DIR D:\TismaBackups` (otro disco; README, «Copias de seguridad»)
  - `TISMA_ALERT_EMAILS tu@correo` (README, «Avisos de problemas»)
  - `TISMA_INTERNAL_EMAILS @tisma.com.uy` (las cuentas del equipo, sin cobro)
  - La base de datos: `sqlite:///./tisma.db` alcanza para la beta (probado con 100 usuarios a la vez, «Capacidad»);
    PostgreSQL cuando haga falta más.
- [ ] **Iniciar**, **Iniciar con la máquina** y **Abrir el puerto en el firewall** (solo para los nodos de la red
      local; en el router no se abre nada). **(probado en Windows Server 2022: la tarea como SYSTEM, el firewall, el
      supervisor que revive el maestro)**
- [ ] **Diagnóstico**: todo en ✔ o «!». Lo que esté en ✖ se arregla con «Arreglar».

## 3. El túnel de Cloudflare

- [ ] `cloudflare-tunnel-windows.md`, pasos 2 a 7 (instalar `cloudflared`, crear el túnel, apuntar el dominio, el
      servicio de Windows). **Sin probar** con un `cloudflared` real.

## 4. Administrador y planes

- [ ] **Panel de control**: la primera vez pide crear el administrador (correo y contraseña).
- [ ] Los planes, públicos para que Tisma y la app los vean (README, «Cuentas, planes y pagos»). Por ejemplo:

```
tisma-cloud admin plan-set basico --hours 10 --concurrent 2 --rpm 120 --priority 1 --classes slice,orient --public
tisma-cloud admin plan-set pro --hours 40 --concurrent 4 --rpm 240 --priority 2 --classes all --public
tisma-cloud admin plan-set escaneo --hours 10 --concurrent 1 --classes scan --public
```

- [ ] Las cuentas de la beta: que se registren solas, o `tisma-cloud admin user-add correo --plan basico`. Sin medio
      de pago todavía, el período pago se da a mano (`admin user-set correo --paid-until AAAA-MM-DD`) o con una cuenta
      interna.

## 5. Los nodos

- [ ] En cada PC de cómputo: el mismo paquete, **Nodo**, la dirección del maestro en la red local
      (`http://<IP del i5>:8000`), el token vacío e **Iniciar**. En el maestro, **Nodos** › **Aceptar** (README,
      «Instalar un nodo»).
- [ ] Qué hace cada uno: `tisma-cloud admin node-set <nodo> --priorities fea,slice,...`.
- [ ] El nodo que escanea (el del maestro se llama `maestro`): `--priorities scan,...` y, en su Diagnóstico,
      «Escaneos 3D: Listo» (README, «Nodo de escaneo en Windows»). Un nodo que no está listo no toma escaneos.

## 6. Probar desde afuera

Desde otra conexión (los datos del celular, compartidos a una notebook con Python):

```
pip install httpx
python deploy/smoke.py --url https://tismaservidores.net
python deploy/smoke.py --url https://tismaservidores.net --token tu_... --project tests/data/bar_project.3mf
```

- [ ] Todo en `OK`. Revisa que la dirección responda con HTTPS válido, que se vean los planes, que la administración
      **no** se vea desde internet y, con un token (`tisma-cloud admin user-token correo`) y un proyecto, que un nodo
      lamine un trabajo y devuelva el G-code.
- [ ] Un escaneo: con el token de una cuenta del plan `escaneo`,
      `python deploy/scan_e2e.py --url https://tismaservidores.net --token tu_... --scan escaneo.zip` (el zip de la
      app). **Probado en el CI con un escaneo sintético; falta uno real del teléfono.**
- [ ] Tisma (el programa) y la app del teléfono: entrar, ver los planes, mandar un trabajo.

## 7. Lo secreto, fuera de la máquina

Sin esto, un disco roto deja los datos sin poder leerse:

- [ ] `C:\TismaCloud\storage\data.key` (la llave de los datos personales de la base; README, «Seguridad de las
      cuentas»). Va **aparte** de las copias de la base de datos.
- [ ] `C:\TismaCloud\.env` (la configuración, con la contraseña del correo).
- [ ] Los archivos del túnel (`cloudflare-tunnel-windows.md`, «Respaldo de lo secreto»).
- [ ] Una copia de la última copia de la base (`TISMA_BACKUP_DIR`) en otro lugar, cada tanto.
- [ ] El simulacro de restaurar, una vez, en otra carpeta: `python deploy/restore_drill.py --exe
      C:\TismaCloud\tisma-cloud.exe --folder D:\simulacro` (README, «Simulacro de restaurar»). Sin `data.key` el
      maestro no arranca: por eso va aparte y fuera de la máquina.

## 8. Qué mirar durante la beta

El maestro se vigila solo y avisa por correo (README, «Avisos de problemas»). Con `TISMA_ALERT_EMAILS` puesto, llegan
estos avisos, uno cuando empieza el problema, un recordatorio cada `TISMA_ALERT_REPEAT_H` horas y otro cuando se
resuelve:

| Aviso | Cuándo (ajuste, valor por defecto) | Qué hacer |
|---|---|---|
| Nodo caído | un nodo no responde hace `TISMA_ALERT_NODE_MINUTES` (5 min) | mirar esa PC: encendida, con red, Diagnóstico del nodo; sus trabajos vuelven solos a la cola |
| Cola trabada | un trabajo espera más de `TISMA_ALERT_QUEUE_MINUTES` (30 min) | ningún nodo hace ese tipo de trabajo (prioridades, «Escaneos 3D: Listo»), o están todos ocupados |
| Disco | quedan menos de `TISMA_ALERT_DISK_FREE_GB` (5 GB) en el maestro | los resultados viejos se borran a las `TISMA_RETENTION_H` (72 h); liberar espacio o bajar ese valor |
| Copia de seguridad | falló la copia diaria de la base | el mensaje dice por qué (disco lleno, carpeta que no existe); `tisma-cloud backup` para probar |

- [ ] Probar que llegan: apagar un nodo más de 5 minutos y prenderlo de nuevo (llegan dos correos).
- [ ] El panel (`/admin`, o **Panel de control** en la ventana) muestra los avisos activos en rojo arriba, los nodos
      en línea y qué corre cada uno, la cola por tipo, los trabajos de hoy (terminados y fallidos), las cuentas y las
      horas calculadas del mes. Mirarlo una vez por día durante la beta.
- [ ] Los trabajos fallidos del día: en el panel, con su error. Uno que falla igual dos veces con proyectos distintos
      es un problema del nodo o del programa, no del proyecto: mirar el registro del nodo (`tisma-cloud logs 200`).
- [ ] Los problemas que informa la gente (Tisma y la app) aparecen en el panel, «Problemas informados», agrupados.

## 9. Actualizar durante la beta

- [ ] Para cada versión nueva: `tisma-cloud stop`, `tisma-cloud backup`, el paquete nuevo encima del viejo en la misma
      carpeta, `tisma-cloud start`; primero el maestro, después los nodos (README, «Actualizar Tisma Cloud»). El CI
      prueba la actualización desde la versión del 24-10 con datos, en SQLite y PostgreSQL (`upgrade_drill.py`).
- [ ] Después de actualizar, `smoke.py` desde afuera.

## 10. Antes de invitar a la gente

- [ ] El Diagnóstico del maestro y de cada nodo, sin ✖.
- [ ] `smoke.py` en `OK` desde afuera.
- [ ] Un corte de luz simulado: reiniciar el i5 y ver que el maestro, el túnel y los nodos vuelven solos.
- [ ] El correo de las alertas llega (paso 8).
- [ ] La guía para quienes prueban la beta (`deploy/BETA_TESTERS.md`) con la dirección real, mandada a cada uno.
