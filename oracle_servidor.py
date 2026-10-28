"""oracle_servidor.py — Crea el servidor gratis de Oracle Cloud reintentando hasta que haya hueco.

Oracle a menudo responde "Out of capacity" con los servidores ARM gratuitos (Always Free).
Este script lo intenta cada ~60 s desde tu PC, sin que tengas que hacer nada, y cuando lo
consigue te avisa por Telegram con la IP del servidor y la deja guardada para
`subir_primera_vez.bat`.

Uso (normalmente con doble clic en crear_servidor_oracle.bat):
    python oracle_servidor.py              # configura si hace falta y empieza a intentarlo
    python oracle_servidor.py configurar   # (re)introducir la clave API de Oracle
    python oracle_servidor.py comprobar    # solo comprueba la conexión y lo que se va a crear

Seguridad: NUNCA cambia el tipo de cuenta (no hace "Upgrade / Pay As You Go") y solo crea
un servidor dentro del límite gratuito (1 OCPU / 6 GB ARM). Si ya existe un servidor con el
mismo nombre, no crea otro.
"""
from __future__ import annotations

import html
import json
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
OCI_DIR = Path.home() / ".oci"
DEFAULT_SSH_KEY = OCI_DIR / "inversor_servidor.key"
SETTINGS_PATH = BASE_DIR / "deploy" / "oracle_servidor.json"
SERVIDOR_LOCAL = BASE_DIR / "deploy" / "servidor_local.bat"
LOG_PATH = BASE_DIR / "logs" / "oracle_reintento.log"

VCN_NAME = "inversorvcn"
DEFAULT_INTERVAL_S = 60
THROTTLE_BASE_S = 120
MAX_BACKOFF_S = 600
PROGRESS_EVERY_S = 3 * 3600
POLL_S = 15
BOOT_TIMEOUT_S = 15 * 60
GONE_STATES = {"TERMINATED", "TERMINATING"}

# Servidores pequeños AMD gratuitos (casi siempre hay hueco) mientras llega el ARM
MICRO_SHAPE = "VM.Standard.E2.1.Micro"
MICRO_BOT = "inversor-micro-bot"      # bot + web + Tailscale; dirige la mudanza al ARM
MICRO_IA = "inversor-micro-ia"        # FreeLLMAPI
HOST_BOT, HOST_IA, HOST_ARM = 10, 11, 20


class FatalError(Exception):
    """Error que no se arregla esperando (clave mal puesta, límite gratuito superado...)."""


class GaveUp(Exception):
    """Se alcanzó el número máximo de intentos."""


@dataclass
class Plan:
    compartment_id: str
    subnet_id: str
    image_id: str
    ssh_public_key: str
    shape: str = "VM.Standard.A1.Flex"
    ocpus: float = 1
    memory_gb: float = 6
    display_name: str = "inversor-ia"
    boot_gb: int = 50
    private_ip: str | None = None
    user_data: str | None = None
    tags: dict | None = None
    auto_migrate: bool = False        # el micro del bot se mudará solo a este servidor


@dataclass
class Result:
    instance_id: str
    ip: str
    attempts: int


@dataclass
class Decision:
    kind: str     # "capacity" | "throttle" | "transient" | "fatal"
    message: str


