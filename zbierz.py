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
import urllib.parse
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
KATEGORIE = {"rescue", "med", "sci", "nature", "sky", "people"}

TERAZ = datetime.now(timezone.utc)

INSTRUKCJA = """Jesteś redaktorem polskiej aplikacji „Wytchnienie”, która pokazuje WYŁĄCZNIE dobre wiadomości.

Dostajesz listę wiadomości (JSON). Dla KAŻDEJ zwróć obiekt z polami:
- "id": to samo id co na wejściu
- "dobra": true tylko jeśli to naprawdę dobra wiadomość z jednej z kategorii poniżej; w razie wątpliwości false
- "kategoria": "rescue" (ktoś kogoś uratował, ratownicy, pomoc w nagłej sytuacji), "med" (sukces lekarzy, nowe leczenie, wyleczenie), "sci" (odkrycie naukowe, wynalazek), "nature" (przyroda, zwierzęta, ochrona środowiska), "sky" (atmosfera, klimat, pogoda, kosmos, czyste powietrze), "people" (dobrzy ludzie: życzliwość, bezinteresowna pomoc, wolontariat, udane zbiórki, sąsiedzka pomoc, sukcesy lokalnych społeczności, ludzie, którzy robią coś dobrego dla innych)
- "kraj": dwuliterowy kod kraju, którego dotyczy wiadomość (PL, US, GB, KE...), albo "ŚW", jeśli dotyczy całego świata. Daj "PL", jeśli wydarzyło się w Polsce ALBO bohaterami są Polacy lub polskie instytucje (np. polscy naukowcy, polski szpital, polska uczelnia).
- "wstepne": true, jeśli to wczesny etap badań (badania na zwierzętach lub w laboratorium, wczesne fazy badań klinicznych, „może pomóc”) – wtedy aplikacja pokaże ostrzeżenie
- "tytul": rzeczowy tytuł po polsku, maks. 110 znaków, który od razu mówi, CO SIĘ STAŁO (kto, co, gdzie). Jeśli oryginał jest dobry i rzeczowy – zostaw go prawie bez zmian (zagraniczny przetłumacz). Jeśli jest clickbaitowy – przepisz go. Clickbait to m.in.: ukrywanie sedna („Znalazł sposób na katar. Wiemy jaki”, „Zobacz, co zrobił”, „Tego się nie spodziewali”), pytania-zagadki, „nie uwierzysz”, „szok”, wykrzykniki, WIELKIE LITERY, dopiski typu [WIDEO], [ZDJĘCIA], [RANKING]. Usuń też nazwy działów i serwisów z początku tytułu („Wiadomości Studia Suwałki:”).
- "lead": 1–2 zdania po polsku, maks. 280 znaków, własnymi słowami (nie kopiuj opisu), rzeczowo: co się wydarzyło – bez clickbaitu i bez trzymania w niepewności

Odrzucaj (dobra=false):
- politykę, wojny i wszystko, co dzieje się w strefie konfliktu zbrojnego (nawet wzruszające czy z pozytywnym akcentem),
- przestępstwa, katastrofy, wypadki, w których byli zabici lub ciężko ranni (nawet jeśli „uratowano kilka osób”),
- próby samobójcze i osoby w kryzysie psychicznym (np. „mężczyzna na moście”, „w kryzysie emocjonalnym”) – NAWET jeśli zostały uratowane,
- znęcanie się nad ludźmi lub zwierzętami (np. „skatowany pies”), nawet z dobrym zakończeniem,
- pożary i katastrofy, w których głównym tematem są zniszczenia (np. „spłonęła stolarnia, uratowano dom obok”),
- wiadomości, które mogą niepokoić lub budzić kontrowersje (zagrożenia, eksperymenty budzące obawy – np. wirusy tworzone przez AI, broń, inżynieria genetyczna ludzi – choroby bez pozytywnego zakończenia),
- zbiorcze przeglądy i podsumowania tygodnia („co dobrego w tym tygodniu”, newslettery),
- reklamy i artykuły sponsorowane, plotki o celebrytach, wyniki sportowe, porady zakupowe, poradniki, artykuły-opinie,
- w kategorii "sci": odkrycia, które brzmią niepokojąco lub dziwnie (toksyny, „bakterie zombie”, choroby, zagrożenia, nawet jeśli to ciekawa nauka) – wybieraj tylko odkrycia budujące, które dają nadzieję lub zachwyt,
- rankingi, zestawienia, ciekawostki, turystykę i lifestyle (np. „najlepsze polskie wsie [RANKING]”) – to nie są wydarzenia, tylko treści poradnikowe.
Czytelnik ma po przeczytaniu poczuć ulgę i radość. W razie wątpliwości – false.

Przykłady, które SĄ dobre (dobra=true): strażacy/policjanci/sąsiedzi uratowali człowieka lub zwierzę i wszyscy przeżyli; lekarze przeprowadzili pierwszy taki zabieg i pacjent ma się dobrze; ktoś odnalazł się cały i zdrowy; polscy naukowcy coś odkryli lub opracowali; wróciły rzadkie zwierzęta, urodziły się młode w zoo lub rezerwacie; zbiórka/wolontariusze/sąsiedzi pomogli komuś (kategoria "people"); czyste powietrze, rekord energii z OZE.

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


RAPORT = {"czas": TERAZ.isoformat(), "zrodla": {}, "zajawki": {}}


def pobierz_kanaly(zrodla):
    nowe = []
    for region, lista in zrodla.items():
        for zr in lista:
            url = zr.get("url")
            if zr.get("zapytanie"):
                # Wyszukiwanie w Google News (wszystkie polskie media naraz), tylko z ostatnich 2 dni
                q = urllib.parse.quote(zr["zapytanie"] + " when:2d")
                url = f"https://news.google.com/rss/search?q={q}&hl=pl&gl=PL&ceid=PL:pl"
            kanal = feedparser.parse(url, agent="Wytchnienie/1.0 (+github.com/ediustest01-ops/wytchnienie-dane)")
            if kanal.bozo and not kanal.entries:
                print(f"[POMINIĘTO] {url}: {kanal.bozo_exception}")
                RAPORT["zrodla"][zr.get("zapytanie") or url] = "BŁĄD"
                continue
            RAPORT["zrodla"][zr.get("zapytanie") or url] = len(kanal.entries)
            print(f"[OK] {zr.get('zapytanie') or url}: {len(kanal.entries)} wpisów")
            for w in kanal.entries:
                link = w.get("link")
                if not link:
                    continue
                kiedy = data_wpisu(w)
                if TERAZ - kiedy > timedelta(days=DNI_W_KANALE):
                    continue
                tytul = oczysc(w.get("title"), 300)
                zrodlo = zr.get("nazwa", "")
                if zr.get("zapytanie"):
                    # Google podaje prawdziwe źródło osobno, a w tytule dokleja " - Nazwa serwisu"
                    zrodlo = (w.get("source") or {}).get("title") or "Google News"
                    if " - " in tytul:
                        tytul = tytul.rsplit(" - ", 1)[0]
                nowe.append({
                    "url": link,
                    "zrodlo": zrodlo,
                    "polskie": region == "polska",
                    "data": kiedy.isoformat(),
                    "tytul_oryg": tytul,
                    "opis": oczysc(w.get("summary"), 1500),
                })
    return nowe


def ocen_paczke(klient, paczka):
    wejscie = [{"id": i, "tytul": p["tytul_oryg"], "opis": p["opis"][:500]} for i, p in enumerate(paczka)]
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


INSTRUKCJA_DUPLIKATY = """Dostajesz listę wiadomości (JSON: id, tytul). Znajdź te, które opisują TO SAMO wydarzenie co inna wiadomość z listy (np. ta sama akcja ratunkowa opisana przez różne portale).
Zwróć WYŁĄCZNIE tablicę JSON z id wiadomości do usunięcia – z każdej grupy duplikatów zostaw jedną (tę o najniższym id), resztę wypisz. Jeśli nie ma duplikatów, zwróć [].
Różne wydarzenia na podobny temat (np. dwie różne akcje ratunkowe) to NIE duplikaty."""


def usun_duplikaty(klient, lista):
    """Prosi Claude o wskazanie wiadomości o tym samym wydarzeniu; zostawia po jednej z grupy."""
    if len(lista) < 2:
        return lista
    wejscie = [{"id": i, "tytul": w["tytul"]} for i, w in enumerate(lista)]
    try:
        odp = klient.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=INSTRUKCJA_DUPLIKATY,
            messages=[{"role": "user", "content": json.dumps(wejscie, ensure_ascii=False)}],
        )
        tekst = "".join(b.text for b in odp.content if b.type == "text")
        do_usuniecia = set(int(x) for x in json.loads(tekst[tekst.find("["):tekst.rfind("]") + 1]))
    except Exception as e:
        print(f"[BŁĄD] Szukanie duplikatów nie wyszło ({e}), zostawiam listę bez zmian")
        return lista
    print(f"Duplikatów usunięto: {len(do_usuniecia)}")
    return [w for i, w in enumerate(lista) if i not in do_usuniecia]


INSTRUKCJA_ZAJAWKI = """Piszesz zajawki do polskiej aplikacji „Wytchnienie” z dobrymi wiadomościami.

