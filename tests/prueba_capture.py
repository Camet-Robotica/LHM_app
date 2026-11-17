"""Prueba de capture.py con LibreHardwareMonitor y la base simulados.

No necesita permisos de administrador, ni red, ni base, ni pythonnet:

    python tests/prueba_capture.py . <carpeta temporal>

Simula una PC que arranca sin base (falla la conexión tres veces), y comprueba que
la copia local se escribe desde el arranque, que la base recibe datos cuando conecta,
que se dan de alta los sensores de AUTO_SENSOR_TYPES, que un sensor sin valor no se
inserta como 0 y que reinicios.csv anota el corte anterior.
"""
import csv
import glob
import importlib
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta

REPO, TMP = sys.argv[1], sys.argv[2]
os.makedirs(TMP, exist_ok=True)
fallas = []


def chequear(cond, texto):
    print(("OK   " if cond else "FALLA") + " " + texto)
    if not cond:
        fallas.append(texto)


# ─── Módulos simulados ──────────────────────────────────────────────────────
clr = types.ModuleType('clr')
clr.AddReference = lambda ruta: None
sys.modules['clr'] = clr

dotenv = types.ModuleType('dotenv')
dotenv.load_dotenv = lambda *a, **k: None
sys.modules['dotenv'] = dotenv


class Sensor:
    def __init__(self, nombre, tipo, valor):
        self.Name, self.SensorType, self.Value = nombre, tipo, valor


class Hardware:
    def __init__(self, nombre, tipo, sensores, sub=()):
        self.Name, self.HardwareType = nombre, tipo
        self.Sensors = [Sensor(*s) for s in sensores]
        self.SubHardware = list(sub)
        self.actualizaciones = 0

    def Update(self):
        self.actualizaciones += 1
        for s in self.Sensors:
            if s.Value is not None and s.SensorType == 'Temperature':
                s.Value += 0.1


class Computer:
    def __init__(self):
        superio = Hardware('ITE IT8689E', 'SuperIO', [('Vcore', 'Voltage', 1.2), ('+12V', 'Voltage', 12.1),
                                                      ('CPU Fan', 'Fan', 1200.0)])
        self.Hardware = [
            Hardware('AMD Ryzen 7 9700X', 'Cpu', [('Core (Tctl/Tdie)', 'Temperature', 55.0),
                                                 ('CCD1 (Tdie)', 'Temperature', 50.0),
                                                 ('CPU Total', 'Load', 30.0), ('Package', 'Power', 60.0),
                                                 ('Core #1', 'Clock', 5000.0)]),
            Hardware('Gigabyte B650I AX', 'Motherboard', [], [superio]),
            Hardware('NVIDIA GeForce RTX 5060', 'GpuNvidia', [('GPU Core', 'Temperature', 59.0),
                                                             ('GPU Package', 'Power', 42.0),
                                                             ('GPU Hot Spot', 'Temperature', None)]),
        ]

    def Open(self):
        pass

    def Close(self):
        pass


lhm = types.ModuleType('LibreHardwareMonitor')
lhm_hw = types.ModuleType('LibreHardwareMonitor.Hardware')
lhm_hw.Computer = Computer
lhm.Hardware = lhm_hw
sys.modules['LibreHardwareMonitor'] = lhm
sys.modules['LibreHardwareMonitor.Hardware'] = lhm_hw


class OperationalError(Exception):
    pass


class InterfaceError(Exception):
    pass


class Base:
    """Base simulada: guarda lo que se inserta."""
    def __init__(self):
        self.sensores = {('CPU Total', 'Load'): 10, ('GPU Core', 'Temperature'): 11, ('GPU Package', 'Power'): 12}
        self.siguiente = 100
        self.lotes = []
        self.fallar_conexiones = 0
        self.intentos = 0


BASE = Base()