# ============================================================ errores ======
def classify_error(exc: BaseException) -> Decision:
    """¿Qué hacer con un error de Oracle? Esperar y reintentar, o parar y explicar por qué."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        code = str(getattr(exc, "code", "") or "")
        msg = str(getattr(exc, "message", "") or exc)
        low = (code + " " + msg).lower()
        if status == 429:
            return Decision("throttle", "Oracle pide ir más despacio (demasiadas peticiones).")
        if "capacity" in low:
            return Decision("capacity", "Oracle no tiene hueco ahora mismo (Out of capacity).")
        if status in (500, 502, 503, 504):
            return Decision("transient", f"Fallo temporal de Oracle ({status}).")
        if "limitexceeded" in low or "quotaexceeded" in low or (status == 400 and "limit" in low):
            return Decision("fatal", "Has llegado al límite gratis de servidores ARM de tu cuenta "
                                     "(¿ya existe otro servidor?). Revisa Compute → Instances en Oracle.")
        if status == 401:
            return Decision("fatal", "Oracle no acepta tu clave API (user, fingerprint o archivo .pem no "
                                     "coinciden). Vuelve a configurarla: python oracle_servidor.py configurar")
        if status == 404:
            return Decision("fatal", "Oracle no encuentra algo o no te deja usarlo (OCID incorrecto o región "
                                     f"equivocada). Detalle: {msg}")
        return Decision("fatal", f"Oracle rechazó la petición ({status} {code}): {msg}")
    name = type(exc).__name__
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(
            w in name for w in ("Connection", "Timeout", "RequestException", "ProtocolError")):
        return Decision("transient", f"Sin conexión con Oracle ({name}). ¿Internet caído?")
    return Decision("fatal", f"Error inesperado ({name}): {exc}")


# =========================================================== reintento =====
def _alive(instances) -> list:
    return [i for i in instances if str(getattr(i, "lifecycle_state", "")).upper() not in GONE_STATES]


def retry_create(api, plan: Plan, *, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic, notify: Callable[[str], None] = lambda m: None,
                 say: Callable[[str], None] = print, interval: float = DEFAULT_INTERVAL_S,
                 max_attempts: int | None = None, max_seconds: float | None = None,
                 progress_every_s: float = PROGRESS_EVERY_S,
                 poll_s: float = POLL_S, boot_timeout_s: float = BOOT_TIMEOUT_S) -> Result:
    """Intenta crear el servidor hasta conseguirlo. Devuelve su id, IP pública y nº de intentos."""
    existing = _alive(api.existing_instances(plan.display_name))
    attempts = 0
    if existing:
        instance_id = existing[0].id
        say(f"Ya existe un servidor llamado '{plan.display_name}': no creo otro, uso ese.")
    else:
        instance_id = None
        ads = api.availability_domains()
        start = last_progress = now()
        throttles = 0
        notify(f"🔁 Empiezo a intentar crear el servidor de Oracle ({plan.ocpus:g} OCPU / {plan.memory_gb:g} GB). "
               f"Te aviso cuando lo consiga.")
        while instance_id is None:
            wait = None
            if attempts:  # ¿lo ha creado otro mientras tanto (p. ej. el reintento de GitHub)?
                other = _alive(api.existing_instances(plan.display_name))
                if other:
                    instance_id = other[0].id
                    say("El servidor ya existe (lo ha creado el otro reintento): no creo otro.")
                    break
            for ad in ads:
                if max_attempts is not None and attempts >= max_attempts:
                    raise GaveUp(f"Sin hueco tras {attempts} intentos.")
                if max_seconds is not None and now() - start >= max_seconds:
                    raise GaveUp(f"Sin hueco tras {attempts} intentos ({max_seconds / 60:.0f} min).")
                attempts += 1
                try:
                    instance_id = api.launch(plan, ad)
                    break
                except Exception as e:  # noqa: BLE001 - se clasifica abajo
                    d = classify_error(e)
                    say(f"Intento {attempts}: {d.message}")
                    if d.kind == "fatal":
                        notify(f"⛔ No puedo crear el servidor de Oracle: {d.message}")
                        raise FatalError(d.message) from e
                    if d.kind == "throttle":
                        wait = min(THROTTLE_BASE_S * 2 ** throttles, MAX_BACKOFF_S) * random.uniform(1.0, 1.1)
                        wait = min(wait, MAX_BACKOFF_S)
                        throttles += 1
                        break  # no seguir insistiendo en otras AD
                    throttles = 0
                    wait = interval * random.uniform(0.9, 1.2)
            if instance_id is not None:
                break
            if now() - last_progress >= progress_every_s:
                last_progress = now()
                hours = (now() - start) / 3600
                notify(f"⏳ Sigo intentándolo: {attempts} intentos en {hours:.1f} h. Oracle aún sin hueco.")
            pause = wait if wait is not None else interval
            if max_seconds is not None:  # no esperar más allá del tiempo de la tanda
                pause = max(0.0, min(pause, max_seconds - (now() - start)))
            sleep(pause)
        notify(f"✅ ¡Servidor de Oracle creado tras {attempts} intentos! Arrancando...")
        say(f"¡Creado tras {attempts} intentos! Esperando a que arranque...")

    # Esperar a que arranque
    waited = 0.0
    while True:
        state = str(api.instance_state(instance_id)).upper()
        if state == "RUNNING":
            break
        if state in GONE_STATES:
            raise FatalError("El servidor se ha borrado mientras arrancaba. Vuelve a lanzar el script.")
        if waited >= boot_timeout_s:
            raise FatalError("El servidor tarda demasiado en arrancar. Míralo en la consola de Oracle.")
        sleep(poll_s)
        waited += poll_s

    ip = None
    for _ in range(4):
        ip = api.public_ip(instance_id)
        if ip:
            break
        sleep(poll_s)
    if not ip:
        notify("⚠️ El servidor está creado pero no tiene IP pública. Revisa la subred en Oracle.")
        raise FatalError("El servidor no tiene IP pública (¿subred privada?).")
    if plan.auto_migrate:
        notify(f"🖥️ ¡Servidor ARM de Oracle listo! IP pública: {ip}\nEl bot se va a mudar solo desde los servidores "
               f"pequeños en unos 10-20 minutos. Te aviso al terminar; no tienes que hacer nada.")
    else:
        notify(f"🖥️ Servidor de Oracle listo. IP pública: {ip}\nSiguiente paso: doble clic en subir_primera_vez.bat")
    return Result(instance_id, ip, attempts)


# ===================================================== config de Oracle ====
_REQUIRED = {
    "user": r"ocid1\.user\.\S+",
    "fingerprint": r"([0-9a-f]{2}:){15}[0-9a-f]{2}",
    "tenancy": r"ocid1\.tenancy\.\S+",
    "region": r"[a-z]+-[a-z]+-\d+",
}


def parse_oci_block(text: str) -> dict[str, str]:
    """Lee el bloque que Oracle muestra al crear la clave API (user, fingerprint, tenancy, region)."""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k.lower() in _REQUIRED:
            values[k.lower()] = v
    missing = [k for k in _REQUIRED if k not in values]
    if missing:
        raise ValueError("Falta en el bloque copiado: " + ", ".join(missing))
    bad = [k for k, rx in _REQUIRED.items() if not re.fullmatch(rx, values[k], re.I)]
    if bad:
        raise ValueError("Valores con formato extraño: " + ", ".join(bad) + ". Copia el bloque completo de Oracle.")
    return {k: values[k] for k in _REQUIRED}


def build_oci_config(values: dict[str, str], key_file: str) -> str:
    lines = ["[DEFAULT]"] + [f"{k}={values[k]}" for k in _REQUIRED] + [f"key_file={key_file}"]
    return "\n".join(lines) + "\n"


def install_api_key(block: str, key_path: str, oci_dir: Path = OCI_DIR) -> Path:
    """Copia la clave API privada a ~/.oci y escribe ~/.oci/config. Devuelve la ruta del config."""
    values = parse_oci_block(block)
    src = Path(str(key_path).strip().strip('"'))
    if not src.is_file():
        raise ValueError(f"No encuentro el archivo de clave: {src}")
    text = src.read_text(encoding="utf-8", errors="replace")
    if "PUBLIC KEY" in text and "PRIVATE KEY" not in text:
        raise ValueError("Ese archivo es la clave PÚBLICA. Necesito la privada (la que termina en .pem sin 'public').")
    if "-----BEGIN" not in text or "PRIVATE KEY" not in text:
        raise ValueError("Ese archivo no parece una clave privada .pem de Oracle.")
    oci_dir = Path(oci_dir)
    oci_dir.mkdir(parents=True, exist_ok=True)
    dest = oci_dir / "oci_api_key.pem"
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    cfg = oci_dir / "config"
    if cfg.exists():
        shutil.copyfile(cfg, oci_dir / "config.bak")
    cfg.write_text(build_oci_config(values, str(dest)), encoding="utf-8")
    return cfg


# ============================================================ clave SSH ====
def _load_private_key(data: bytes):
    from cryptography.hazmat.primitives import serialization

    try:
        return serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        return serialization.load_ssh_private_key(data, password=None)


def _public_openssh(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode()


def ensure_ssh_key(existing: str | None, default_path: Path = DEFAULT_SSH_KEY) -> str:
    """Clave pública SSH para entrar al servidor. Usa la clave privada indicada (p. ej. la .key que
    descargaste de Oracle) o, si no hay ninguna, crea un par nuevo en default_path."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if existing:
        path = Path(str(existing).strip().strip('"'))
        if not path.is_file():
            raise ValueError(f"No encuentro la clave SSH: {path}")
        try:
            return _public_openssh(_load_private_key(path.read_bytes()))
        except Exception as e:  # noqa: BLE001 - cualquier formato no válido
            raise ValueError(f"No puedo leer la clave SSH {path.name} (¿tiene contraseña o es la pública?): {e}") from e
    default_path = Path(default_path)
    if default_path.is_file():
        return _public_openssh(_load_private_key(default_path.read_bytes()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    default_path.parent.mkdir(parents=True, exist_ok=True)
    default_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                               serialization.NoEncryption()))
    pub = _public_openssh(key)
    Path(str(default_path) + ".pub").write_text(pub + "\n", encoding="utf-8")
    return pub


