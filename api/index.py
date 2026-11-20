"""Точка входа Vercel: WSGI-приложение radar-wms-plus.
Диск read-only, кроме /tmp — демо-данные генерируем в /tmp при холодном старте."""
import os, sys, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Если рядом нет реального baltrad_wms.cfg — работаем в демо-режиме
DEMO = not os.path.exists(os.path.join(ROOT, "baltrad_wms.cfg"))
if DEMO:
    os.environ["RADAR_DEMO_DATA"] = "/tmp/radar_demo"
    os.environ["RADAR_CFG"] = "/tmp/radar_demo.cfg"

_lock = threading.Lock()

def ensure_demo():
    if not DEMO:
        return
    with _lock:
        if not (os.path.exists("/tmp/radar_demo/radar.db")
                and os.path.exists("/tmp/radar_demo.cfg")):
            import make_demo_data
            make_demo_data.main()

ensure_demo()
import radar_server  # конфиг читается при импорте — после генерации!

def app(environ, start_response):
    ensure_demo()  # если /tmp очистился — перегенерируем (кэш сервера сам обновится)
    return radar_server.application(environ, start_response)

application = app  # Vercel принимает оба имени
