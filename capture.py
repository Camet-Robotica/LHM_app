import os
import sys
import clr
import csv
import glob
import time
import ctypes
import threading
import subprocess
import xml.etree.ElementTree as ET
from queue import Queue, Empty, Full
import psycopg2
from psycopg2 import extras
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import re
import fnmatch
import logging
from logging.handlers import RotatingFileHandler

# --- CONFIGURACIÓN DEL LOGGER ---
# Determinar el directorio base correcto tanto para script como para PyInstaller
if getattr(sys, 'frozen', False):
    # Ejecutable PyInstaller: guardar el log alongside el .exe, no en _MEIPASS
    _base_log_dir = os.path.dirname(sys.executable)
else:
    _base_log_dir = os.path.dirname(os.path.abspath(__file__))

log_file = os.path.join(_base_log_dir, 'telemetry_capture.log')

# Formato: [Fecha Hora] - NIVEL - Mensaje
log_formatter = logging.Formatter('[%(asctime)s] - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Configurar el logger principal
logger = logging.getLogger('HardwareTelemetry')
logger.setLevel(logging.INFO)

# Handler para escribir en el archivo de log (con rotación: 5MB max, 3 backups)
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

load_dotenv()

# Configuración .NET
os.chdir(os.path.dirname(os.path.abspath(__file__)))
dll_path = os.path.join(os.getcwd(), 'LibreHardwareMonitorLib.dll')
clr.AddReference(dll_path)
from LibreHardwareMonitor.Hardware import Computer

# Mapeo constante de tipos de hardware LHM → tipos de la BD
LHM_TO_DB_HW = {
    "Cpu": "CPU", "GpuNvidia": "GPU", "GpuAti": "GPU",
    "Motherboard": "MOTHERBOARD", "SuperIO": "MOTHERBOARD",
    "Memory": "MEMORIA RAM", "Storage": "ALMACENAMIENTO"
}

# Patrones regex para auto-registro de sensores dinámicos (pre-compilados)
DYNAMIC_REGEX = [
    (re.compile(r'^CPU Core #\d+$', re.IGNORECASE),     'temperature'),
    (re.compile(r'^CPU Core #\d+$', re.IGNORECASE),     'load'),
    (re.compile(r'^CPU Core #\d+$', re.IGNORECASE),     'power'),
    (re.compile(r'^System Fan #\d+.*$', re.IGNORECASE), 'fan'),
    (re.compile(r'^CPU Fan.*$', re.IGNORECASE),          'fan'),
    (re.compile(r'^GPU Fan #\d+.*$', re.IGNORECASE),     'fan'),
]

# Nombres que no se dan de alta solos aunque su tipo esté en AUTO_SENSOR_TYPES:
# valores fijos que LHM informa como temperatura (límites y resolución del sensor
# de las memorias DDR5, temperaturas de aviso y críticas del disco), el VID que
# pide cada núcleo y la "distancia a TjMax" de Intel (repite la temperatura).
# Se cambia con AUTO_SENSOR_EXCLUDE ("" = ninguno).
AUTO_EXCLUDE_DEFAULT = r"Limit|Resolution|Distance to TjMax|^Warning Temperature$|^Critical Temperature$|VID$"

# Candado de la base para dar de alta sensores de a uno (ver _register_sensor).
CANDADO_ALTA_SENSOR = "lhm_app.alta_sensor"

# Errores de red o de base que no deben frenar la captura: se reintenta.
ERRORES_BD = (psycopg2.OperationalError, psycopg2.InterfaceError, OSError, ConnectionError)


def _tipos_desde_entorno(nombre: str, por_defecto: str) -> set[str]:
    """Lista de tipos de sensor de LHM separada por comas (ej. 'Temperature,Voltage')."""
    valor = os.getenv(nombre, por_defecto)
    return {t.strip().upper() for t in valor.split(',') if t.strip()}


def _regex_desde_entorno(nombre: str, por_defecto: str) -> 're.Pattern | None':
    """Expresión regular de una variable de entorno (sin distinguir mayúsculas).
    Vacía devuelve None; si no es válida se usa la de por defecto."""
    valor = os.getenv(nombre, por_defecto).strip()
    if not valor:
        return None
    try:
        return re.compile(valor, re.IGNORECASE)
    except re.error as e:
        logger.warning(f"{nombre} no es una expresión regular válida ({e}): uso '{por_defecto}'.")
        return re.compile(por_defecto, re.IGNORECASE)


# ─── Copia local ────────────────────────────────────────────────────────────

class CopiaLocal:
    """Guarda cada lectura en un CSV por día junto al ejecutable, haya base o no.

    Una fila por lectura y una columna por sensor. Cada fila se fuerza a disco,
    así la última lectura antes de un corte de luz o de un reset queda guardada.
    Se borran solos los archivos de más de LOCAL_RETENTION_DAYS días."""

    PATRON = 'telemetria_*.csv'

    def __init__(self, carpeta: str, dias: int):
        self.carpeta = carpeta
        self.dias = dias
        self.columnas: list[str] = []
        self._archivo = None
        self._fecha = None
        os.makedirs(self.carpeta, exist_ok=True)

    @classmethod
    def desde_entorno(cls, base_dir: str):
        carpeta = os.getenv("LOCAL_DIR", "datos_locales").strip()
        if carpeta.lower() in ("", "no", "0", "off"):
            return None
        if not os.path.isabs(carpeta):
            carpeta = os.path.join(base_dir, carpeta)
        return cls(carpeta, int(os.getenv("LOCAL_RETENTION_DAYS", 14)))

    def _abrir(self, ahora: datetime):
        self.cerrar()
        self._fecha = ahora.date()
        ruta = os.path.join(self.carpeta, f"telemetria_{ahora:%Y-%m-%d_%H%M%S}.csv")
        self._archivo = open(ruta, 'w', newline='', encoding='utf-8')
        csv.writer(self._archivo).writerow(['hora'] + self.columnas)
        self._limpiar_viejos()

    def _limpiar_viejos(self):
        limite = time.time() - self.dias * 86400
        for ruta in glob.glob(os.path.join(self.carpeta, self.PATRON)):
            try:
                if os.path.getmtime(ruta) < limite:
                    os.remove(ruta)
            except OSError:
                pass

    def escribir(self, ahora: datetime, valores: list):
        if self._archivo is None or ahora.date() != self._fecha:
            self._abrir(ahora)
        fila = [ahora.strftime('%Y-%m-%d %H:%M:%S')]
        fila += ['' if v is None else f"{v:.3f}" for v in valores]
        csv.writer(self._archivo).writerow(fila)
        self._archivo.flush()
        os.fsync(self._archivo.fileno())

    def ultima_fila(self):
        """(hora, {columna: valor}) de la última fila del archivo más nuevo, o None."""
        archivos = sorted(glob.glob(os.path.join(self.carpeta, self.PATRON)))
        for ruta in reversed(archivos):
            try:
                with open(ruta, newline='', encoding='utf-8', errors='replace') as fh:
                    filas = list(csv.reader(fh))
            except OSError:
                continue
            if len(filas) < 2:
                continue
            cabecera, ultima = filas[0], filas[-1]
            try:
                hora = datetime.strptime(ultima[0], '%Y-%m-%d %H:%M:%S')
            except (ValueError, IndexError):
                continue
            return hora, dict(zip(cabecera[1:], ultima[1:]))
        return None

    def cerrar(self):
        if self._archivo is not None:
            try:
                self._archivo.close()
            except OSError:
                pass
            self._archivo = None


# ─── Registro de arranques ──────────────────────────────────────────────────

_NS_EVENTOS = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}


