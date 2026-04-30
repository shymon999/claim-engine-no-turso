# Claim Engine v4

Aplikacja do automatycznego przypisywania claimów do handlerów.  
**Zero bazy danych. Zero Turso. Działa natychmiastowo.**

## Struktura

```
streamlit_app.py          ← główna aplikacja
requirements.txt          ← zależności
config/
  handlers.json           ← wszyscy handlerzy + kody Riskonnect
  rules_global.json       ← zasady dla CHC Global
  rules_nordic.json       ← zasady dla CHC Nordic (+ VIP customers)
  schenker.json           ← lista krajów scalonych ze Schenkerem
```

## Jak zaktualizować zasady

1. Wyślij nowy plik XLSX do administratora (tego samego co konfigurował aplikację)
2. Administrator wygeneruje zaktualizowane pliki JSON
3. Wgraj je na GitHub (zastąp stare pliki w folderze `config/`)
4. Streamlit Cloud automatycznie przeładuje aplikację

## Deployment na Streamlit Cloud

1. Wgraj to repozytorium na GitHub (publiczne lub prywatne)
2. Wejdź na [share.streamlit.io](https://share.streamlit.io)
3. Kliknij **New app** → wskaż repozytorium → plik `streamlit_app.py`
4. Kliknij **Deploy** — gotowe, bez żadnych Secrets ani zmiennych środowiskowych

## Uruchomienie lokalne

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Logika przypisania — CHC Global

```
1. Schenker (brak myślnika w numerze shipmentu + DoL ≤ 2025) → Claims Schenker Legacy
2. Special Customers (Abbott, Adidas, LEGO...) → konkretny handler
3. XPress division → Oliwia / Aleksandra
4. Low Value: kwota > 0 i < 200 EUR* → Team: CHC Low Value (bez handlera)
5. Fast Track: kwota 200–500 EUR* → Team: CHC Bucharest (bez handlera)
6. Reguły standardowe per kraj/dywizja
```
*Wyjątki od reguł kwotowych (idą do standardowych reguł):  
Delay, Errors & Omissions, Total Missing, General Average

## Logika przypisania — CHC Nordic

```
1. Schenker → Claims Schenker Legacy
2. VIP Customers (LEGO, IKEA, Bestseller...) → konkretny handler / team
3. Reguły standardowe (Low Value, Fast Track, Denmark/Norway/Sweden...)
```

## Attendance

Przełączniki w sidebarze. Gdy handler jest nieobecny:
- Sprawa trafia do kolejnego dostępnego handlera z tej samej reguły (load-balanced)
- Jeśli wszyscy z głównej listy nieobecni → brana jest osoba z kolumny "Alternative"
- Reset po zamknięciu przeglądarki (brak bazy = brak trwałości)
