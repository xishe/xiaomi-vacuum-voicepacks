"""Inspect, extract, and assemble Xiaomi/ijai-style ZIP voice packs."""

import argparse
from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass
from hashlib import md5
from io import BytesIO
import os
from pathlib import Path
import re
import sys
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from mutagen import MutagenError
from mutagen.mp3 import MP3


MANIFEST = "md5.ini"
TRACK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.mp3", re.I)
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
V19 = "ijai.vacuum.v19"


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    layer: int


@dataclass
class VoicePack:
    tracks: dict
    formats: dict
    archive: bytes

    @property
    def zip_bytes(self):
        return len(self.archive)


def audio_format(name, data):
    try:
        info = MP3(BytesIO(data)).info
    except MutagenError:
        raise ValueError(f"{name}: не читается как MP3.") from None
    return AudioFormat(info.sample_rate, info.channels, info.layer)


def read_pack(path):
    if path.stat().st_size > MAX_UNPACKED_BYTES:
        raise ValueError("ZIP превышает лимит инструмента в 256 МиБ.")
    serialized = path.read_bytes()
    with ZipFile(BytesIO(serialized)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(set(map(str.casefold, names))) != len(names):
            raise ValueError("В ZIP повторяются имена файлов.")
        tracks = [name for name in names if name != MANIFEST]
        if MANIFEST not in names or not tracks or any(not TRACK_NAME.fullmatch(name) for name in tracks):
            raise ValueError("Нужен ZIP с файлами MP3 и md5.ini в корне.")
        if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
            raise ValueError("Распакованный ZIP превышает лимит инструмента в 256 МиБ.")
        config = ConfigParser(interpolation=None)
        config.read_string(archive.read(MANIFEST).decode("utf-8-sig"))
        if set(config.sections()) != set(tracks) or config.defaults():
            raise ValueError("Секции md5.ini не совпадают с файлами в ZIP.")
        payload = {}
        for name in tracks:
            payload[name] = archive.read(name)
            expected = config[name].get("md5", "").strip().lower()
            if expected != md5(payload[name]).hexdigest():
                raise ValueError(f"{name}: контрольная сумма MD5 не совпадает.")
    formats = {name: audio_format(name, track) for name, track in payload.items()}
    return VoicePack(payload, formats, serialized)


def ensure_audio(name, actual, expected, model=None):
    # The installed v19 pack uses mono at 48 kHz for every track.
    compatible_v19 = model == V19 and actual == AudioFormat(48000, 1, 3)
    if actual != expected and not compatible_v19:
        raise ValueError(
            f"{name}: нужен формат шаблона "
            f"({expected.sample_rate} Гц, каналов {expected.channels}, MPEG layer {expected.layer})."
        )


def compare_with_template(pack, template, model=None):
    extra = sorted(pack.tracks.keys() - template.tracks.keys())
    missing = sorted(template.tracks.keys() - pack.tracks.keys())
    if extra or missing:
        raise ValueError("Набор файлов отличается от шаблона. "
                         f"Отсутствуют: {', '.join(missing) or '—'}. "
                         f"Лишние: {', '.join(extra) or '—'}.")
    for name in pack.tracks:
        ensure_audio(name, pack.formats[name], template.formats[name], model)


def build(template_path, clips_dir, output, model=None):
    template = read_pack(template_path)
    replacements = {path.name: path for path in clips_dir.iterdir()
                    if path.is_file() and path.suffix.lower() == ".mp3"}
    if not replacements:
        raise ValueError("В папке замен нет MP3-файлов.")
    unknown = sorted(replacements.keys() - template.tracks.keys())
    if unknown:
        raise ValueError("В заводском шаблоне нет дорожек: " + ", ".join(unknown))
    if output.exists():
        raise ValueError("Укажите новое имя выходного ZIP.")
    tracks = template.tracks.copy()
    for name, path in replacements.items():
        data = path.read_bytes()
        ensure_audio(name, audio_format(name, data), template.formats[name], model)
        tracks[name] = data

    digest = {name: md5(data).hexdigest() for name, data in tracks.items()}
    ini = "".join(f"[{name}]\nMD5={digest[name]}\n\n" for name in sorted(tracks, key=digest.get))
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as result:
            for name, data in tracks.items():
                result.writestr(name, data)
            result.writestr(MANIFEST, ini.encode("ascii"))
        final = read_pack(temporary)
        compare_with_template(final, template, model)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Заменено {len(replacements)} из {len(final.tracks)} дорожек. ZIP: {final.zip_bytes} байт.")
    compare_size(final, template)


def compare_size(pack, template):
    audio_size = sum(map(len, pack.tracks.values()))
    template_size = sum(map(len, template.tracks.values()))
    print(f"Шаблон: {template.zip_bytes} байт ZIP, {template_size} байт MP3.")
    print(f"Пакет: {pack.zip_bytes} байт ZIP, {audio_size} байт MP3.")
    if pack.zip_bytes > template.zip_bytes or audio_size > template_size:
        print("Пакет больше заводского шаблона. Проверьте ограничения памяти вашей модели.")


def main():
    parser = argparse.ArgumentParser(description="Инструменты для ZIP-озвучек MP3 + md5.ini")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="проверить структуру, MP3 и MD5")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--template", type=Path, help="сравнить с заводским ZIP нужной модели")
    inspect.add_argument("--model", choices=(V19,), help="учесть проверенный формат MP3 для v19")
    inspect.add_argument("--list", action="store_true", help="вывести имена и параметры MP3")
    extract = commands.add_parser("extract", help="сохранить дорожки отдельными MP3")
    extract.add_argument("archive", type=Path)
    extract.add_argument("output", type=Path)
    builder = commands.add_parser("build", help="собрать ZIP из шаблона и папки замен")
    builder.add_argument("template", type=Path)
    builder.add_argument("clips", type=Path)
    builder.add_argument("output", type=Path)
    builder.add_argument("--model", choices=(V19,), help="учесть проверенный формат MP3 для v19")
    options = parser.parse_args()
    try:
        if options.command == "build":
            build(options.template, options.clips, options.output, options.model)
            return 0
        pack = read_pack(options.archive)
        if options.command == "extract":
            options.output.mkdir()
            for name, data in pack.tracks.items():
                (options.output / name).write_bytes(data)
            print(f"Извлечено {len(pack.tracks)} MP3.")
            return 0
        print(f"ZIP: {pack.zip_bytes} байт. Проверено {len(pack.tracks)} MP3 и их MD5.")
        if options.template:
            template = read_pack(options.template)
            compare_with_template(pack, template, options.model)
            print("Имена и параметры MP3 прошли проверку по шаблону.")
            compare_size(pack, template)
        if options.list:
            for name, fmt in pack.formats.items():
                print(f"{name}\t{fmt.sample_rate} Гц\t{fmt.channels} кан.\tlayer {fmt.layer}")
        return 0
    except (ValueError, BadZipFile, ConfigError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