# ================================================ selección de recursos ====
def choose_image(images):
    """Ubuntu completa (no Minimal) más reciente. Oracle ya las devuelve de más nueva a más antigua."""
    for img in images or []:
        name = str(getattr(img, "display_name", "")).lower()
        if "minimal" not in name and "gpu" not in name:
            return img
    raise FatalError("No encuentro una imagen de Ubuntu 24.04 para ese tipo de servidor en tu región.")


def choose_subnet(vcns, subnets, vcn_name: str = VCN_NAME):
    vcn = next((v for v in vcns or [] if getattr(v, "display_name", "") == vcn_name), None)
    if vcn is None:
        raise FatalError(f"No encuentro la red '{vcn_name}' en Oracle (Networking → Virtual cloud networks).")
    public = [s for s in subnets or [] if getattr(s, "vcn_id", None) == vcn.id
              and not getattr(s, "prohibit_public_ip_on_vnic", True)]
    if not public:
        raise FatalError(f"No encuentro una subred pública en la red '{vcn_name}'.")
    public.sort(key=lambda s: "public" not in str(getattr(s, "display_name", "")).lower())
    return public[0]


# ===================================================== SDK de Oracle =======
class OciApi:
    """Envoltorio mínimo del SDK oficial `oci` (gratis). Inyectable para los tests."""

    def __init__(self, compute, network, identity, models, tenancy: str):
        self.compute, self.network, self.identity, self.models = compute, network, identity, models
        self.tenancy = tenancy

    @classmethod
    def from_config(cls, config: dict) -> "OciApi":
        import oci

        oci.config.validate_config(config)
        return cls(oci.core.ComputeClient(config), oci.core.VirtualNetworkClient(config),
                   oci.identity.IdentityClient(config), oci.core.models, config["tenancy"])

    def availability_domains(self) -> list[str]:
        return [ad.name for ad in self.identity.list_availability_domains(compartment_id=self.tenancy).data]

    def existing_instances(self, name: str) -> list:
        return list(self.compute.list_instances(compartment_id=self.tenancy, display_name=name).data)

    def find_subnet(self, vcn_name: str = VCN_NAME):
        vcns = self.network.list_vcns(compartment_id=self.tenancy).data
        subnets = []
        for v in vcns:
            if v.display_name == vcn_name:
                subnets += self.network.list_subnets(compartment_id=self.tenancy, vcn_id=v.id).data
        return choose_subnet(vcns, subnets, vcn_name)

    def find_image(self, shape: str):
        images = self.compute.list_images(compartment_id=self.tenancy, operating_system="Canonical Ubuntu",
                                          operating_system_version="24.04", shape=shape,
                                          sort_by="TIMECREATED", sort_order="DESC").data
        return choose_image(images)

    def launch(self, plan: Plan, ad: str) -> str:
        import base64

        m = self.models
        vnic = dict(subnet_id=plan.subnet_id, assign_public_ip=True)
        if plan.private_ip:
            vnic["private_ip"] = plan.private_ip
        metadata = {"ssh_authorized_keys": plan.ssh_public_key}
        if plan.user_data:
            metadata["user_data"] = base64.b64encode(plan.user_data.encode()).decode()
        kw = dict(
            availability_domain=ad,
            compartment_id=plan.compartment_id,
            display_name=plan.display_name,
            shape=plan.shape,
            source_details=m.InstanceSourceViaImageDetails(source_type="image", image_id=plan.image_id,
                                                           boot_volume_size_in_gbs=plan.boot_gb),
            create_vnic_details=m.CreateVnicDetails(**vnic),
            metadata=metadata,
        )
        if plan.shape.endswith(".Flex"):  # las formas fijas (E2.1.Micro) no admiten shape_config
            kw["shape_config"] = m.LaunchInstanceShapeConfigDetails(ocpus=plan.ocpus, memory_in_gbs=plan.memory_gb)
        if plan.tags:
            kw["freeform_tags"] = dict(plan.tags)
        return self.compute.launch_instance(m.LaunchInstanceDetails(**kw)).data.id

    def used_private_ips(self, subnet_id: str) -> set[str]:
        return {p.ip_address for p in self.network.list_private_ips(subnet_id=subnet_id).data}

    def instance_state(self, instance_id: str) -> str:
        return self.compute.get_instance(instance_id).data.lifecycle_state

    def public_ip(self, instance_id: str) -> str | None:
        atts = self.compute.list_vnic_attachments(compartment_id=self.tenancy, instance_id=instance_id).data
        for a in atts:
            if str(getattr(a, "lifecycle_state", "")).upper() == "ATTACHED":
                ip = self.network.get_vnic(a.vnic_id).data.public_ip
                if ip:
                    return ip
        return None


