"""
Scraper nevyřízených absencí z Bakaláře + automatické odesílání zpráv v Komens.
Určeno pro produkční běh na Ubuntu (cron / systemd timer), spouštěno 1x denně.

Loguje do:  ~/bakalari/logs/absence_scraper.log  (rotující, max 5 MB × 3 zálohy)
Pid soubor: ~/bakalari/absence_scraper.pid        (ochrana proti souběžnému běhu)
Cache:      ~/bakalari/absence_cache.json         (záznamy o odeslaných zprávách)
"""

import gc
import smtplib
import json
import functools
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
from datetime import date, timedelta, datetime
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
#from selenium.webdriver.firefox.options import Options

from selenium.webdriver.chrome.options import Options as ChromeOptions

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Cesty ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_DIR    = BASE_DIR / "logs"
LOG_FILE   = LOG_DIR / "absence_scraper.log"
PID_FILE   = BASE_DIR / "absence_scraper.pid"
CACHE_FILE    = BASE_DIR / "absence_cache.json"
VYJIMKY_FILE  = BASE_DIR / "vyjimky.json"
ENV_FILE   = BASE_DIR / ".env"
HISTORY_FILE = BASE_DIR / "absence_history.json"



LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("absence_scraper")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(fmt="%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()

# ── Konfigurace ────────────────────────────────────────────────────────────────
load_dotenv(ENV_FILE)

# Výchozí hodnoty pro třídy bez vlastního záznamu
VYCHOZI_UPOZORNENI_PO_DNECH = 7    # Za kolik dní od absence poslat první zprávu
VYCHOZI_FREKVENCE_DNI       = 14   # Za kolik dní znovu upozornit pokud stále neomluveno

# Konfigurace per-třída: klíč = označení třídy (přesně jak ho vrací Bakaláře)
# upozornit_po  ... za kolik dní od absence odeslat první zprávu
# opakovat_po   ... za kolik dní od posledního odeslání zprávu zopakovat
TRIDY_KONFIGURACE = {
    "1.A":  {"upozornit_po": 7,  "opakovat_po": 7},
    "1.B":  {"upozornit_po": 7,  "opakovat_po": 7},
    "2.":  {"upozornit_po": 7,  "opakovat_po": 7},
    "3.":  {"upozornit_po": 7,  "opakovat_po": 7},
    "4.":  {"upozornit_po": 7,  "opakovat_po": 7},
    "5.A":  {"upozornit_po": 7,  "opakovat_po": 7},
    "5.B":  {"upozornit_po": 7,  "opakovat_po": 7},
    "6.":  {"upozornit_po": 30,  "opakovat_po": 30},
    "7.":  {"upozornit_po": 30,  "opakovat_po": 30},
    "8.":  {"upozornit_po": 14,  "opakovat_po": 14},
    "9.":  {"upozornit_po": 30,  "opakovat_po": 30}
}

def trida_config(trida: str) -> dict:
    """Vrátí konfiguraci pro třídu, nebo výchozí hodnoty."""
    return TRIDY_KONFIGURACE.get(trida, {
        "upozornit_po": VYCHOZI_UPOZORNENI_PO_DNECH,
        "opakovat_po":  VYCHOZI_FREKVENCE_DNI,
    })

SELENIUM_TIMEOUT = 20
HEADLESS         = True    # False jen pro ruční ladění

BASE_URL    = "https://zsamsbohuslavice.bakalari.cz"
LOGIN_URL   = f"{BASE_URL}/next/login.aspx"
ABSENCE_URL = f"{BASE_URL}/next/omlouvani.aspx"
KOMENS_URL  = f"{BASE_URL}/next/komens_zprava.aspx"

USERNAME    = os.getenv("USERNAME2")
PASSWORD    = os.getenv("PASSWORD")
ODESILATEL  = os.getenv("ODESILATEL")
HESLO       = os.getenv("HESLO")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", ODESILATEL)

SMTP_SERVER = "smtp.webzdarma.cz"
SMTP_PORT   = 465

# ── Cache odeslaných zpráv ─────────────────────────────────────────────────────
def cache_load() -> dict:
    """Načte cache ze souboru. Struktura: { "trida|zak": "YYYY-MM-DD" }"""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Nepodařilo se načíst cache ({CACHE_FILE}): {e}")
        return {}

def history_append(trida: str, zak: str, dny: list) -> None:
    """Připíše záznam o odeslané zprávě do JSON historie."""
    try:
        zaznamy = []
        if HISTORY_FILE.exists():
            zaznamy = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        zaznamy.append({
            "datum": date.today().isoformat(),
            "cas":   datetime.now().strftime("%H:%M"),
            "trida": trida,
            "zak":   zak,
            "dny":   dny,
        })
        HISTORY_FILE.write_text(
            json.dumps(zaznamy, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"Nepodařilo se zapsat do historie: {e}")


def cache_save(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"Nepodařilo se uložit cache: {e}")

def cache_key(trida: str, zak: str) -> str:
    return f"{trida}|{zak}"

def cache_get_last_sent(cache: dict, trida: str, zak: str) -> Optional[date]:
    """Vrátí datum posledního odeslání zprávy, nebo None."""
    val = cache.get(cache_key(trida, zak))
    if val:
        try:
            return date.fromisoformat(val)
        except Exception:
            return None
    return None

def cache_set_sent(cache: dict, trida: str, zak: str) -> None:
    cache[cache_key(trida, zak)] = date.today().isoformat()


# ── Výjimky – žáci bez zpráv ───────────────────────────────────────────────────
def load_vyjimky() -> set:
    """Načte seznam jmen žáků kteří mají výjimku (zprávy se jim neposílají).
    Soubor vyjimky.json má formát: ["Novák Jan", "Svobodová Marie"]
    Pokud soubor neexistuje, vytvoří prázdný.
    """
    if not VYJIMKY_FILE.exists():
        VYJIMKY_FILE.write_text("[]", encoding="utf-8")
        log.info(f"Vytvořen prázdný soubor výjimek: {VYJIMKY_FILE}")
        return set()
    try:
        data = json.loads(VYJIMKY_FILE.read_text(encoding="utf-8"))
        vyjimky = set(data)
        if vyjimky:
            log.info(f"Načteno {len(vyjimky)} výjimek: {', '.join(sorted(vyjimky))}")
        return vyjimky
    except Exception as e:
        log.warning(f"Nepodařilo se načíst výjimky ({VYJIMKY_FILE}): {e}")
        return set()

# ── PID ochrana ────────────────────────────────────────────────────────────────
def check_pid_lock() -> None:
    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(old_pid, 0)
            log.error(f"Jiná instance již běží (PID {old_pid}). Ukončuji.")
            sys.exit(1)
        except ProcessLookupError:
            log.warning(f"Nalezen zastaralý PID soubor (PID {old_pid} neběží). Pokračuji.")
    PID_FILE.write_text(str(os.getpid()))
    log.debug(f"PID soubor vytvořen: {PID_FILE} (PID {os.getpid()})")

def remove_pid_lock() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ── Email notifikace chyb ──────────────────────────────────────────────────────
def send_error_email(subject: str, body: str) -> None:
    if not ODESILATEL or not HESLO or not ADMIN_EMAIL:
        log.warning("Email notifikace není nakonfigurována (chybí ODESILATEL/HESLO/ADMIN_EMAIL v .env).")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Absence scraper] {subject}"
        msg["From"]    = ODESILATEL
        msg["To"]      = ADMIN_EMAIL
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(
            f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:600px;">
  <div style="background:#922b21;padding:14px 20px;border-radius:6px 6px 0 0;">
    <span style="color:#fff;font-size:15px;font-weight:bold;">&#9888; Chyba – Absence scraper</span>
  </div>
  <div style="background:#fdfefe;padding:18px 20px;border:1px solid #e5e7e9;border-top:none;border-radius:0 0 6px 6px;">
    <p><strong>{subject}</strong></p>
    <pre style="background:#f2f3f4;padding:12px;border-radius:4px;font-size:12px;white-space:pre-wrap;">{body}</pre>
    <p style="font-size:11px;color:#999;margin-top:16px;">
      Čas: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>Log: {LOG_FILE}
    </p>
  </div>
</div>""", "html", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.login(ODESILATEL, HESLO)
            smtp.sendmail(ODESILATEL, ADMIN_EMAIL, msg.as_string())
        log.info(f"Chybový email odeslán na {ADMIN_EMAIL}.")
    except Exception as e:
        log.error(f"Nepodařilo se odeslat chybový email: {e}")

# ── Dekorátor pro zachytávání chyb ────────────────────────────────────────────
def report_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            log.error(f"Chyba ve funkci {func.__name__}: {e}\n{tb}")
            send_error_email(subject=f"Chyba ve funkci {func.__name__}(): {e}", body=tb)
            raise
    return wrapper

# ── Driver ─────────────────────────────────────────────────────────────────────

def create_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    if HEADLESS:
        options.add_argument("--headless=new")  # moderní headless režim
        options.add_argument("--window-size=1920,1080")  # ← přidej tohle
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    # Automaticky najde Chrome/Chromium binárku
    for cesta in [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium"
    ]:
        if os.path.exists(cesta):
            options.binary_location = cesta
            log.info(f"🌐 Chrome/Chromium nalezen: {cesta}")
            break
    else:
        log.warning("⚠️ Chrome/Chromium nenalezen na standardních cestách, zkouším výchozí...")

    return webdriver.Chrome(options=options)

# ── Přihlášení ─────────────────────────────────────────────────────────────────
@report_errors
def login(driver: webdriver.Firefox, wait: WebDriverWait) -> None:
    log.info("Přihlašování do Bakalářů...")
    driver.get(LOGIN_URL)
    wait.until(EC.presence_of_element_located((By.ID, "username")))
    driver.find_element(By.ID, "username").send_keys(USERNAME)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.ID, "loginButton").click()
    log.info("Přihlášení úspěšné.")
    time.sleep(2)

# ── Filtr absencí ──────────────────────────────────────────────────────────────
@report_errors
def set_filter_only_absence(driver: webdriver.Firefox, wait: WebDriverWait) -> None:
    log.debug("Nastavuji filtr absencí...")
    for rb_id in ("cphmain_TypeFilter_RB3_I_D", "cphmain_TypeFilter_RB4_I_D"):
        try:
            el = wait.until(EC.presence_of_element_located((By.ID, rb_id)))

            cls = el.get_attribute("class") or ""

            # pokud je ZAŠKRTNUTÝ → odškrtnout
            if "Checked" in cls:
                driver.execute_script("arguments[0].click();", el)

                # počkej, až se změní na unchecked
                wait.until(lambda d: "Unchecked" in el.get_attribute("class"))

                log.debug(f"Odškrtnuto: {rb_id}")
            else:
                log.debug(f"Už odškrtnuto: {rb_id}")

        except Exception as e:
            log.warning(f"Nepodařilo se odškrtnout {rb_id}: {e}")

    try:
        wait.until(EC.presence_of_element_located((By.ID, "cphmain_TypeFilterButton_I")))
        btn = driver.find_element(By.ID, "cphmain_TypeFilterButton_I")
        driver.execute_script("arguments[0].click();", btn)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
        time.sleep(10)
        log.info("Filtr potvrzen – zobrazeny pouze absence žáka.")
    except Exception as e:
        log.error(f"Tlačítko Filtrovat nenalezeno: {e}")

# ── Scraping absencí ───────────────────────────────────────────────────────────
@report_errors
def get_absence_data(driver: webdriver.Firefox, wait: WebDriverWait) -> list:
    log.info("Načítám stránku nevyřízených absencí...")
    driver.get(ABSENCE_URL)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.classCheckBox")))
    except Exception:
        log.warning("Stránka absencí se nenačetla nebo žádné nevyřízené absence neexistují.")
        return []

    time.sleep(1)
    set_filter_only_absence(driver, wait)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.classCheckBox")))
        time.sleep(0.5)
    except Exception:
        pass

    results = []
    for chb in driver.find_elements(By.CSS_SELECTOR, "input.classCheckBox"):
        try:
            h3 = chb.find_element(By.XPATH, "./ancestor::h3")
            trida = re.sub(r"^[Tt]řída\s*", "", h3.text.strip()).strip()
        except Exception:
            trida = "?"

        data_class = chb.get_attribute("data-class")
        for sch in driver.find_elements(By.CSS_SELECTOR,
                                        f"input.studentCheckBox[data-class='{data_class}']"):
            data_code = sch.get_attribute("data-code")
            try:
                title_div = sch.find_element(By.XPATH, "./ancestor::div[contains(@class,'title')]")
                zak = title_div.find_element(By.TAG_NAME, "span").text.strip()
            except Exception:
                zak = "?"
            try:
                parent = title_div.find_element(
                    By.XPATH, f"./ancestor::div[.//input[@data-code='{data_code}']][1]")
                dny = [h5.text.strip() for h5 in parent.find_elements(By.TAG_NAME, "h5")
                       if h5.text.strip()]
            except Exception:
                dny = []
            results.append({"trida": trida, "zak": zak, "dny": dny})

    log.info(f"Nalezeno {len(results)} žáků s nevyřízenou absencí.")
    return results

# ── Parsování datumu ───────────────────────────────────────────────────────────
MESICE_CZ = {
    "1.": 1, "2.": 2, "3.": 3, "4.": 4, "5.": 5, "6.": 6,
    "7.": 7, "8.": 8, "9.": 9, "10.": 10, "11.": 11, "12.": 12,
}

def parse_den(den_str: str) -> Optional[date]:
    """Parsuje 'středa 18. 2.' → date objekt."""
    try:
        casti = den_str.strip().split()
        cisla = [c for c in casti if re.match(r"\d+\.?", c)]
        if len(cisla) < 2:
            return None
        den_cislo   = int(cisla[0].rstrip("."))
        mesic_token = cisla[1].rstrip(".") + "."
        mesic_cislo = MESICE_CZ.get(mesic_token)
        if not mesic_cislo:
            return None
        rok = date.today().year
        d = date(rok, mesic_cislo, den_cislo)
        if d > date.today() + timedelta(days=30):
            d = date(rok - 1, mesic_cislo, den_cislo)
        return d
    except Exception:
        return None

# ── Filtrování + anti-spam ─────────────────────────────────────────────────────
@report_errors
def filtruj_k_odeslani(data: list, cache: dict, vyjimky: set) -> list:
    """Vrátí jen žáky kterým má smysl poslat zprávu:
    - nejstarší absence je starší než upozornit_po dní (dle třídy)
    - zpráva jim nebyla odeslána, NEBO od posledního odeslání uplynulo více než opakovat_po dní
    """
    dnes = date.today()
    vysledek = []

    for row in data:
        trida = row["trida"]
        zak   = row["zak"]
        cfg   = trida_config(trida)
        upozornit_po = cfg["upozornit_po"]
        opakovat_po  = cfg["opakovat_po"]

        # 0. Přeskoč žáky na seznamu výjimek
        if zak in vyjimky:
            log.debug(f"  {zak} ({trida}): výjimka – zprávy se neposílají.")
            continue

        # 1. Nejstarší absence musí být starší než upozornit_po
        datumy = [parse_den(d) for d in row["dny"]]
        datumy = [d for d in datumy if d is not None]
        if not datumy:
            log.debug(f"  {zak} ({trida}): nelze parsovat datum, přeskakuji.")
            continue

        nejstarsi = min(datumy)
        hranice_upozorneni = dnes - timedelta(days=upozornit_po)

        if nejstarsi > hranice_upozorneni:
            log.debug(f"  {zak} ({trida}): absence {nejstarsi} ještě čerstvá "
                      f"(upozornit po {upozornit_po} dnech). Přeskakuji.")
            continue

        # 2. Anti-spam: zkontroluj kdy byla zpráva naposledy odeslána
        posledni = cache_get_last_sent(cache, trida, zak)
        if posledni is not None:
            dalsi_odeslani = posledni + timedelta(days=opakovat_po)
            if dnes < dalsi_odeslani:
                log.debug(f"  {zak} ({trida}): zpráva odeslána {posledni}, "
                          f"další nejdříve {dalsi_odeslani}. Přeskakuji.")
                continue
            else:
                log.debug(f"  {zak} ({trida}): opakování – naposledy odesláno {posledni}, "
                          f"frekvence {opakovat_po} dní → odesílám znovu.")
        else:
            log.debug(f"  {zak} ({trida}): první upozornění.")

        vysledek.append(row)

    return vysledek

# ── Generování zprávy ──────────────────────────────────────────────────────────
def pocet_dni_absence(dny: list) -> int:
    """Vrátí počet dní od nejstarší absence do dnes."""
    datumy = [parse_den(d) for d in dny]
    datumy = [d for d in datumy if d is not None]
    if not datumy:
        return 0
    return (date.today() - min(datumy)).days


def zprava_predmet(zak: str, dni_absence: int) -> str:
    return f"Neomluvená absence stará {dni_absence} dní – {zak}"


def zprava_html(zak: str, trida: str, dny: list, dni_absence: int) -> str:
    dny_radky = "".join(
        f'<tr><td style="padding:8px 16px;border-bottom:1px solid #e8ecf0;font-size:14px;">'
        f'&#128197; {den}</td></tr>'
        for den in dny
    )
    return f"""<div style="font-family:Arial,sans-serif;color:#222;max-width:600px;">

  <div style="background:#1a5276;padding:18px 24px;border-radius:8px 8px 0 0;">
    <span style="color:#ffffff;font-size:16px;font-weight:bold;letter-spacing:0.3px;">
      ZŠ a MŠ Bohuslavice &nbsp;&#124;&nbsp; Upozornění na neomluvené absence
    </span>
  </div>

  <div style="background:#ffffff;padding:24px 24px 20px;border:1px solid #d5d8dc;border-top:none;border-radius:0 0 8px 8px;">

    <p style="margin-top:0;font-size:15px;">Vážení rodiče,</p>

    <p style="font-size:14px;line-height:1.6;">
      dovolujeme si Vás upozornit, že <strong>{zak}</strong> (třída <strong>{trida}</strong>)
      má v systému Bakaláři evidovány absence, které <strong>dosud nebyly omluveny</strong>.
      Nejstarší neomluvená absence pochází před
      <strong style="color:#c0392b;">{dni_absence} dny</strong>.
    </p>
    <p style="font-size:14px; font-weight:bold; color:#b30000; margin:0;">⚠️ NA TUTO ZPRÁVU PROSÍM NEODPOVÍDEJTE – JEDNÁ SE O AUTOMATICKY GENEROVANOU ZPRÁVU.</p>
    <p style="margin-bottom:8px;font-size:14px;font-weight:bold;">Neomluvené dny:</p>
    <table style="border-collapse:collapse;width:100%;background:#f8f9fa;border:1px solid #d5d8dc;border-radius:6px;margin-bottom:20px;">
      {dny_radky}
    </table>

    <div style="background:#fef9e7;border-left:4px solid #f39c12;padding:14px 16px;border-radius:0 6px 6px 0;margin-bottom:20px;">
      <p style="margin:0 0 6px 0;font-size:14px;font-weight:bold;color:#856404;">&#128276; Co je potřeba udělat:</p>
      <p style="margin:0;font-size:14px;line-height:1.6;color:#333;">
        Prosíme, kontaktujte <strong>třídního učitele</strong> a doručte mu omluvenku —
        osobně, nebo zadejte omluvu přímo v Bakalářích v sekci <em>Omluvenky</em>.<br>
        <strong>Tuto zprávu prosím neopovídejte</strong> — slouží pouze jako automatické upozornění
        a odpověď na ni třídní učitel neuvidí.
      </p>
    </div>

    <p style="font-size:14px;line-height:1.6;">
      V případě jakýchkoliv dotazů nebo nejasností nás neváhejte kontaktovat
      prostřednictvím nové zprávy v Bakalářích.
    </p>

    <p style="font-size:14px;margin-bottom:0;">
      Děkujeme za Vaši spolupráci a rychlé vyřešení.<br><br>
      <strong>ZŠ a MŠ Bohuslavice</strong>
    </p>

    <hr style="border:none;border-top:1px solid #e8ecf0;margin:20px 0 14px;">
    <p style="font-size:11px;color:#aaa;margin:0;line-height:1.5;">
      &#9881;&#65039; Tato zpráva byla vygenerována automaticky systémem <em>Bakalátor 3000</em> – správa absencí.
      <strong>Prosíme, neodpovídejte na tuto zprávu</strong> — odpověď třídní učitel neuvidí.
      V případě potíží kontaktujte Radka Matouška, ITC koordinátora
      (<a href="mailto:matousek.radek@zsamsbohuslavice.cz" style="color:#aaa;">matousek.radek@zsamsbohuslavice.cz</a>).
    </p>
  </div>
</div>"""

# ── Odeslání zprávy v Komens ───────────────────────────────────────────────────
@report_errors
def send_message(driver: webdriver.Firefox, wait: WebDriverWait,
                 zak: str, trida: str, dny: list) -> bool:
    log.info(f"  Otvírám formulář zprávy pro: {zak} ({trida})")
    driver.get(KOMENS_URL)
    time.sleep(2)

    # 1. Checkbox "jako ředitel" – klikneme jen pokud hodnota NENÍ "C" (zaškrtnutý)
    try:
        wait.until(EC.presence_of_element_located((By.ID, "cphmain_cbAsDirector_S_D")))
        # Skutečná hodnota je v skrytém <input id="cphmain_cbAsDirector_S">
        hidden_input = driver.find_element(By.ID, "cphmain_cbAsDirector_S")
        aktualni_hodnota = hidden_input.get_attribute("value")
        if aktualni_hodnota != "C":
            checkbox_wrapper = driver.find_element(By.ID, "cphmain_cbAsDirector_S_D")
            driver.execute_script("arguments[0].click();", checkbox_wrapper)
            time.sleep(0.5)
            # Ověř že se hodnota změnila na C
            nova_hodnota = hidden_input.get_attribute("value")
            if nova_hodnota == "C":
                log.debug("  Checkbox 'jako ředitel' zakliknut (C).")
            else:
                log.warning(f"  Checkbox po kliknutí má hodnotu '{nova_hodnota}', očekáváno 'C'.")
        else:
            log.debug("  Checkbox 'jako ředitel' již zaškrtnut (C), přeskakuji.")
    except Exception as e:
        log.warning(f"  Checkbox nenalezen: {e}")

    # 2. Příjemce
    prijmeni = zak.split()[0] if zak else zak
    jmeno = zak.split()[1] if len(zak.split()) > 1 else ""
    try:
        recipient = wait.until(EC.element_to_be_clickable((By.ID, "cphmain_cmbRecipient_I")))
        recipient.click()
        recipient.clear()
        recipient.send_keys(prijmeni)
        log.debug(f"  Zadáno příjemce: '{prijmeni}'")
    except Exception as e:
        log.error(f"  Pole příjemce nenalezeno: {e}")
        return False

    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dxeListBoxItem_NextBlueTheme")))
        time.sleep(0.3)
        items = [i for i in driver.find_elements(
            By.CSS_SELECTOR, ".dxeListBoxItem_NextBlueTheme") if i.text.strip()]
        if not items:
            log.error(f"  Rozbalovací seznam prázdný pro: {zak}")
            return False
        shoda = next(
            (item for item in items if prijmeni in item.text and jmeno in item.text),
            items[0])
        log.debug(f"  Vybírám příjemce: '{shoda.text.strip()}'")
        shoda.click()
        time.sleep(0.5)
    except Exception as e:
        log.error(f"  Nepodařilo se vybrat příjemce: {e}")
        return False

    # 3. Předmět
    try:
        predmet_pole = wait.until(EC.element_to_be_clickable((By.ID, "cphmain_tbTitle_I")))
        predmet_pole.click()
        predmet_pole.clear()
        predmet_pole.send_keys(zprava_predmet(zak, pocet_dni_absence(dny)))
        log.debug("  Předmět vyplněn.")
    except Exception as e:
        log.error(f"  Pole předmětu nenalezeno: {e}")
        return False

    # 4. Tělo zprávy (uvnitř iframe)
    try:
        iframe = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "iframe.dxheIFrame_NextBlueTheme, iframe[id*='MessageEditor']")))
        driver.switch_to.frame(iframe)
        body = wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        driver.execute_script("arguments[0].innerHTML = arguments[1];",
                              body, zprava_html(zak, trida, dny, pocet_dni_absence(dny)))
        driver.switch_to.default_content()
        log.debug("  Tělo zprávy vyplněno.")
    except Exception as e:
        driver.switch_to.default_content()
        log.error(f"  Nepodařilo se vyplnit tělo zprávy: {e}")
        return False

    # 5. Odeslat

    #return True
    if not(HEADLESS):
        return True

    try:
        send_btn = wait.until(EC.presence_of_element_located((By.ID, "button_poslat")))
        driver.execute_script("arguments[0].click();", send_btn)
        time.sleep(2)
        log.info(f"  ✓ Zpráva odeslána: {zak} ({trida})")
        return True
    except Exception as e:
        log.error(f"  Nepodařilo se odeslat zprávu: {e}")
        return False

# ── Výpis tabulky do logu ──────────────────────────────────────────────────────
def log_table(data: list) -> None:
    if not data:
        log.info("Žádné nevyřízené absence nenalezeny.")
        return
    log.info("─" * 70)
    log.info(f"{'TŘÍDA':<8} {'ŽÁK / ŽÁKYNĚ':<30} {'DNY S ABSENCÍ'}")
    log.info("─" * 70)
    for row in data:
        log.info(f"{row['trida']:<8} {row['zak']:<30} {', '.join(row['dny']) or '—'}")
    log.info("─" * 70)
    log.info(f"Celkem žáků: {len(data)}, celkem absencí: {sum(len(r['dny']) for r in data)}")

# ── Kontrola konfigurace ───────────────────────────────────────────────────────
def check_config() -> None:
    chyby = []
    if not USERNAME:
        chyby.append("USERNAME2 není nastaven v .env")
    if not PASSWORD:
        chyby.append("PASSWORD není nastaven v .env")
    if not ODESILATEL:
        log.warning("ODESILATEL není nastaven – chybové emaily nebudou odesílány.")
    if not HESLO:
        log.warning("HESLO není nastaven – chybové emaily nebudou odesílány.")
    if chyby:
        for c in chyby:
            log.critical(f"Chyba konfigurace: {c}")
        sys.exit(1)

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 70)
    log.info("SPUŠTĚNÍ – automatické upozornění na neomluvené absence")
    log.info("=" * 70)

    check_config()
    #check_pid_lock()

    driver = None
    exit_code = 0

    try:
        cache = cache_load()
        log.debug(f"Cache načtena: {len(cache)} záznamů.")
        vyjimky = load_vyjimky()

        driver = create_driver()
        wait = WebDriverWait(driver, SELENIUM_TIMEOUT)

        login(driver, wait)
        data = get_absence_data(driver, wait)
        log_table(data)

        k_odeslani = filtruj_k_odeslani(data, cache, vyjimky)

        if k_odeslani:
            log.info(f"Žáci k odeslání zprávy ({len(k_odeslani)}):")
            for row in k_odeslani:
                cfg = trida_config(row["trida"])
                log.info(f"  {row['trida']:<8} {row['zak']:<30} "
                         f"(upozornit po {cfg['upozornit_po']}d, opakovat po {cfg['opakovat_po']}d)")

            uspech = 0
            neuspech = 0
            for row in k_odeslani:
                ok = send_message(driver, wait, row["zak"], row["trida"], row["dny"])
                if ok:
                    cache_set_sent(cache, row["trida"], row["zak"])
                    cache_save(cache)   # ukládáme po každém úspěšném odeslání
                    history_append(row["trida"], row["zak"], row["dny"])
                    uspech += 1
                else:
                    neuspech += 1
                time.sleep(1)

            log.info(f"Výsledek: odesláno {uspech}/{len(k_odeslani)} zpráv"
                     + (f", {neuspech} selhalo." if neuspech else "."))
            if neuspech:
                exit_code = 2
        else:
            log.info("Žádní žáci nesplňují podmínky pro odeslání zprávy.")

    except KeyboardInterrupt:
        log.warning("Přerušeno uživatelem (Ctrl+C).")
        exit_code = 130
    except Exception:
        tb = traceback.format_exc()
        log.critical(f"Neočekávaná chyba:\n{tb}")
        send_error_email(subject="Neočekávaná chyba v main()", body=tb)
        exit_code = 1
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        remove_pid_lock()
        gc.collect()
        log.info(f"UKONČENÍ (exit code {exit_code})")
        log.info("=" * 70)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()