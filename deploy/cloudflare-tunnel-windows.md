# Tisma Cloud con dominio propio en Windows, sin IP fija: Cloudflare Tunnel

Para un servidor en casa con IP que cambia (en Uruguay suele cambiar de madrugada) y sin pagar IP fija ni DNS dinámico.

**Cómo funciona:**
- el servidor abre una conexión **saliente** hacia Cloudflare;
- Cloudflare recibe las visitas a `tismaservidores.net` y las manda por esa conexión;
- cuando la IP cambia, el túnel se vuelve a conectar solo, en segundos;
- no se abre ningún puerto en el router, funciona aunque el proveedor use CGNAT y no se ve la IP de la casa;
- HTTPS va incluido, con el certificado de Cloudflare.

**Costo:** el plan gratis de Cloudflare alcanza. Solo se paga el dominio, que ya está comprado.

**Límite del plan gratis:** Cloudflare corta los pedidos de más de 100 MB. Por eso el paso 6 baja a 100 MB el proyecto más
grande que acepta Tisma Cloud (por defecto son 200).

> Esta guía reemplaza a Caddy, el reenvío de puertos y el DNS dinámico (No-IP) en esta máquina. Los nodos de la red
> local siguen hablando con el maestro directo, por `http://<IP del maestro>:8000`.

## 1. Pasar el dominio a Cloudflare

1. Crear una cuenta gratis en <https://dash.cloudflare.com/sign-up>.
2. **Add a site** → escribir `tismaservidores.net` → elegir el plan **Free**.
3. Cloudflare muestra dos *nameservers* (por ejemplo `xxx.ns.cloudflare.com`). En la página del registrador donde se compró
   el dominio, reemplazar los nameservers actuales por esos dos.
4. Esperar a que Cloudflare diga que el sitio está **Active**. Suele tardar minutos, a veces unas horas.
5. Borrar los registros `A` o `CNAME` viejos de `tismaservidores.net` que apunten a la IP de la casa. El paso 4 crea el
   registro correcto.

## 2. Instalar cloudflared en el maestro

En PowerShell **como administrador**:

```powershell
winget install --id Cloudflare.cloudflared
```

Cerrar y volver a abrir PowerShell, y comprobar con `cloudflared --version`.

Sin winget: bajar `cloudflared-windows-amd64.msi` de la página oficial de descargas de Cloudflare
(<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>) e instalarlo.

## 3. Crear el túnel

```powershell
cloudflared tunnel login
```

Abre el navegador: elegir `tismaservidores.net`. Queda guardado `C:\Users\<usuario>\.cloudflared\cert.pem`.

```powershell
cloudflared tunnel create tisma
```

Muestra el **id del túnel** (algo como `6ff42ae2-765d-4adf-8112-31c55c1551ef`) y crea
`C:\Users\<usuario>\.cloudflared\<id>.json`, la credencial del túnel. **Ese archivo es secreto:** quien lo tenga puede
hacerse pasar por el servidor.

## 4. Apuntar el dominio al túnel

```powershell
cloudflared tunnel route dns tisma tismaservidores.net
```

Crea en Cloudflare un `CNAME` de `tismaservidores.net` hacia el túnel. No hace falta tocar nada más en el DNS.

## 5. Configuración y servicio de Windows

El servicio de Windows corre como «Sistema local» y no lee la carpeta `.cloudflared` de tu usuario: su configuración, la
credencial y `cert.pem` van en la carpeta de ese usuario. Son los pasos de la guía oficial de Cloudflare para un túnel
administrado localmente como servicio de Windows
(<https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/windows/>).

```powershell
$dir = "C:\Windows\System32\config\systemprofile\.cloudflared"
New-Item -ItemType Directory -Force $dir
Copy-Item "$env:USERPROFILE\.cloudflared\<id>.json" $dir
Copy-Item "$env:USERPROFILE\.cloudflared\cert.pem" $dir
Copy-Item "<carpeta de Tisma Cloud>\deploy\cloudflared-config.example.yml" "$dir\config.yml"
notepad "$dir\config.yml"
```

En `config.yml`, reemplazar las dos `<TUNNEL-ID>` por el id del túnel. Si el maestro usa otro puerto, cambiar el `8000`.
Comprobar las reglas:

```powershell
cloudflared tunnel --config "$dir\config.yml" ingress validate
cloudflared tunnel --config "$dir\config.yml" ingress rule https://tismaservidores.net/admin      # http_status:404
cloudflared tunnel --config "$dir\config.yml" ingress rule https://tismaservidores.net/v1/health  # http://localhost:8000
```