# ============================================================ salidas ======
def write_servidor_local(path: Path, ip: str, key: str) -> None:
    """Lo lee subir_primera_vez.bat para no volver a preguntar la IP y la clave."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"set IP={ip}\r\nset KEY={key}\r\n", encoding="utf-8")


def telegram_notifier_from_db(db, client_factory=None) -> Callable[[str], None]:
    """Aviso por Telegram con el bot configurado en la web (si lo está). Nunca lanza excepciones."""
    try:
        token, chat = db.get_setting("telegram_token"), db.get_setting("telegram_chat_id")
    except Exception:  # noqa: BLE001
        token = chat = None
    if not (token and chat):
        return lambda text: None
    if client_factory is None:
        from telegram_notifier import TelegramClient as client_factory
    holder = {}

    def notify(text: str) -> None:
        try:
            if "c" not in holder:
                holder["c"] = client_factory(token)
            holder["c"].send(chat, html.escape(text, quote=False))
        except Exception:  # noqa: BLE001
            pass
    return notify


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _say(text: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {text}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
    except OSError:
        pass


def _keep_awake() -> None:
    """Evita que Windows se suspenda mientras el script está intentándolo."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
        except Exception:  # noqa: BLE001
            pass


def _beep() -> None:
    if sys.platform == "win32":
        try:
            import winsound

            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                time.sleep(0.4)
        except Exception:  # noqa: BLE001
            pass


