"""
Wytchnienie – zbieracz dobrych wieści.

Uruchamiany przez GitHub Actions. Czyta kanały RSS z zrodla.json,
wysyła NOWE nagłówki do Claude (model Haiku), który:
  - ocenia, czy to naprawdę dobra wieść,
  - przypisuje kategorię (rescue / med / sci / nature / sky),
  - tłumaczy zagraniczne wieści na polski.
Wynik trafia do wiadomosci.json, który pobiera apka.

Klucz API bierze ze zmiennej środowiskowej ANTHROPIC_API_KEY
(sekret w ustawieniach repozytorium, nigdy w kodzie).
"""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser

KATALOG = Path(__file__).parent
PLIK_ZRODLA = KATALOG / "zrodla.json"
PLIK_WYNIK = KATALOG / "wiadomosci.json"
PLIK_OCENIONE = KATALOG / "stan" / "ocenione.json"

MODEL = "claude-haiku-5-5"
PACZKA = 20                 # ile wieści oceniać w jednym zapytaniu
DNI_W_KANALE = 7            # jak długo wieść zostaje w apce
MAKS_WIESCI = 150           # ile maksymalnie wieści w pliku
DNI_PAMIECI_OCEN = 21       # jak długo pamiętamy, co już ocenione
KATEGORIE = {"rescue", "med", "sci", "nature", "sky"}

TERAZ = datetime.now(timezone.utc)

INSTRUKCJA = """Jesteś redaktorem polskiej aplikacji „Wytchnienie”, która pokazuje WYŁĄCZNIE dobre wiadomości.

Dostajesz listę wiadomości (JSON). Dla KAŻDEJ zwróć obiekt z polami:
- "id": to samo id co na wejściu
- "dobra": true tylko jeśli to naprawdę dobra wiadomość z jednej z kategorii poniżej; w razie wątpliwości false
- "kategoria": "rescue" (ktoś kogoś uratował, ratownicy, pomoc w nagłej sytuacji), "med" (sukces lekarzy, nowe leczenie, wyleczenie), "sci" (odkrycie naukowe, wynalazek), "nature" (przyroda, zwierzęta, ochrona środowiska), "sky" (atmosfera, klimat, pogoda, kosmos, czyste powietrze)
- "kraj": dwuliterowy kod kraju, którego dotyczy wiadomość (PL, US, GB, KE...), albo "ŚW", jeśli dotyczy całego świata
- "wstepne": true, jeśli to wczesny etap badań (badania na zwierzętach lub w laboratorium, wczesne fazy badań klinicznych, „może pomóc”) – wtedy aplikacja pokaże ostrzeżenie
- "tytul": tytuł po polsku. Jeśli wiadomość jest po polsku – przepisz oryginalny tytuł bez zmian. Jeśli w innym języku – przetłumacz naturalnie, maks. 120 znaków.
- "lead": 1–2 zdania po polsku, maks. 280 znaków, własnymi słowami (nie kopiuj opisu), co się wydarzyło

Odrzucaj (dobra=false): politykę, wojny, przestępstwa (nawet z pozytywnym akcentem), katastrofy, w których „uratowano kilka osób” ale były ofiary śmiertelne, reklamy i artykuły sponsorowane, plotki o celebrytach, wyniki sportowe, porady zakupowe, artykuły-opinie.

Odpowiedz WYŁĄCZNIE tablicą JSON, bez żadnego innego tekstu."""


