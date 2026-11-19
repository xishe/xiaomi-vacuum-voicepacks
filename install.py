"""Install a verified ijai voice pack on one robot over the local network."""

import argparse
from configparser import Error as ConfigError
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import logging
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
import warnings
from zipfile import BadZipFile


MODEL = "ijai.vacuum.v19"
PROFILES = ("ijai.vacuum.v3", "ijai.vacuum.v17", "ijai.vacuum.v18", MODEL)
PACK = Path(__file__).resolve().parent / "packs" / MODEL / "mop-2s-witcher-v4.1-v19.zip"
PACK_SHA256 = "d90720903f48acc2a7157a9cdff7f3cc453c02a469b22c66c69fc06ff586245a"
LOCALES = ("ru_RU", "fr_FR")
DOWNLOAD_TIMEOUT = 105


def verified_pack():
    if not PACK.is_file():
        raise ValueError(f"Файл {PACK.name} должен быть в папке packs/{MODEL}.")
    data = PACK.read_bytes()
    if hashlib.sha256(data).hexdigest() != PACK_SHA256:
        raise ValueError("SHA-256 не совпадает с контрольной суммой пакета V4.1.")
    return data


def verified_custom_pack(path, template, model):
    from voicepack import compare_with_template, read_pack

    package = read_pack(path)
    compare_with_template(package, read_pack(template), model)
    return package.archive


def read_token():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("miIO-токен робота: ").strip()
        except getpass.GetPassWarning:
            raise ValueError("Откройте обычный терминал с поддержкой скрытого ввода.") from None
    if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        raise ValueError("Токен должен содержать 32 шестнадцатеричных символа.")
    return token


def action(device, action_id, inputs=()):
    return device.send("action", {
        "did": f"call-14-{action_id}",
        "siid": 14,
        "aiid": action_id,
        "in": list(inputs),
    }, retry_count=0)


def voice_status(device):
    result = action(device, 2)
    if result.get("code") != 0:
        raise ValueError(f"Робот вернул код {result.get('code')} при чтении статуса озвучки.")
    properties = {entry["piid"]: entry.get("value") for entry in result.get("out") or []}
    return properties.get(2), properties.get(3), properties.get(4)


def robot_status(device):
    indicators = {"state": (2, 1), "fault": (2, 2), "battery": (3, 1)}
    query = [{"did": name, "siid": siid, "piid": piid}
             for name, (siid, piid) in indicators.items()]
    reply = device.send("get_properties", query, retry_count=1)
    return {entry["did"]: entry.get("value") for entry in reply}


def handler_for(robot_ip, url_path, package, sent):
    class VoicePackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def serve(self, send_body):
            if self.client_address[0] != robot_ip or self.path != url_path:
                self.send_error(404)
                return
            self.connection.settimeout(15)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(package)))
            self.send_header("Connection", "close")
            self.end_headers()
            if send_body:
                try:
                    self.wfile.write(package)
                    self.wfile.flush()
                except OSError:
                    return
                sent.set()

        def do_GET(self):
            self.serve(send_body=True)

        def do_HEAD(self):
            self.serve(send_body=False)

    return VoicePackHandler


