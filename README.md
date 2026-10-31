# wytchnienie-dane

Dane dla aplikacji **Wytchnienie** – tylko dobre wieści z Polski i świata.

- `zrodla.json` – lista kanałów RSS (Polska i świat)
- `zbierz.py` – skrypt: pobiera RSS, Claude ocenia i tłumaczy, zapisuje wynik
- `wiadomosci.json` – gotowy plik, który pobiera aplikacja
- `stan/ocenione.json` – pamięć już ocenionych linków (żeby nie płacić dwa razy)
- `.github/workflows/zbieraj.yml` – uruchamia skrypt co godzinę

Klucz API jest w sekrecie repozytorium `ANTHROPIC_API_KEY`, nigdy w kodzie.

Adres pliku dla aplikacji:
`https://raw.githubusercontent.com/ediustest01-ops/wytchnienie-dane/main/wiadomosci.json`