def _arranque_windows() -> datetime:
    """Hora local en que arrancó Windows (ahora menos el tiempo encendido)."""
    ms = ctypes.windll.kernel32.GetTickCount64() if hasattr(ctypes, 'windll') else 0
    return datetime.now() - timedelta(milliseconds=ms)


def _eventos_sistema(ids: list[int], desde_ms: int) -> list[tuple[int, datetime, dict]]:
    """Eventos del registro System con esos ids de los últimos `desde_ms` ms.
    Usa wevtutil, que no necesita permisos de administrador."""
    filtro = ' or '.join(f'EventID={i}' for i in ids)
    xpath = f"*[System[({filtro}) and TimeCreated[timediff(@SystemTime) <= {int(desde_ms)}]]]"
    try:
        r = subprocess.run(
            ['wevtutil', 'qe', 'System', f'/q:{xpath}', '/c:20', '/rd:true', '/f:xml'],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"No pude leer el registro de eventos: {e}")
        return []
    if r.returncode != 0 or not r.stdout:
        return []
    texto = r.stdout.decode('utf-8', errors='replace')
    try:
        raiz = ET.fromstring(f"<eventos>{texto}</eventos>")
    except ET.ParseError as e:
        logger.warning(f"No pude interpretar el registro de eventos: {e}")
        return []
    eventos = []
    for ev in raiz.findall('e:Event', _NS_EVENTOS):
        sistema = ev.find('e:System', _NS_EVENTOS)
        try:
            id_evento = int(sistema.find('e:EventID', _NS_EVENTOS).text)
            utc = sistema.find('e:TimeCreated', _NS_EVENTOS).get('SystemTime')
            hora = datetime.fromisoformat(utc)  # Python 3.11+: acepta 'Z' y 7 decimales
        except (AttributeError, TypeError, ValueError):
            continue
        if hora.tzinfo is None:
            hora = hora.replace(tzinfo=timezone.utc)
        hora_local = hora.astimezone().replace(tzinfo=None)
        datos = {d.get('Name'): (d.text or '') for d in ev.findall('e:EventData/e:Data', _NS_EVENTOS)}
        eventos.append((id_evento, hora_local, datos))
    return eventos