# ========================================================= interacción =====
def _read_block() -> str:
    print("\nPega aquí el bloque de configuración que te mostró Oracle (empieza por [DEFAULT])")
    print("y pulsa Enter dos veces al terminar:\n")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
        if line.strip().lower().startswith("key_file"):  # última línea del bloque de Oracle
            break
    return "\n".join(lines)


def clean_key_path(raw: str) -> str:
    """Ruta del archivo arrastrado, aunque venga con comillas o restos de texto delante."""
    raw = (raw or "").strip()
    found = re.findall(r'[A-Za-z]:\\[^"<>|*?\r\n]*?\.pem', raw, re.IGNORECASE)
    if found:
        return found[-1]
    return raw.strip('"').strip()


def cmd_configurar() -> None:
    while True:
        block = _read_block()
        print("\n✔ Bloque recibido.")
        key = ""
        while not key.strip():  # ignora los Enter de más tras pegar el bloque
            key = input("\nArrastra aquí el archivo de clave PRIVADA de la API (.pem) y pulsa Enter: ")
        try:
            cfg = install_api_key(block, clean_key_path(key))
            print(f"\n✔ Configuración guardada en {cfg}")
            return
        except ValueError as e:
            print(f"\n✖ {e}\nVamos a intentarlo otra vez.")


def _connect() -> OciApi:
    import oci

    try:
        config = oci.config.from_file()
        return OciApi.from_config(config)
    except Exception as e:  # noqa: BLE001
        raise FatalError(f"La configuración de Oracle no es válida: {e}") from e


def cmd_comprobar(api: OciApi | None = None) -> tuple[OciApi, dict]:
    api = api or _connect()
    _say("Conectando con Oracle...")
    try:
        ads = api.availability_domains()
        subnet = api.find_subnet()
        image = api.find_image(Plan.shape)
        existing = _alive(api.existing_instances(Plan.display_name))
    except FatalError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FatalError(classify_error(e).message) from e
    _say(f"✔ Conexión correcta. Zonas: {', '.join(a.split(':')[-1] for a in ads)}")
    _say(f"✔ Red: {VCN_NAME} → {subnet.display_name}")
    _say(f"✔ Imagen: {image.display_name}")
    if existing:
        _say(f"ℹ Ya existe un servidor '{Plan.display_name}' ({existing[0].lifecycle_state}).")
    return api, {"subnet_id": subnet.id, "image_id": image.id}


def cmd_crear() -> int:
    import oci  # noqa: F401 - comprueba que está instalado antes de empezar

    if not (OCI_DIR / "config").exists():
        cmd_configurar()
    try:
        api, res = cmd_comprobar()
    except FatalError as e:
        _say(f"✖ {e}")
        if "clave API" in str(e) or "configuración" in str(e):
            if input("\n¿Quieres volver a introducir la clave API de Oracle? (s/n): ").strip().lower().startswith("s"):
                cmd_configurar()
                api, res = cmd_comprobar()
            else:
                return 1
        else:
            return 1

    settings = _load_settings()
    ssh_key = settings.get("ssh_key")
    if not ssh_key:
        print("\nClave SSH para entrar al servidor:")
        print(" - Si descargaste una clave privada (.key) de Oracle al crear el servidor, arrástrala aquí.")
        print(" - Si no, pulsa Enter y creo una nueva (se guarda en tu carpeta .oci).")
        ssh_key = input("> ").strip().strip('"') or None
    try:
        pub = ensure_ssh_key(ssh_key, DEFAULT_SSH_KEY)
    except ValueError as e:
        _say(f"✖ {e}")
        return 1
    ssh_key = ssh_key or str(DEFAULT_SSH_KEY)
    settings["ssh_key"] = ssh_key
    _save_settings(settings)

    notify = lambda text: None  # noqa: E731
    try:
        import database

        notify = telegram_notifier_from_db(database.get_database())
    except Exception as e:  # noqa: BLE001
        _say(f"(Sin avisos por Telegram: {e})")

    try:
        plan = arm_plan(api, pub)
    except FatalError as e:
        _say(f"✖ {e}")
        return 1
    _keep_awake()
    print("\n" + "=" * 64)
    print(" Intentando crear el servidor cada ~60 s. Puede tardar horas o días.")
    print(" Deja esta ventana abierta (puedes minimizarla) y el PC encendido.")
    print(" Para pararlo: cierra la ventana o pulsa Ctrl+C.")
    print("=" * 64 + "\n")
    try:
        result = retry_create(api, plan, notify=notify, say=_say)
    except FatalError as e:
        _say(f"✖ {e}")
        return 1
    except KeyboardInterrupt:
        _say("Detenido por ti. Puedes volver a lanzarlo cuando quieras.")
        return 1
    write_servidor_local(SERVIDOR_LOCAL, result.ip, ssh_key)
    _beep()
    print("\n" + "=" * 64)
    print(f"  ✅ SERVIDOR CREADO   IP pública: {result.ip}")
    print("  Siguiente paso: cierra el bot y la web de este PC y haz")
    print("  doble clic en subir_primera_vez.bat (ya no te pedirá la IP).")
    print("=" * 64)
    return 0