def install(device, robot_ip, package, locale, communication_error):
    status = robot_status(device)
    active, task, _progress = voice_status(device)
    print(f"Активный слот: {active}. Заряд: {status.get('battery', '?')}%.", flush=True)
    if status.get("state") != 4:
        raise ValueError("Поставьте робот на зарядную базу и повторите установку.")
    fault = status.get("fault")
    if fault not in {0, 2103, 2104} and (fault, status.get("battery")) != (2105, 100):
        raise ValueError(f"Проверьте состояние робота в Xiaomi Home. Код устройства: {fault}.")
    if task not in {0, 15} or not active:
        raise ValueError(f"Робот пока не готов принять озвучку. Статус загрузки: {task}.")
    locale = locale or ("fr_FR" if active == "ru_RU" else "ru_RU")
    if locale == active:
        raise ValueError("Этот слот уже активен. Выберите второй слот или уберите --locale.")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((robot_ip, 54321))
        host = route.getsockname()[0]
    url_path = f"/{secrets.token_hex(12)}.zip"
    sent = threading.Event()
    handler = handler_for(robot_ip, url_path, package, sent)
    with ThreadingHTTPServer((host, 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            print(f"Передаю пакет в слот {locale} по локальной сети.", flush=True)
            inputs = [
                {"piid": 1, "value": locale},
                {"piid": 5, "value": f"http://{host}:{server.server_port}{url_path}"},
                {"piid": 6, "value": hashlib.md5(package).hexdigest()},
            ]
            try:
                response = action(device, 1, inputs)
                if response.get("code") != 0:
                    raise ValueError(f"Робот отклонил загрузку. Код: {response.get('code')}.")
            except communication_error:
                print("Ответ на команду задержался. Проверяю состояние загрузки.", flush=True)

            previous = None
            until = time.monotonic() + DOWNLOAD_TIMEOUT
            while time.monotonic() < until:
                try:
                    state = voice_status(device)
                except communication_error:
                    time.sleep(3)
                    continue
                current, status_code, progress = state
                if state != previous:
                    print(f"Статус: {status_code}, загрузка: {progress}%, слот: {current}.", flush=True)
                    previous = state
                if sent.is_set() and current == locale and status_code in {0, 15}:
                    print(f"Голосовой пакет активен в слоте {locale}.")
                    return
                if status_code in {17, 18, 19, 20, 21, 22}:
                    raise ValueError(f"Ошибка установки на роботе. Код загрузки: {status_code}.")
                time.sleep(3)
            if sent.is_set():
                raise ValueError("Архив передан. Робот пока не подтвердил активацию. Проверьте --status.")
            raise ValueError("Робот не получил архив. Проверьте доступ к компьютеру из локальной сети.")
        finally:
            server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Локальная установка голосовых пакетов Xiaomi/ijai")
    parser.add_argument("--ip", type=IPv4Address, help="локальный IPv4-адрес робота")
    parser.add_argument("--model", choices=PROFILES, help="точная модель робота для своего пакета")
    parser.add_argument("--package", type=Path, help="файл собственного пакета")
    parser.add_argument("--template", type=Path, help="заводской ZIP для сверки своего пакета")
    parser.add_argument("--locale", choices=LOCALES, help="языковой слот для установки")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="проверить ZIP на компьютере")
    mode.add_argument("--status", action="store_true", help="прочитать состояние без установки")
    options = parser.parse_args()
    if not options.check and options.ip is None:
        parser.error("укажите --ip либо выберите --check")
    if options.package and not options.status:
        if not options.template:
            parser.error("для --package укажите --template с заводским ZIP.")
        if not options.model and not options.check:
            parser.error("для установки своего пакета укажите точную --model.")
    if not options.status and not options.package and options.model not in {None, MODEL}:
        parser.error(f"Встроенный пакет предназначен для {MODEL}. Для другой модели укажите --package.")

    try:
        if not options.status:
            data = (verified_custom_pack(options.package, options.template, options.model)
                    if options.package else verified_pack())
        if options.check:
            print(f"Пакет прошёл проверку, {len(data)} байт.")
            return 0
        try:
            from miio.device import Device
            from miio.exceptions import DeviceException
        except ImportError:
            raise ValueError("Установите зависимости командой python -m pip install -r requirements.txt.") from None

        logging.disable(logging.CRITICAL)
        ip = str(options.ip)
        token = read_token()
        device = Device(ip, token, timeout=3, start_id=secrets.randbelow(100000))
        try:
            info = device.send("miIO.info", retry_count=1)
            detected = info.get("model")
            print(f"{detected}, прошивка {info.get('fw_ver')}.", flush=True)
            if options.status:
                if detected not in PROFILES:
                    print("Модель определена. Её профиль отсутствует в локальном установщике.")
                    return 0
                status = robot_status(device)
                active, code, progress = voice_status(device)
                print(f"Режим: {status.get('state')}, заряд: {status.get('battery')}%, слот: {active}, статус озвучки: {code}, загрузка: {progress}%.")
            else:
                selected = options.model or MODEL
                if detected != selected:
                    raise ValueError(f"Выбран профиль {selected}, по указанному IP ответила модель {detected}.")
                if selected != MODEL:
                    print("Экспериментальный профиль, проверенный по спецификации MIoT.", flush=True)
                install(device, ip, data, options.locale, DeviceException)
        except DeviceException:
            raise ValueError("Связь с роботом не подтверждена. Проверьте IP, токен и доступ по локальной сети.") from None
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nОперация прервана.", file=sys.stderr)
        return 1
    except (ValueError, BadZipFile, ConfigError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError:
        print("Ошибка чтения ZIP или открытия локального HTTP-сервера.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