def clasificar_arranque(arranque: datetime, eventos: list[tuple[int, datetime, dict]]) -> tuple[str, str, str]:
    """(tipo, código, detalle) del último arranque según los eventos de Windows.

    - Kernel-Power 41 después del arranque: apagado inesperado. Con BugcheckCode
      es una pantalla azul; sin código, un reset en seco (corte de luz, fuente,
      botón de reset o falla de hardware brusca).
    - Si no, un 1074 antes del arranque: reinicio o apagado pedido.
    """
    for id_evento, hora, datos in eventos:
        if id_evento == 41 and hora >= arranque - timedelta(minutes=1):
            codigo = int(datos.get('BugcheckCode') or 0)
            if codigo:
                return 'pantalla azul', f"0x{codigo:X}", datos.get('BugcheckParameter1', '')
            return 'reset en seco', '', 'sin pantalla azul ni orden de Windows'
    for id_evento, hora, datos in eventos:
        if id_evento == 1074 and arranque - timedelta(days=2) <= hora <= arranque:
            proceso = datos.get('param1', '')
            motivo = datos.get('param3', '')
            return 'reinicio pedido', '', f"{proceso} ({motivo})".strip()
    return 'arranque normal', '', ''


def registrar_arranque(base_dir: str, copia: 'CopiaLocal | None'):
    """Anota en reinicios.csv cómo terminó la sesión anterior de Windows y la
    última lectura guardada antes del corte. Una sola vez por arranque."""
    try:
        arranque = _arranque_windows()
        ultima = copia.ultima_fila() if copia else None
        if ultima and ultima[0] >= arranque:
            return  # la app se reinició sin que se reiniciara Windows
        ruta = os.path.join(base_dir, 'reinicios.csv')
        clave = arranque.strftime('%Y-%m-%d %H:%M')
        if os.path.exists(ruta):
            with open(ruta, newline='', encoding='utf-8', errors='replace') as fh:
                if any(f and f[0].startswith(clave) for f in csv.reader(fh)):
                    return
        uptime_ms = (datetime.now() - arranque).total_seconds() * 1000
        eventos = _eventos_sistema([41, 1074, 6008, 1001], uptime_ms + 3 * 86400 * 1000)
        tipo, codigo, detalle = clasificar_arranque(arranque, eventos)

        ultimo_dato, segundos, temp_max = '', '', ''
        if ultima:
            hora, valores = ultima
            ultimo_dato = hora.strftime('%Y-%m-%d %H:%M:%S')
            segundos = f"{(arranque - hora).total_seconds():.0f}"
            temps = []
            for columna, valor in valores.items():
                if '(Temperature)' in columna and valor:
                    try:
                        temps.append((float(valor), columna))
                    except ValueError:
                        pass
            if temps:
                valor, columna = max(temps)
                temp_max = f"{valor:.1f} °C en {columna}"
            logger.info(f"Última lectura antes del arranque ({ultimo_dato}): {valores}")

        nuevo = not os.path.exists(ruta)
        with open(ruta, 'a', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            if nuevo:
                w.writerow(['arranque', 'tipo', 'codigo', 'detalle', 'ultimo_dato_local',
                            'segundos_sin_datos', 'temperatura_maxima_al_final'])
            w.writerow([arranque.strftime('%Y-%m-%d %H:%M:%S'), tipo, codigo, detalle,
                        ultimo_dato, segundos, temp_max])
        logger.info(f"Arranque de Windows {arranque:%Y-%m-%d %H:%M:%S}: {tipo} {codigo} {detalle}".strip())
    except Exception as e:  # nunca frena la captura
        logger.error(f"No pude registrar el arranque: {e}", exc_info=True)


class TelemetryLogger:
    def __init__(self, on_insert=None):
        """
        :param on_insert: callable(n_records: int) opcional. Se llama tras cada
                          inserción exitosa para notificar a la GUI o a tests.
        """
        self._on_insert = on_insert  # callback opcional: fn(n_records)
        self.table_name = os.getenv("CLIENT_TABLE_NAME")
        self.update_time = int(os.getenv("UPDATE_TIME", 10))
        self.retry_seconds = int(os.getenv("DB_RETRY_SECONDS", 30))
        # Tipos de sensor que se dan de alta solos en la tabla Sensor si no están
        # (así entran, por ejemplo, la temperatura del CPU de AMD y los voltajes).
        self.auto_types = _tipos_desde_entorno("AUTO_SENSOR_TYPES", "Temperature,Voltage")
        # Nombres que no se dan de alta solos (valores fijos y VID).
        self.auto_exclude = _regex_desde_entorno("AUTO_SENSOR_EXCLUDE", AUTO_EXCLUDE_DEFAULT)
        # Tipos de sensor que van a la copia local.
        self.local_types = _tipos_desde_entorno("LOCAL_SENSOR_TYPES", "Temperature,Voltage,Power,Fan,Load")
        self.conn = None
        self.cache_hw = {}
        self.cache_sensor = {}
        self.dynamic_patterns = []
        self.sensor_plan = []

        # Evento para señalizar apagado limpio de hilos (también corta los reintentos)
        self._stop_event = threading.Event()
        # LHM no se puede actualizar desde dos hilos a la vez
        self._hw_lock = threading.Lock()

        # Cola thread-safe para comunicación Productor → Consumidor.
        # maxsize=5: si la BD se atrasa se descartan los lotes más viejos,
        # pero la copia local sigue completa.
        self._data_queue = Queue(maxsize=5)

        # 1. LibreHardwareMonitor primero: no depende de la base
        self.pc = self._init_lhm()
        self._hw_list = self._collect_all_hardware()
        for hw, _, _ in self._hw_list:
            hw.Update()

        # 2. Copia local y registro de cómo terminó la sesión anterior de Windows
        self.local = CopiaLocal.desde_entorno(_base_log_dir)
        self._sensores_locales = []
        if self.local is not None:
            for hw, _, _ in self._hw_list:
                for s in hw.Sensors:
                    if str(s.SensorType).upper() in self.local_types:
                        self._sensores_locales.append(s)
                        self.local.columnas.append(f"{hw.Name} | {s.Name} ({s.SensorType})")
            logger.info(f"Copia local en {self.local.carpeta}: {len(self._sensores_locales)} sensores.")
        registrar_arranque(_base_log_dir, self.local)

        # 3. Base: un intento ahora. Si no responde (por ejemplo, la red todavía no
        #    levantó después de un reinicio), el consumidor reintenta sin frenar la captura.
        try:
            self.conn = self._connect_to_db()
            self._load_metadata_cache()
            self.sensor_plan = self._build_sensor_plan()
        except ERRORES_BD as e:
            self.conn = None
            logger.warning(f"Sin conexión con la base al arrancar: {e}. "
                           f"Sigo guardando la copia local y reintento cada {self.retry_seconds} s.")

    # ─── Conexión y configuración ──────────────────────────────────────

    def _connect_to_db(self):
        host = os.getenv("DB_HOST")
        db   = os.getenv("DB_NAME")
        logger.info(f"Conectando a la base de datos '{db}' en {host}...")
        conn = psycopg2.connect(
            host=host,
            database=db,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASS"),
            port=os.getenv("DB_PORT"),
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=15,
            keepalives_interval=10,
            keepalives_count=5
        )
        logger.info(f"Conexión establecida con '{db}' en {host}.")
        return conn

    def _init_lhm(self):
        logger.info("Inicializando LibreHardwareMonitor...")
        c = Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsControllerEnabled = True
        c.IsStorageEnabled = True
        c.IsPsuEnabled = True
        c.Open()
        return c

    # ─── Carga de metadata y resolución de sensores ────────────────────

    def _load_metadata_cache(self):
        """Carga la tabla Componente y Sensor de la BD en memoria."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT hardware_id, hardware_type FROM Componente")
                self.cache_hw = {row[1].upper(): row[0] for row in cur.fetchall()}

                cur.execute("SELECT sensor_id, sensor_name, sensor_type FROM Sensor")
                rows = cur.fetchall()

            self.cache_sensor = {}
            self.dynamic_patterns = []

            for sensor_id, sensor_name, sensor_type in rows:
                key = (sensor_name.upper(), sensor_type.upper())
                self.cache_sensor[key] = sensor_id
                if '%' in sensor_name:
                    self.dynamic_patterns.append((sensor_name.upper(), sensor_type.upper(), sensor_id))

            logger.info(
                f"Cache cargada: {len(self.cache_hw)} componentes, "
                f"{len(self.cache_sensor)} sensores "
                f"({len(self.dynamic_patterns)} con patrón wildcard)."
            )
        except Exception as e:
            logger.error(f"Error cargando la cache de la base de datos: {e}", exc_info=True)

    def _resolve_sensor_id(self, s_name: str, s_type: str) -> int | None:
        """Resuelve el sensor_id. Se ejecuta una sola vez por sensor en _build_sensor_plan."""
        key = (s_name.upper(), s_type.upper())

        # 1. Coincidencia exacta en cache
        if key in self.cache_sensor:
            return self.cache_sensor[key]

        # 2. Coincidencia por patrón wildcard (%) de la BD
        for pat_name, pat_type, pat_id in self.dynamic_patterns:
            if pat_type == s_type.upper() and fnmatch.fnmatch(s_name.upper(), pat_name):
                self.cache_sensor[key] = pat_id
                return pat_id

        # Los valores fijos y los VID (AUTO_SENSOR_EXCLUDE) no se dan de alta solos;
        # si alguien los carga a mano en la tabla Sensor, entran por el paso 1.
        if self.auto_exclude and self.auto_exclude.search(s_name):
            return None

        # 3. Auto-registro si coincide con un patrón regex conocido
        s_type_lower = s_type.lower()
        for compiled_re, expected_type in DYNAMIC_REGEX:
            if s_type_lower == expected_type and compiled_re.match(s_name):
                return self._register_sensor(s_name, s_type)

        # 4. Auto-registro por tipo (AUTO_SENSOR_TYPES). Cubre los nombres que no
        #    siguen el patrón de Intel, como "Core (Tctl/Tdie)" o "CCD1 (Tdie)" de
        #    AMD, los voltajes de la placa y las temperaturas de las memorias DDR5.
        if s_type.upper() in self.auto_types:
            return self._register_sensor(s_name, s_type)

        return None

    def _register_sensor(self, s_name: str, s_type: str) -> int | None:
        """Da de alta el sensor en la tabla Sensor (o toma el id si ya existe).

        En la base, sensor_id no se numera solo (no tiene secuencia): el número lo
        pone la app, el más alto + 1. El candado de la base (pg_advisory_xact_lock)
        hace que dos PCs que arrancan a la vez den de alta de a una y no tomen el
        mismo número; se suelta solo con el commit o el rollback."""
        key = (s_name.upper(), s_type.upper())
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (CANDADO_ALTA_SENSOR,))
                # Otra PC pudo darlo de alta después de que esta cargó la cache.
                cur.execute("SELECT sensor_id FROM Sensor WHERE sensor_name=%s AND sensor_type=%s", (s_name, s_type))
                res = cur.fetchone()
                nuevo = res is None
                if nuevo:
                    cur.execute(
                        "INSERT INTO Sensor (sensor_id, sensor_name, sensor_type) "
                        "SELECT COALESCE(MAX(sensor_id), 0) + 1, %s, %s FROM Sensor RETURNING sensor_id",
                        (s_name, s_type)
                    )
                    res = cur.fetchone()
            self.conn.commit()
            new_id = res[0]
            self.cache_sensor[key] = new_id
            if nuevo:
                logger.info(f"Sensor dinámico auto-registrado: {s_name} ({s_type}) → id={new_id}")
            return new_id
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.error(f"Error auto-registrando sensor '{s_name}': {e}")
            return None

    def _collect_all_hardware(self):
        """Recorre iterativamente todo el árbol de hardware y devuelve una lista plana
        de (hw_object, lhm_type_str, db_hw_type_upper)."""
        result = []
        stack = list(self.pc.Hardware)
        while stack:
            hw = stack.pop()
            lhm_type = str(hw.HardwareType)
            db_hw_type = LHM_TO_DB_HW.get(lhm_type, "").upper()
            result.append((hw, lhm_type, db_hw_type))
            sub = list(hw.SubHardware)
            if sub:
                stack.extend(sub)
        return result

    def _build_sensor_plan(self):
        """Se ejecuta una sola vez al inicio. Descubre todos los sensores del hardware,
        resuelve sus IDs, y devuelve una lista de tuplas listas para el loop:
        [(hw_object, hardware_name, hardware_id, sensor_object, sensor_id), ...]

        Los sensores que no resuelven ID se descartan aquí y no se vuelven a evaluar."""
        logger.info("Construyendo plan de sensores (resolución única de IDs)...")

        with self._hw_lock:
            for hw, _, _ in self._hw_list:
                hw.Update()

        plan = []
        skipped = 0

        for hw, lhm_hw_type, db_hw_type in self._hw_list:
            h_id = self.cache_hw.get(db_hw_type)
            if h_id is None:
                continue

            hw_name = str(hw.Name)

            for s in hw.Sensors:
                s_name = s.Name
                s_type = str(s.SensorType)

                # Rename "Memory" → "Virtual Memory" para sensores de RAM virtual
                resolved_name = "Virtual Memory" if (s_name == "Memory" and "Virtual Memory" in hw_name) else s_name

                s_id = self._resolve_sensor_id(resolved_name, s_type)
                if s_id is None:
                    skipped += 1
                    logger.debug(f"Sensor descartado (sin ID en BD): '{resolved_name}' tipo '{s_type}' en '{hw_name}'")
                    continue

                plan.append((hw, hw_name, h_id, s, s_id))

        logger.info(f"Plan de sensores construido: {len(plan)} sensores activos, {skipped} descartados sin ID en BD.")
        return plan

    # ─── Reconexión ────────────────────────────────────────────────────

    def _reconnect_db(self, primera: bool = False):
        """Intenta (re)conectar a la base de datos indefinidamente.
        Con primera=True es la primera conexión (la base no estaba al arrancar)."""
        if primera:
            logger.warning("Esperando la base de datos para empezar a insertar (la copia local sigue)...")
        else:
            logger.critical("Conexión perdida con la base de datos. Iniciando ciclo de reconexión...")
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            try:
                try:
                    if self.conn and not self.conn.closed:
                        self.conn.close()
                        logger.debug("Conexión anterior cerrada correctamente antes de reconectar.")
                except Exception:
                    pass

                self.conn = self._connect_to_db()
                self._load_metadata_cache()
                if not self.sensor_plan:
                    self.sensor_plan = self._build_sensor_plan()
                logger.info(f"Reconexión exitosa a la base de datos (intento #{attempt}).")
                break
            except ERRORES_BD as e:
                logger.warning(f"Reconexión fallida (intento #{attempt}): {e}. Reintentando en {self.retry_seconds}s...")
                self._stop_event.wait(timeout=self.retry_seconds)
            except Exception as e:
                logger.error(f"Error inesperado durante reconexión (intento #{attempt}): {e}. Reintentando en {self.retry_seconds}s...", exc_info=True)
                self._stop_event.wait(timeout=self.retry_seconds)

    # ─── Hilo Productor: captura de sensores ───────────────────────────

    def _producer_loop(self):
        """Hilo dedicado a la lectura de hardware.
        Captura timestamp → actualiza hardware → lee valores → pone batch en la cola."""
        logger.info("Hilo productor de sensores iniciado.")
        avisado_cola_llena = False

        while not self._stop_event.is_set():
            try:
                # 1. Capturar timestamp al inicio de la lectura
                now = datetime.now()

                # 2. Actualizar todo el hardware una sola vez (lo usan la base y la copia local)
                with self._hw_lock:
                    for hw, _, _ in self._hw_list:
                        hw.Update()

                # 3. Copia local: no depende de la base
                if self.local is not None:
                    try:
                        self.local.escribir(now, [
                            float(s.Value) if s.Value is not None else None
                            for s in self._sensores_locales
                        ])
                    except OSError as e:
                        logger.error(f"No pude escribir la copia local: {e}")

                # 4. Leer valores del plan pre-resuelto (vacío hasta que conecte la base).
                #    Un sensor sin valor no se inserta: guardar 0 hacía parecer que la
                #    temperatura del CPU era 0 °C cuando LHM no la podía leer.
                batch = []
                for _, hw_name, h_id, sensor, s_id in self.sensor_plan:
                    if sensor.Value is None:
                        continue
                    batch.append((now, h_id, s_id, hw_name, float(sensor.Value)))

                # 5. Enviar batch a la cola sin bloquear: si la base viene atrasada se
                #    descarta el lote más viejo, así la lectura y la copia local no se frenan.
                if batch:
                    try:
                        self._data_queue.put_nowait(batch)
                    except Full:
                        try:
                            self._data_queue.get_nowait()
                        except Empty:
                            pass
                        try:
                            self._data_queue.put_nowait(batch)
                        except Full:
                            pass
                        if not avisado_cola_llena:
                            logger.warning("La base viene atrasada: se descartan los lotes más viejos "
                                           "(la copia local sigue completa).")
                            avisado_cola_llena = True
                    else:
                        avisado_cola_llena = False

            except Exception as e:
                logger.error(f"Error en hilo productor durante la captura de sensores: {e}", exc_info=True)

            # Esperar el intervalo, pero interrumpible por stop_event
            self._stop_event.wait(timeout=self.update_time)

        logger.info("Hilo productor finalizado.")

    # ─── Hilo Principal (Consumidor): inserción en BD ──────────────────

    def _consumer_loop(self):
        """Loop principal: toma batches de la cola e inserta en la BD.
        queue.get() bloquea hasta que el productor deposita datos → sincronización natural."""
        insert_query = f"INSERT INTO {self.table_name} (timestamp, hardware_id, sensor_id, hardware_name, value) VALUES %s"
        fallos_consecutivos = 0

        while not self._stop_event.is_set():
            # --- Si la base no estaba al arrancar, esperarla (la copia local sigue) ---
            if self.conn is None:
                self._reconnect_db(primera=True)
                continue

            # --- Esperar datos del productor (con timeout para poder chequear stop_event) ---
            try:
                batch = self._data_queue.get(timeout=self.update_time * 2)
            except Empty:
                logger.debug("Consumidor: timeout esperando datos del productor (posible retraso o inactividad).")
                continue

            # --- Insertar en la BD ---
            try:
                with self.conn.cursor() as cur:
                    extras.execute_values(cur, insert_query, batch)
                    self.conn.commit()

                n = len(batch)
                logger.info(f"Inserción OK: {n} registros insertados en '{self.table_name}'.")
                fallos_consecutivos = 0

                # Notificar a la GUI (o cualquier consumidor externo) de la inserción
                if self._on_insert is not None:
                    try:
                        self._on_insert(n)
                    except Exception:
                        pass

            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.critical(f"Error de conexión con la BD durante inserción: {e}")
                self._reconnect_db()
                fallos_consecutivos = 0
                # Reintentar insertar el batch perdido tras reconectar
                logger.info("Reintentando inserción del batch pendiente tras reconexión...")
                try:
                    with self.conn.cursor() as cur:
                        extras.execute_values(cur, insert_query, batch)
                        self.conn.commit()
                    logger.info(f"Batch pendiente re-insertado correctamente ({len(batch)} registros).")
                except Exception as retry_e:
                    logger.error(f"Fallo al re-insertar batch pendiente tras reconexión: {retry_e}", exc_info=True)

            except Exception as e:
                fallos_consecutivos += 1
                logger.error(f"Error inesperado durante inserción (fallo {fallos_consecutivos}/3): {e}", exc_info=True)
                if fallos_consecutivos >= 3:
                    logger.critical("Límite de 3 fallos consecutivos alcanzado. Forzando reconexión a la BD...")
                    self._reconnect_db()
                    fallos_consecutivos = 0

    # ─── Entry point ───────────────────────────────────────────────────

    def run(self):
        logger.info(f"Iniciando captura de telemetria en tabla {self.table_name} (Intervalo: {self.update_time}s)...")

        # Lanzar hilo productor como daemon (muere automáticamente si el principal termina)
        producer = threading.Thread(
            target=self._producer_loop,
            name="SensorProducer",
            daemon=True
        )
        producer.start()

        try:
            # El hilo principal actúa como consumidor (inserción en BD)
            self._consumer_loop()
        except KeyboardInterrupt:
            logger.info("Deteniendo captura de telemetria por orden del usuario (Ctrl+C).")
        finally:
            # Señalizar apagado limpio
            logger.info("Señalizando apagado a los hilos...")
            self._stop_event.set()
            producer.join(timeout=5)
            if self.local is not None:
                self.local.cerrar()
            logger.info("Cerrando LibreHardwareMonitor...")
            self.pc.Close()
            if self.conn and not self.conn.closed:
                self.conn.close()
                logger.info("Conexión con la base de datos cerrada.")
            logger.info("Recursos liberados. Script finalizado.")


if __name__ == "__main__":
    app = TelemetryLogger()
    app.run()


# Mother 
# Vcore => Voltaje que recibe el procesador.
# VRM MOS => Sensor de los reguladores de voltaje
# CPU Fan => Velocidad de ventilador de CPU
# System Fan #X => Velocidad de ventilador adicional (Pueden ser varios)

# CPU
# CORE (Tctl/Tdie) => Sensor de temperatura principal
# Package => Consumo total en Watts
# CPU Total => Porcentaje de uso global
# CPU Core #X => Porcentaje de uso de procesador logico

# Almacenamiento
# Composite Temperature => Temperatura general del reporte SMART
# Used Space => Porcentaje de espacio utilizado
# Read Activity => Porcentaje de lectura 
# Write Activity => Porcentaje de escritura
# Total Activity => Porcentaje de actividad total

# GPU
# GPU Package => Consumo de GPU en Watts
# GPU Core => Temperatura del procesador grafico
# GPU Memory Junction => Temperatura mas elevada de los modulos de memoria
# GPU Fan #X => Velocidad de los ventiladores 

# RAM
# DIMM => Sensores térmicos
# Physical Memory => Porcentaje de ocupación de memoria física
# Virtual Memory => Porcentaje de ocupación de memoria virtual