# ============================================ micros + mudanza al ARM ======
def pick_ip(cidr: str, used: set[str], preferred_host: int) -> str:
    """Primera IP privada libre de la subred a partir de la posición preferida (p. ej. 10.0.0.10)."""
    import ipaddress

    net = ipaddress.ip_network(cidr, strict=False)
    for n in range(preferred_host, net.num_addresses - 1):
        ip = str(net.network_address + n)
        if ip not in used:
            return ip
    raise FatalError(f"No quedan IPs privadas libres en la subred {cidr}.")


def generate_link_key() -> tuple[str, str]:
    """Par de claves SSH para que el micro del bot pueda entrar al de IA y al ARM (mudanza automática)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                             serialization.NoEncryption()).decode()
    return priv, _public_openssh(key) + " enlace-inversor"


def micro_user_data(rol: str, red: dict, private_key: str | None = None) -> str:
    """Script de primer arranque (cloud-init): rol del servidor, IPs de los otros, clave de enlace y 2 GB de swap
    (los micros solo tienen 1 GB de memoria)."""
    env = "\n".join([f"ROL={rol}"] + [f"{k}={v}" for k, v in red.items()])
    lines = ["#!/bin/bash", "set -e", "mkdir -p /etc/inversor",
             f"cat > /etc/inversor/red.env <<'EOF'\n{env}\nEOF"]
    if private_key:
        lines += ["install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh",
                  f"cat > /home/ubuntu/.ssh/enlace <<'EOF'\n{private_key.strip()}\nEOF",
                  "chown ubuntu:ubuntu /home/ubuntu/.ssh/enlace", "chmod 600 /home/ubuntu/.ssh/enlace"]
    lines += ["if [ ! -f /swapfile ]; then",
              "  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile",
              "  echo '/swapfile none swap sw 0 0' >> /etc/fstab",
              "fi", ""]
    return "\n".join(lines)


def _link_tags(api) -> dict:
    bots = _alive(api.existing_instances(MICRO_BOT))
    return dict(getattr(bots[0], "freeform_tags", None) or {}) if bots else {}


def arm_plan(api, ssh_public_key: str) -> Plan:
    """Plan del servidor ARM. Si existen los micros, lleva la clave de enlace y la IP privada que el micro del bot
    espera, para que la mudanza sea automática."""
    subnet, image = api.find_subnet(), api.find_image(Plan.shape)
    plan = Plan(compartment_id=api.tenancy, subnet_id=subnet.id, image_id=image.id, ssh_public_key=ssh_public_key)
    tags = _link_tags(api)
    ip, pub = tags.get("arm_ip"), tags.get("enlace_pub")
    if ip and pub and ip not in api.used_private_ips(subnet.id):
        plan.private_ip = ip
        plan.ssh_public_key = f"{ssh_public_key.strip()}\n{pub.strip()}"
        plan.auto_migrate = True
    return plan


def create_micros(api, bundle: dict, notify: Callable[[str], None], max_attempts: int | None = None,
                  max_seconds: float | None = 50 * 60, sleep: Callable[[float], None] = time.sleep,
                  say: Callable[[str], None] = print) -> str:
    """Crea los dos servidores pequeños gratuitos (bot e IA). Devuelve "creado", "seguir" o "parar"."""
    try:
        if _alive(api.existing_instances(Plan.display_name)):
            notify("ℹ️ Ya tienes el servidor ARM: no hacen falta los servidores pequeños.")
            return "parar"
        subnet, image = api.find_subnet(), api.find_image(MICRO_SHAPE)
        used = set(api.used_private_ips(subnet.id))
        user_key = bundle["ssh_public_key"].strip()
        base = dict(compartment_id=api.tenancy, subnet_id=subnet.id, image_id=image.id, shape=MICRO_SHAPE)
        quiet = dict(sleep=sleep, notify=lambda m: None, say=say, max_attempts=max_attempts, max_seconds=max_seconds)

        bots = _alive(api.existing_instances(MICRO_BOT))
        if bots:
            tags = dict(getattr(bots[0], "freeform_tags", None) or {})
            bot = retry_create(api, Plan(**base, ssh_public_key=user_key, display_name=MICRO_BOT), **quiet)
        else:
            priv, pub = generate_link_key()
            bot_ip = pick_ip(subnet.cidr_block, used, HOST_BOT)
            ia_ip = pick_ip(subnet.cidr_block, used | {bot_ip}, HOST_IA)
            arm_ip = pick_ip(subnet.cidr_block, used | {bot_ip, ia_ip}, HOST_ARM)
            tags = {"enlace_pub": pub, "ia_ip": ia_ip, "arm_ip": arm_ip}
            bot = retry_create(api, Plan(**base, ssh_public_key=user_key, display_name=MICRO_BOT, private_ip=bot_ip,
                                         tags=tags, user_data=micro_user_data(
                                             "bot", {"IA_IP": ia_ip, "ARM_IP": arm_ip}, private_key=priv)), **quiet)
        ia_ip = tags.get("ia_ip")
        ia_plan = Plan(**base, display_name=MICRO_IA, ssh_public_key=f"{user_key}\n{tags.get('enlace_pub', '').strip()}",
                       private_ip=ia_ip if ia_ip and ia_ip not in used else None, user_data=micro_user_data("ia", {}))
        retry_create(api, ia_plan, **quiet)
    except GaveUp:
        say("Sin hueco para los servidores pequeños en esta tanda.")
        return "seguir"
    except FatalError as e:
        notify(f"⛔ No pude crear los servidores pequeños: {e}")
        return "parar"
    except Exception as e:  # noqa: BLE001
        d = classify_error(e)
        if d.kind == "fatal":
            notify(f"⛔ No pude crear los servidores pequeños: {d.message}")
            return "parar"
        return "seguir"
    notify(f"🖥️ ¡Servidores pequeños creados! IP del servidor del bot: {bot.ip}\n"
           f"Siguiente paso (en tu PC): cierra el bot y la web y haz doble clic en subir_primera_vez.bat; "
           f"cuando pida la IP, pon {bot.ip}.\nGitHub sigue buscando el servidor grande y, cuando lo consiga, "
           f"el bot se muda solo.")
    return "creado"


# ===================================================== modo GitHub Actions ==
# GitHub lo ejecuta cada hora en sus servidores (gratis en repositorios públicos), aunque tu PC esté
# apagado. Las claves van en UN secreto cifrado de GitHub llamado ORACLE_BUNDLE (nunca en el código).
BUNDLE_PATH = OCI_DIR / "github_secreto.txt"
_BUNDLE_KEYS = ("user", "fingerprint", "tenancy", "region", "key_pem", "ssh_public_key")


def build_bundle(config_path: Path, ssh_public_key: str, telegram_token: str | None, chat_id: str | None) -> str:
    """Todo lo que necesita GitHub, en un solo texto JSON para pegarlo como secreto."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    sec = parser["DEFAULT"]
    data = {k: sec.get(k, "") for k in ("user", "fingerprint", "tenancy", "region")}
    data["key_pem"] = Path(sec.get("key_file", "")).read_text(encoding="utf-8")
    data.update(ssh_public_key=ssh_public_key, telegram_token=telegram_token or "", telegram_chat_id=chat_id or "")
    return json.dumps(data, ensure_ascii=False)