class Cursor:
    def __init__(self):
        self._r = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        if sql.startswith('SELECT hardware_id'):
            self._r = [(1, 'CPU'), (2, 'GPU'), (3, 'MOTHERBOARD'), (4, 'MEMORIA RAM'), (5, 'ALMACENAMIENTO')]
        elif sql.startswith('SELECT sensor_id, sensor_name'):
            self._r = [(i, n, t) for (n, t), i in BASE.sensores.items()]
        elif sql.startswith('INSERT INTO Sensor'):
            clave = tuple(args)
            if clave in BASE.sensores:
                self._r = []
            else:
                BASE.sensores[clave] = BASE.siguiente
                BASE.siguiente += 1
                self._r = [(BASE.sensores[clave],)]
        elif sql.startswith('SELECT sensor_id FROM Sensor'):
            self._r = [(BASE.sensores[tuple(args)],)]

    def fetchall(self):
        return self._r

    def fetchone(self):
        return self._r[0] if self._r else None


class Conexion:
    closed = 0

    def cursor(self):
        return Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = 1


def conectar(**k):
    BASE.intentos += 1
    if BASE.intentos <= BASE.fallar_conexiones:
        raise OperationalError('timeout expired')
    return Conexion()


psycopg2 = types.ModuleType('psycopg2')
psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.connect = OperationalError, InterfaceError, conectar
extras = types.ModuleType('psycopg2.extras')
extras.execute_values = lambda cur, sql, lote: BASE.lotes.append(list(lote))
psycopg2.extras = extras
sys.modules['psycopg2'] = psycopg2
sys.modules['psycopg2.extras'] = extras

# ─── Entorno ────────────────────────────────────────────────────────────────
datos = os.path.join(TMP, 'datos_locales')
os.environ.update({'CLIENT_TABLE_NAME': 'recursos_prueba', 'UPDATE_TIME': '1', 'DB_RETRY_SECONDS': '1',
                   'LOCAL_DIR': datos, 'LOCAL_RETENTION_DAYS': '14'})
sys.path.insert(0, REPO)
capture = importlib.import_module('capture')
capture._base_log_dir = TMP

# Una corrida anterior que se cortó: último dato a las 11:22:38, arranque a las 11:23:09
os.makedirs(datos, exist_ok=True)
arranque = datetime(2026, 11, 17, 11, 23, 9)
with open(os.path.join(datos, 'telemetria_2026-11-17_032000.csv'), 'w', newline='', encoding='utf-8') as fh:
    w = csv.writer(fh)
    w.writerow(['hora', 'AMD Ryzen 7 9700X | Core (Tctl/Tdie) (Temperature)', 'ITE IT8689E | +12V (Voltage)'])
    w.writerow(['2026-11-17 11:22:28', '71.000', '12.050'])
    w.writerow(['2026-11-17 11:22:38', '72.500', '11.900'])
capture._arranque_windows = lambda: arranque
capture._eventos_sistema = lambda ids, ms: [(41, arranque + timedelta(seconds=1), {'BugcheckCode': '0'})]

# ─── 1. Arranca sin base: no se cae, guarda en local y después conecta ─────
BASE.fallar_conexiones = 3
t = capture.TelemetryLogger()
chequear(t.conn is None, 'sin base al arrancar no se cae (conn = None)')
chequear(len(t._sensores_locales) == 10, f'copia local con 10 sensores (tiene {len(t._sensores_locales)}, sin Clock)')
hilo = threading.Thread(target=t.run, daemon=True)
hilo.start()
time.sleep(6)
t._stop_event.set()
hilo.join(timeout=10)
chequear(not hilo.is_alive(), 'se detiene limpio con _stop_event')
chequear(BASE.intentos >= 4, f'reintentó la conexión ({BASE.intentos} intentos)')
nombres = {n for (n, _t) in BASE.sensores}
for n in ('Core (Tctl/Tdie)', 'CCD1 (Tdie)', 'Vcore', '+12V'):
    chequear(n in nombres, f'dio de alta {n}')