Aplikacja ma być odpoczynkiem od krzykliwych nagłówków. Piszesz spokojnie i rzeczowo, jak dobry dziennikarz, a nie jak portal walczący o kliknięcia.

Dostajesz listę wiadomości (JSON: id, tytul, tekst). Dla każdej zwróć:
- "tytul": rzeczowy tytuł (maks. 110 znaków), który od razu mówi, co się stało. Jeśli obecny tytuł jest dobry – zwróć go bez zmian. Jeśli jest clickbaitowy (ukrywa sedno, „wiemy jaki”, „zobacz”, pytanie-zagadka, wykrzykniki, WIELKIE LITERY, [WIDEO]/[ZDJĘCIA], nazwa działu na początku) – przepisz go.
- "lead": zajawka po polsku:
- 3–4 zdania, ok. 350–550 znaków,
- własnymi słowami – NIE przepisuj zdań z tekstu dosłownie,
- opieraj się WYŁĄCZNIE na podanym tekście: nie dopisuj faktów, liczb, imion ani miejsc, których w nim nie ma,
- jeśli tekst jest krótki lub ogólnikowy, napisz krócej (nawet 1–2 zdania) zamiast zmyślać,
- ciepły, rzeczowy ton; od razu mów, co się wydarzyło – bez trzymania w niepewności, bez wykrzykników, pytań retorycznych i zwrotów typu „zobacz”, „sprawdź”, „nie uwierzysz”.