def config_from_bundle(raw: str, workdir: Path) -> tuple[dict, str]:
    """Config del SDK de Oracle a partir del secreto (escribe la clave .pem en un archivo temporal)."""
    try:
        data = json.loads(raw or "")
    except ValueError as e:
        raise FatalError("Falta o está mal el secreto ORACLE_BUNDLE en GitHub") from e
    missing = [k for k in _BUNDLE_KEYS if not data.get(k)]
    if missing:
        raise FatalError("Al secreto ORACLE_BUNDLE le falta: " + ", ".join(missing))
    key_path = Path(workdir) / "oci_api_key.pem"
    key_path.write_text(data["key_pem"], encoding="utf-8")
    config = {k: data[k] for k in ("user", "fingerprint", "tenancy", "region")}
    config["key_file"] = str(key_path)
    return config, str(key_path)


def important_only(notify: Callable[[str], None]) -> Callable[[str], None]:
    """En GitHub cada tanda dura 1 h: solo se avisa del éxito o de un problema, no de cada intento."""
    def f(text: str) -> None:
        if text[:1] in ("✅", "🖥", "⛔", "⚠"):
            notify(text)
    return f


def telegram_sender(token: str | None, chat_id: str | None) -> Callable[[str], None]:
    """Aviso por Telegram sin depender del resto del proyecto (solo librería estándar)."""
    import urllib.request

    def send(text: str) -> None:
        if not token or not chat_id:
            print(f"(Telegram no configurado) {text}")
            return
        try:
            body = json.dumps({"chat_id": chat_id, "text": text}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:  # noqa: BLE001
            print(f"No se pudo avisar por Telegram: {e}")
    return send


def github_round(api, bundle: dict, notify: Callable[[str], None], max_attempts: int | None = None,
                 max_seconds: float | None = 55 * 60,
                 sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> str:
    """Una tanda de intentos. Devuelve "creado" (parar: ya hay servidor), "parar" (error que no se
    arregla esperando) o "seguir" (sin hueco todavía; la siguiente tanda lo vuelve a intentar)."""
    try:
        if _alive(api.existing_instances(Plan.display_name)):
            say("Ya existe el servidor: no hay nada que hacer.")
            return "creado"
        plan = arm_plan(api, bundle["ssh_public_key"])
        retry_create(api, plan, sleep=sleep, notify=important_only(notify), say=say, max_attempts=max_attempts,
                     max_seconds=max_seconds)
        return "creado"
    except GaveUp:
        say("Sin hueco en esta tanda. La próxima lo vuelve a intentar.")
        return "seguir"
    except FatalError as e:
        say(f"✖ {e}")
        return "parar"
    except Exception as e:  # noqa: BLE001
        d = classify_error(e)
        if d.kind == "fatal":
            notify(f"⛔ El reintento de Oracle en GitHub se ha parado: {d.message}")
            return "parar"
        return "seguir"


def cmd_github_run(task: str = "arm") -> int:
    """Lo ejecuta GitHub Actions. Escribe el resultado en GITHUB_OUTPUT para desactivarse al terminar."""
    import os
    import tempfile

    raw = os.environ.get("ORACLE_BUNDLE", "")
    data = json.loads(raw) if raw.strip().startswith("{") else {}
    notify = telegram_sender(data.get("telegram_token"), data.get("telegram_chat_id"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            config, _ = config_from_bundle(raw, Path(tmp))
            api = OciApi.from_config(config)
            secs = float(os.environ.get("MAX_MINUTES", "55")) * 60
            if task == "micros":
                result = create_micros(api, data, notify, max_seconds=secs)
            else:
                result = github_round(api, data, notify, max_seconds=secs)
        except FatalError as e:
            notify(f"⛔ El reintento de Oracle en GitHub se ha parado: {e}")
            result = "parar"
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"result={result}\n")
    print(f"Resultado: {result}")
    return 0


def cmd_github_export() -> int:
    """En tu PC: prepara el texto del secreto ORACLE_BUNDLE y lo abre en el Bloc de notas."""
    cfg = OCI_DIR / "config"
    if not cfg.exists():
        cmd_configurar()
    ssh_key = _load_settings().get("ssh_key")
    pub = ensure_ssh_key(ssh_key, DEFAULT_SSH_KEY)
    if not ssh_key:
        _save_settings({**_load_settings(), "ssh_key": str(DEFAULT_SSH_KEY)})
    token = chat = None
    try:
        import database

        db = database.get_database()
        token, chat = db.get_setting("telegram_token"), db.get_setting("telegram_chat_id")
    except Exception as e:  # noqa: BLE001
        print(f"(No pude leer Telegram de la web: {e})")
    if not (token and chat):
        print("⚠ Telegram no está configurado en la web: GitHub no podrá avisarte.")
    BUNDLE_PATH.write_text(build_bundle(cfg, pub, token, chat), encoding="utf-8")
    print(f"\n✔ Preparado: {BUNDLE_PATH}\nSe abre en el Bloc de notas: selecciónalo TODO (Ctrl+A), cópialo (Ctrl+C)")
    print("y pégalo en GitHub como secreto ORACLE_BUNDLE. Después BORRA ese archivo.")
    if sys.platform == "win32":
        import subprocess

        subprocess.Popen(["notepad.exe", str(BUNDLE_PATH)])
    return 0


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "crear").lower()
    try:
        if cmd == "github-run":
            return cmd_github_run()
        if cmd == "github-micros":
            return cmd_github_run("micros")
        if cmd == "github":
            return cmd_github_export()
        if cmd == "configurar":
            cmd_configurar()
            cmd_comprobar()
            return 0
        if cmd == "comprobar":
            cmd_comprobar()
            return 0
        return cmd_crear()
    except FatalError as e:
        _say(f"✖ {e}")
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