def wczytaj_json(sciezka, domyslnie):
    try:
        return json.loads(sciezka.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return domyslnie


def zapisz_json(sciezka, dane):
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(json.dumps(dane, ensure_ascii=False, indent=1), encoding="utf-8")


def oczysc(tekst, limit=500):
    tekst = re.sub(r"<[^>]+>", " ", tekst or "")
    tekst = re.sub(r"\s+", " ", tekst).strip()
    return tekst[:limit]


def data_wpisu(wpis):
    for pole in ("published_parsed", "updated_parsed"):
        t = wpis.get(pole)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return TERAZ


def pobierz_kanaly(zrodla):
    nowe = []
    for region, lista in zrodla.items():
        for zr in lista:
            kanal = feedparser.parse(zr["url"], agent="Wytchnienie/1.0 (+github.com/ediustest01-ops/wytchnienie-dane)")
            if kanal.bozo and not kanal.entries:
                print(f"[POMINIĘTO] {zr['url']}: {kanal.bozo_exception}")
                continue
            print(f"[OK] {zr['url']}: {len(kanal.entries)} wpisów")
            for w in kanal.entries:
                link = w.get("link")
                if not link:
                    continue
                kiedy = data_wpisu(w)
                if TERAZ - kiedy > timedelta(days=DNI_W_KANALE):
                    continue
                nowe.append({
                    "url": link,
                    "zrodlo": zr["nazwa"],
                    "polskie": region == "polska",
                    "data": kiedy.isoformat(),
                    "tytul_oryg": oczysc(w.get("title"), 300),
                    "opis": oczysc(w.get("summary")),
                })
    return nowe


def ocen_paczke(klient, paczka):
    wejscie = [{"id": i, "tytul": p["tytul_oryg"], "opis": p["opis"]} for i, p in enumerate(paczka)]
    odp = klient.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=INSTRUKCJA,
        messages=[{"role": "user", "content": json.dumps(wejscie, ensure_ascii=False)}],
    )
    tekst = "".join(b.text for b in odp.content if b.type == "text")
    poczatek, koniec = tekst.find("["), tekst.rfind("]")
    if poczatek == -1 or koniec == -1:
        print("[BŁĄD] Brak tablicy JSON w odpowiedzi, paczka pominięta")
        return []
    try:
        return json.loads(tekst[poczatek:koniec + 1])
    except json.JSONDecodeError as e:
        print(f"[BŁĄD] Niepoprawny JSON: {e}")
        return []


def main():
    zrodla = wczytaj_json(PLIK_ZRODLA, {})
    wynik = wczytaj_json(PLIK_WYNIK, {"wiesci": []})
    ocenione = wczytaj_json(PLIK_OCENIONE, {})

    # zapomnij stare oceny
    granica = (TERAZ - timedelta(days=DNI_PAMIECI_OCEN)).isoformat()
    ocenione = {u: t for u, t in ocenione.items() if t >= granica}

    kandydaci = [k for k in pobierz_kanaly(zrodla) if k["url"] not in ocenione]
    # usuń duplikaty w obrębie jednego przebiegu
    kandydaci = list({k["url"]: k for k in kandydaci}.values())
    print(f"Nowych do oceny: {len(kandydaci)}")

    dobre = []
    if kandydaci:
        klient = anthropic.Anthropic()
        for start in range(0, len(kandydaci), PACZKA):
            paczka = kandydaci[start:start + PACZKA]
            oceny = ocen_paczke(klient, paczka)
            for o in oceny:
                try:
                    k = paczka[int(o["id"])]
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
                ocenione[k["url"]] = TERAZ.isoformat()
                if o.get("dobra") is True and o.get("kategoria") in KATEGORIE and o.get("tytul"):
                    dobre.append({
                        "id": hashlib.sha1(k["url"].encode()).hexdigest()[:12],
                        "tytul": str(o["tytul"])[:160],
                        "lead": str(o.get("lead", ""))[:320],
                        "kategoria": o["kategoria"],
                        "kraj": str(o.get("kraj", "ŚW"))[:3].upper(),
                        "zrodlo": k["zrodlo"],
                        "url": k["url"],
                        "data": k["data"],
                        "przetlumaczone": not k["polskie"],
                        "wstepne": bool(o.get("wstepne")),
                    })

    # połącz z dotychczasowymi, usuń stare i duplikaty
    granica_kanalu = (TERAZ - timedelta(days=DNI_W_KANALE)).isoformat()
    wszystkie = {w["url"]: w for w in wynik.get("wiesci", []) if w["data"] >= granica_kanalu}
    for d in dobre:
        wszystkie[d["url"]] = d
    lista = sorted(wszystkie.values(), key=lambda w: w["data"], reverse=True)[:MAKS_WIESCI]

    zapisz_json(PLIK_WYNIK, {"aktualizacja": TERAZ.isoformat(), "wiesci": lista})
    zapisz_json(PLIK_OCENIONE, ocenione)
    print(f"Dobrych nowych: {len(dobre)}, razem w pliku: {len(lista)}")


if __name__ == "__main__":
    main()