Odpowiedz WYŁĄCZNIE tablicą JSON: [{"id": ..., "tytul": "...", "lead": "..."}]."""

MAKS_ZAJAWEK = 40           # ile zajawek najwyżej rozszerzać w jednym przebiegu


def pobierz_tekst_artykulu(url):
    """Zwraca (prawdziwy_url, tekst) – próbuje rozkodować link Google News i wyciągnąć treść artykułu."""
    prawdziwy = url
    try:
        if "news.google.com" in url:
            from googlenewsdecoder import gnewsdecoder
            r = gnewsdecoder(url, interval=1)
            if isinstance(r, dict) and r.get("status") and r.get("decoded_url"):
                prawdziwy = r["decoded_url"]
            else:
                return url, ""
        import trafilatura
        html = trafilatura.fetch_url(prawdziwy)
        tekst = trafilatura.extract(html, include_comments=False, include_tables=False) if html else ""
        return prawdziwy, re.sub(r"\s+", " ", tekst or "").strip()[:4000]
    except Exception as e:
        print(f"[ARTYKUŁ] {url}: {e}")
        return prawdziwy, ""


def rozszerz_zajawki(klient, wiesci, materialy):
    """Dla wieści bez dłuższej zajawki: pobiera artykuł (lub bierze opis z RSS) i prosi Claude o 3–4 zdania."""
    do_zrobienia = [w for w in wiesci if not w.get("rozszerzony")][:MAKS_ZAJAWEK]
    stat = {"probowano": len(do_zrobienia), "z_artykulu": 0, "z_rss": 0, "bez_tekstu": 0, "gotowe": 0}
    paczka = []
    for w in do_zrobienia:
        opis = materialy.get(w["url"], "")
        tekst = ""
        if len(opis) < 300:
            prawdziwy, tekst = pobierz_tekst_artykulu(w["url"])
            if prawdziwy != w["url"]:
                w["url_zrodla"] = prawdziwy
            if tekst:
                stat["z_artykulu"] += 1
        if not tekst and opis:
            tekst = opis
            stat["z_rss"] += 1
        w["rozszerzony"] = True          # nie próbujemy w kółko – nawet jeśli tekstu brak
        if len(tekst) < 150:
            stat["bez_tekstu"] += 1
            continue
        paczka.append((w, tekst))

    for start in range(0, len(paczka), 8):
        czesc = paczka[start:start + 8]
        wejscie = [{"id": i, "tytul": w["tytul"], "tekst": t} for i, (w, t) in enumerate(czesc)]
        try:
            odp = klient.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=INSTRUKCJA_ZAJAWKI,
                messages=[{"role": "user", "content": json.dumps(wejscie, ensure_ascii=False)}],
            )
            tekst = "".join(b.text for b in odp.content if b.type == "text")
            for o in json.loads(tekst[tekst.find("["):tekst.rfind("]") + 1]):
                w = czesc[int(o["id"])][0]
                nowy_tytul = str(o.get("tytul", "")).strip()
                if 10 <= len(nowy_tytul) <= 140:
                    w["tytul"] = nowy_tytul
                lead = str(o.get("lead", "")).strip()
                if len(lead) > len(w.get("lead", "")):
                    w["lead"] = lead[:650]
                    stat["gotowe"] += 1
        except Exception as e:
            print(f"[BŁĄD] Zajawki nie wyszły ({e})")
    RAPORT["zajawki"] = stat
    print(f"Zajawki: {stat}")


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
    materialy = {k["url"]: k["opis"] for k in kandydaci}
    klient = anthropic.Anthropic()
    if kandydaci:
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
    if dobre:
        lista = usun_duplikaty(klient, lista)
    rozszerz_zajawki(klient, lista, materialy)

    zapisz_json(PLIK_WYNIK, {"aktualizacja": TERAZ.isoformat(), "wiesci": lista})
    zapisz_json(PLIK_OCENIONE, ocenione)
    RAPORT["dobrych_nowych"] = len(dobre)
    RAPORT["razem"] = len(lista)
    zapisz_json(KATALOG / "stan" / "raport.json", RAPORT)
    print(f"Dobrych nowych: {len(dobre)}, razem w pliku: {len(lista)}")


if __name__ == "__main__":
    main()