chequear('Package' not in nombres, 'no da de alta tipos fuera de AUTO_SENSOR_TYPES (Power)')
chequear('CPU Fan' in nombres, 'la regla vieja de ventiladores sigue andando (CPU Fan)')
chequear(len(BASE.lotes) >= 1, f'insertó lotes en la base después de conectar ({len(BASE.lotes)})')
if BASE.lotes:
    ids = {fila[2] for fila in BASE.lotes[-1]}
    chequear(BASE.sensores[('Core (Tctl/Tdie)', 'Temperature')] in ids, 'el lote incluye la temperatura del CPU')
    chequear(BASE.sensores[('GPU Hot Spot', 'Temperature')] not in ids, 'un sensor sin valor no se inserta como 0')
    chequear(len(BASE.lotes[-1]) == 8, f'8 sensores por lote, sin el que no da valor (tiene {len(BASE.lotes[-1])})')
    chequear(all(fila[4] != 0.0 or fila[2] == 0 for fila in BASE.lotes[-1]), 'ningún 0 inventado')

nuevos = sorted(set(glob.glob(os.path.join(datos, 'telemetria_*.csv'))) - {os.path.join(datos, 'telemetria_2026-11-17_032000.csv')})
chequear(len(nuevos) == 1, 'creó un CSV local nuevo')
if nuevos:
    with open(nuevos[0], encoding='utf-8') as fh:
        filas = list(csv.reader(fh))
    chequear(len(filas) >= 5, f'el CSV local tiene filas desde el arranque, con la base caída ({len(filas) - 1} filas)')
    chequear(len(filas) - 1 > len(BASE.lotes), 'la copia local tiene más lecturas que la base (empezó antes de conectar)')
    chequear(any(c.endswith('| GPU Hot Spot (Temperature)') for c in filas[0]), 'incluye sensores sin valor como columna')
    chequear(filas[1][filas[0].index('NVIDIA GeForce RTX 5060 | GPU Hot Spot (Temperature)')] == '', 'un valor None queda vacío')

# ─── 2. Registro del arranque ────────────────────────────────────────────────
with open(os.path.join(TMP, 'reinicios.csv'), encoding='utf-8') as fh:
    filas = list(csv.reader(fh))
print('reinicios.csv:', filas)
chequear(len(filas) == 2 and filas[1][1] == 'reset en seco', 'anotó el reset en seco')
chequear(filas[1][4] == '2026-11-17 11:22:38' and filas[1][5] == '31', 'con el último dato local y 31 s sin datos')
chequear('72.5' in filas[1][6], 'y la temperatura más alta al final')
capture.registrar_arranque(TMP, capture.CopiaLocal(datos, 14))
with open(os.path.join(TMP, 'reinicios.csv'), encoding='utf-8') as fh:
    chequear(len(list(csv.reader(fh))) == 2, 'no repite el mismo arranque')

# ─── 3. Clasificación ───────────────────────────────────────────────────────
a = datetime(2026, 11, 17, 3, 58, 22)
chequear(capture.clasificar_arranque(a, [(41, a, {'BugcheckCode': '10', 'BugcheckParameter1': '0xfffff'})])[:2]
         == ('pantalla azul', '0xA'), 'pantalla azul 0xA')
chequear(capture.clasificar_arranque(a, [(1074, a - timedelta(minutes=2), {'param1': 'shutdown.exe', 'param3': 'Otros'})])[0]
         == 'reinicio pedido', 'reinicio pedido (1074)')
chequear(capture.clasificar_arranque(a, [(41, a - timedelta(days=3), {'BugcheckCode': '0'})])[0]
         == 'arranque normal', 'un 41 viejo no cuenta')

print()
print('FALLAS: ' + (', '.join(fallas) if fallas else 'ninguna'))
sys.exit(1 if fallas else 0)