La configuración ya **cierra** `/admin`, `/v1/admin` y `/v1/node/join`: desde internet responden «no existe». La
administración y el alta de nodos quedan solo para la red local, igual que antes. El maestro también las niega desde
internet por su cuenta (paso 6), así que el túnel es una segunda barrera.

Instalar el servicio y decirle qué configuración usar. Sin el segundo comando el servicio arranca sin `config.yml` y el
túnel no se conecta:

```powershell
cloudflared service install
$exe = (Get-Command cloudflared).Source
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\Cloudflared" ImagePath `
    "`"$exe`" --config=`"$dir\config.yml`" tunnel run"
Restart-Service cloudflared
Get-Service cloudflared          # tiene que decir Running
```

El servicio arranca solo con Windows y se reconecta después de cada cambio de IP o corte de internet. Después de cambiar
`config.yml`: `Restart-Service cloudflared`. El registro queda en `$dir\cloudflared.log` (`logfile` de `config.yml`).

## 6. Ajustes de Tisma Cloud detrás del túnel

En la carpeta de Tisma Cloud, o en la ventana de `tisma-cloud.exe` › Configuración:

```powershell
tisma-cloud config TISMA_PUBLIC_URL https://tismaservidores.net
tisma-cloud config TISMA_CLIENT_IP_HEADER cf-connecting-ip
tisma-cloud config TISMA_MAX_UPLOAD_MB 100
tisma-cloud config TISMA_DDNS_HOST --clear
tisma-cloud restart
```

- **`TISMA_CLIENT_IP_HEADER cf-connecting-ip` es obligatorio con el túnel.** Detrás del túnel todos los pedidos llegan
  desde la misma máquina (`cloudflared`), y la dirección real del usuario viene en el encabezado `CF-Connecting-IP`, que
  pone Cloudflare. Sin este ajuste, Tisma Cloud lee `X-Forwarded-For`. Cloudflare **agrega** su dato a ese encabezado,
  pero no borra lo que mandó el usuario, así que alguien podría escribir ahí una dirección de la red local, pasar por
  local y llegar a la administración. Con el ajuste, eso no pasa.
- El maestro lee ese encabezado solo en conexiones desde la red local (`TISMA_TRUSTED_PROXIES`, por defecto las redes
  privadas y esta máquina): `cloudflared` llega desde `localhost`. Una conexión que llegue de internet directo al puerto
  8000 (un reenvío olvidado en el router) se juzga por su propia dirección, aunque traiga el encabezado.
- `--clear` deja vacío el ajuste (en PowerShell 5, `""` no llega al programa). `TISMA_DDNS_HOST` vacío apaga el DNS dinámico de No-IP, que con el túnel ya no hace falta.
- Si estaba abierto el puerto en el router (reenvío al 80, 443 u 8000), cerrarlo.

## 7. Probar

Desde un celular **con datos móviles**, fuera de la red de la casa:

- `https://tismaservidores.net/v1/health` responde.
- `https://tismaservidores.net/admin` dice que no existe (404).
- Tisma (el programa) › Tisma Cloud: con el servidor `https://tismaservidores.net`, se ven los planes y se puede entrar.

Desde la red local, `http://<IP del maestro>:8000/admin` sigue abriendo la administración.

Si algo falla:

```powershell
Get-Service cloudflared
cloudflared tunnel info tisma               # conexiones activas del túnel
Get-WinEvent -LogName Application -MaxEvents 30 | Where-Object ProviderName -eq cloudflared
Get-Content "C:\Windows\System32\config\systemprofile\.cloudflared\cloudflared.log" -Tail 30
```

## 8. Qué pasa cuando cambia la IP

Nada visible. A la hora del cambio (de madrugada) el túnel se corta unos segundos y se vuelve a conectar solo, con la IP
nueva. El dominio no cambia, porque apunta al túnel y no a la IP. Los trabajos que estaban corriendo en los nodos
siguen: los nodos hablan con el maestro por la red local, no por el túnel.

## Respaldo de lo secreto

Guardar fuera de la máquina, por ejemplo en un pendrive cifrado:
- `C:\Windows\System32\config\systemprofile\.cloudflared\<id>.json`;
- `config.yml`;
- `cert.pem` (en esa misma carpeta y en `C:\Users\<usuario>\.cloudflared\`).

Con eso el túnel se puede rearmar en otra máquina sin crear uno nuevo.
