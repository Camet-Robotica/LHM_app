# LHM Telemetry Agent

## Descripción

Agente de escritorio para Windows que captura métricas de hardware en tiempo real usando `LibreHardwareMonitor` y las almacena en una base de datos PostgreSQL remota. Incluye una interfaz gráfica (bandeja del sistema), auto-actualización vía Git y logging centralizado.

---

## Prerrequisitos

- Python 3.12+
- PostgreSQL
- .NET Framework (requerido por `LibreHardwareMonitorLib.dll`)
- Git (requerido por el auto-updater)

---

## Instalación

1. **Clonar el repositorio:**
   ```bash
   git clone <URL-del-repositorio>
   cd <nombre-del-directorio>
   ```

2. **Configurar las variables de entorno:**
   - Copie `example-env.md` y cree un archivo `.env` en la raíz del proyecto:
     ```
     DB_HOST=<host_del_servidor>
     DB_NAME=<nombre_base_de_datos>
     DB_USER=<usuario>
     DB_PASS=<contraseña>
     DB_PORT=5432
     CLIENT_TABLE_NAME=<nombre_tabla_cliente>
     UPDATE_TIME=10
     ```
   - Opcionales (con su valor por defecto):
     ```
     # Tipos de sensor de LHM que se dan de alta solos en la tabla Sensor si no están
     # (por ejemplo "CCD1 (Tdie)" de AMD o las temperaturas de las memorias DDR5).
     AUTO_SENSOR_TYPES=Temperature,Voltage
     # Segundos entre reintentos de conexión a la base (al arrancar y si se corta).
     DB_RETRY_SECONDS=30
     # Copia local en CSV (una fila por lectura, una columna por sensor), haya base o no.
     # "no" la apaga. Ruta relativa a la carpeta del programa.
     LOCAL_DIR=datos_locales
     LOCAL_SENSOR_TYPES=Temperature,Voltage,Power,Fan,Load
     LOCAL_RETENTION_DAYS=14
     ```

3. **Ejecutar el instalador como administrador:**
   ```
   install.bat
   ```
   Este script configura el entorno virtual, instala dependencias y registra la tarea programada de Windows para inicio automático.

---

## Uso

### Configuración inicial de la base de datos

```bash
python db_setup.py
python init_master_tables.py
```

### Modo standalone (sin GUI)

Ejecuta la captura directamente en consola. Ideal para entornos de servidor o depuración:

```bash
python capture.py
```

### Modo GUI (aplicación de escritorio)

Lanza la aplicación con interfaz gráfica en la bandeja del sistema:

```bash
python app/main_app.py
```

### Auto-actualización (bootstrapper)

El `Updater.exe` / `updater/main_updater.py` verifica actualizaciones en Git antes de lanzar la aplicación principal:

```bash
python updater/main_updater.py
```

### Tarea Programada de Windows

Para registrar la tarea de inicio automático:

```powershell
.\service_setup.ps1
```

---

## Arquitectura

```
LHM_app/
├── capture.py               # Núcleo de captura (standalone, modo servicio)
├── app/
│   ├── main_app.py          # Punto de entrada GUI (PySide6)
│   ├── core/
│   │   ├── capture_worker.py  # QThread que encapsula TelemetryLogger
│   │   └── update_worker.py   # QThread para git fetch/pull
│   └── gui/
│       └── dashboard.py     # Ventana principal / System Tray
├── updater/
│   └── main_updater.py      # Bootstrapper de auto-actualización
└── service_setup.ps1        # Script de configuración de tarea programada
```

**Flujo de datos:**
1. `CaptureWorker` importa `TelemetryLogger` desde `capture.py`.
2. `TelemetryLogger` inicializa LibreHardwareMonitor y la conexión PostgreSQL.
3. El bucle de captura recopila sensores, los resuelve contra la caché y hace `INSERT` masivo con `execute_values`.
4. Ante fallos de conexión se activa reconexión automática con backoff de 60 s.

---

## Sistema de Logs

La aplicación genera **tres archivos de log** independientes:

| Archivo | Módulo responsable | Contenido |
|---|---|---|
| `telemetry_capture.log` | `capture.py` / `capture_worker.py` | Ciclo de captura, inserciones en DB, reconexiones, sensores dinámicos registrados. Logger: `HardwareTelemetry`. Tamaño máx.: 5 MB × 3 backups. |
| `app.log` | `app/main_app.py` | Eventos de la GUI: inicio/cierre de la app, carga del stylesheet, mensajes de `Dashboard`, `CaptureWorker` y `UpdateWorker`. Logger raíz configurado con `basicConfig`. Tamaño máx.: 5 MB × 2 backups. |
| `updater.log` | `updater/main_updater.py` | Proceso de auto-actualización: git fetch, comparación de commits, git reset, lanzamiento de la app principal. Logger: `Updater`. |

> **Formato común:** `[YYYY-MM-DD HH:MM:SS] - NIVEL - Mensaje`

---

## Si la PC se corta

- Si al arrancar la base no responde (por ejemplo, la red todavía no levantó), la captura
  sigue igual: guarda la copia local y reintenta la conexión cada `DB_RETRY_SECONDS`.
- Cada fila de la copia local (`datos_locales\telemetria_<fecha>_<hora>.csv`) se fuerza a
  disco, así la última lectura antes de un corte de luz o de un reset queda guardada.
- Al arrancar, `reinicios.csv` anota cómo terminó la sesión anterior de Windows (pantalla
  azul con su código, reset en seco o reinicio pedido), la hora del último dato guardado y
  la temperatura más alta en esa última lectura. Lee el registro de eventos con `wevtutil`.

## Notas Adicionales

- **Versión de LibreHardwareMonitor**: con la 0.9.5 que trae este repo, en una PC con Ryzen 7
  9700X y placa Gigabyte B650I AX no se lee la temperatura ni el consumo del CPU, ni el chip de la
  placa (voltajes y ventiladores): LHM no da valor y la app guardaba 0. La versión anterior de la
  captura, con la 0.9.6 y el driver PawnIO 2.1.0, sí los leía en esa misma PC. Conviene pasar
  `LibreHardwareMonitorLib.dll` (y sus dependencias) a la 0.9.6 o más nueva y recompilar.
- Un sensor sin valor ya no se inserta como 0: simplemente no se inserta en ese ciclo.
- **Ejecutable compilado**: el `Updater.exe` actualiza el código con git, pero si existe
  `LHM_Capture.exe` lanza ese ejecutable. Para que un cambio de código llegue a una PC hay
  que recompilar (`python build.py`) o borrar `LHM_Capture.exe` para que corra el código
  con el `venv`.

- **`LibreHardwareMonitor`**: Biblioteca de monitoreo de hardware de código abierto para Windows. Requiere permisos de administrador.
- **Sensores dinámicos**: Los sensores con patrón variable (ej. `CPU Core #N`, `GPU Fan #N`) se auto-registran en la base de datos la primera vez que se detectan.
- **TCP Keepalives**: La conexión a PostgreSQL usa keepalives (`keepalives_idle=15, keepalives_interval=10, keepalives_count=5`) para detectar desconexiones silenciosas en redes WAN.
- **`HidSharp`**: Biblioteca para comunicación con dispositivos HID.
- **`OxyPlot`**: Biblioteca de trazado para visualización de datos.
- **`Microsoft.Win32.TaskScheduler`**: Biblioteca para interactuar con el Programador de Tareas de Windows.
