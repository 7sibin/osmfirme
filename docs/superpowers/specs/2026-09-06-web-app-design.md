# Web aplikacija nad `osm_businesses` — dizajn

Datum: 2026-09-06
Status: predlog za odobrenje

## 1. Cilj

Web interfejs nad postojećim CLI alatom: korisnik u browseru izabere mesto
(pretragom po imenu ili crtanjem po mapi), pregleda i filtrira pronađene firme
u tabeli, pa preuzme rezultat kao Excel (`.xlsx`) fajl.

Ne-ciljevi: Google Sheets integracija, javni multi-user hosting, login,
pinovi rezultata na mapi.

## 2. Odluke

| Pitanje | Odluka |
|---|---|
| Export | `.xlsx` (openpyxl). Bez CSV dugmeta — postojeći CLI i dalje piše CSV. |
| Izbor oblasti | Pretraga po imenu (Nominatim) + crtanje pravougaonika/kruga na mapi |
| Poligon (`--poly`) | Van obima ove faze |
| Hosting | Lokalno (`uvicorn`), pisano tako da se može deployovati |
| Čekanje | Posao u pozadini + prikaz statusa, sa keširanjem rezultata |
| Kategorije | Dva nivoa: grube grupe pre upita, konkretne vrednosti posle upita |
| Jezik UI | Srpski (latinica). Kod, komentari, nazivi promenljivih — engleski. |
| Frontend | Vanilla HTML/CSS/JS + Leaflet sa CDN-a. Bez npm-a i build koraka. |

## 3. Arhitektura

Jedan Python proces. FastAPI servira i JSON API i statički frontend.

```
browser (index.html + app.js + Leaflet)
    |  fetch /api/...
    v
FastAPI (webapp/main.py)
    |
    +-- webapp/jobs.py    registar poslova, asyncio taskovi
    +-- webapp/cache.py   kes rezultata na disku
    +-- webapp/export.py  Row[] -> .xlsx
    |
    +-- osm_businesses.py / geo.py   (postojeci kod, nepromenjen)
            |
            v
      Nominatim + Overpass
```

### Struktura fajlova

```
E:\Scraper\
  osm_businesses.py, geo.py          # nepromenjeni; 73 testa i dalje prolaze
  test_parse.py, test_geo.py         # nepromenjeni
  webapp/
    __init__.py
    main.py          # FastAPI app, rute, montiranje statike
    models.py        # Pydantic modeli zahteva i odgovora
    jobs.py          # JobRegistry, Job, faze, otkazivanje
    runner.py        # posao: resolve -> query -> parse -> cache
    cache.py         # kes rezultata na disku, kljuc i zapis
    results.py       # filtriranje, facets, sortiranje, stranicenje
    export.py        # workbook iz Row objekata
    nominatim.py     # /lookup za geometriju granice (search ide preko geo.py)
    static/
      index.html     # ovde ide Claude Design izlaz
      app.js
      style.css
  test_webapp_api.py      # rute, mockovana mreza
  test_webapp_jobs.py     # masina stanja posla, otkazivanje, kes
  test_webapp_results.py  # filtriranje, facets, stranicenje
  test_webapp_export.py   # sadrzaj .xlsx
  requirements.txt       # + fastapi, uvicorn[standard], openpyxl, httpx
```

Postojeći moduli se ne diraju. `webapp` ih uvozi. Cepanje
`osm_businesses.py` na manje module nije deo ovog posla.

## 4. Model oblasti

Frontend šalje jedan od tri oblika; backend ga prevodi u postojeći `AreaSpec`.

```json
{"kind": "area",   "area_id": 3611538321, "label": "Nis, Grad Nis, ..."}
{"kind": "bbox",   "bbox": [43.28, 21.85, 43.36, 21.96]}
{"kind": "circle", "center": [43.32, 21.90], "radius_m": 3000}
```

`bbox` je Overpass redosled: jug, zapad, sever, istok. Prevod koristi
`area_spec_from_area_id`, `area_spec_from_bbox`, `area_spec_from_center`.

Validacija u Pydantic modelima: `area_id` pozitivan; `bbox` sa `s < n`,
`w < e` i vrednostima u opsegu; `radius_m` između 50 i 50000. Neispravan
zahtev je `422`, ne pad.

## 5. API

| Ruta | Šta radi |
|---|---|
| `GET /` | `static/index.html` |
| `GET /api/places?q=Nis&country=RS` | Kandidati iz Nominatima, preko postojećeg `NominatimClient.search`. Vraća `AreaCandidate.to_dict()` plus `area_id`. Prazna lista je `200` sa `[]`, ne greška. |
| `GET /api/places/{osm_type}/{osm_id}/geometry` | GeoJSON granica oblasti i njen bbox, preko Nominatim `/lookup?osm_ids=R<id>&polygon_geojson=1`. Za crtanje na mapi i pomeranje pogleda. Keširano na disku. |
| `POST /api/jobs` | Telo: `{area, categories}`. Vraća `{job_id, cached}`. Ako keš već ima rezultat, posao se odmah kreira u stanju `done`. |
| `GET /api/jobs/{id}` | Status posla (vidi ispod). |
| `DELETE /api/jobs/{id}` | Otkazivanje. |
| `GET /api/jobs/{id}/results` | Filtrirani redovi, stranično. Parametri: `categories` (fine vrednosti, ponovljivo), `require_contact`, `q` (tekst po imenu/ulici), `page`, `page_size`, `sort`, `order`. Vraća `{rows, total, page, page_size, facets}`. |
| `GET /api/jobs/{id}/export.xlsx` | Isti filteri kao `/results`, bez straničenja. Vraća fajl. |

`GET /api/jobs/{id}` vraća:

```json
{
  "job_id": "...",
  "status": "running",
  "phase": "querying",
  "message": "Saljem upit Overpass API-ju...",
  "started_at": "2026-09-06T21:15:02Z",
  "elapsed_s": 47.2,
  "elements_found": null,
  "rows": null,
  "error": null
}
```

`status`: `pending` | `running` | `done` | `error` | `cancelled`.
`phase`: `resolving` | `querying` | `parsing` | `finished`.

`facets` u `/results` su spisak konkretnih kategorija koje su stvarno
pronađene, sa brojem pojavljivanja — to puni checkbox listu u UI-ju.
Facets se računaju pre primene filtera po kategoriji, a posle ostalih
filtera, da bi ostali stabilni dok korisnik čekira i odčekira.

## 6. Tok posla

1. `POST /api/jobs` — izračuna se ključ keša. Pogodak → posao odmah `done`.
2. Promašaj → `JobRegistry` kreira posao i pušta `asyncio.create_task`.
3. Faza `resolving`: samo za `kind: "area"` ako nedostaje `area_id`;
   inače se preskače.
4. Faza `querying`: `build_query` pa `OverpassClient.fetch`.
5. Faza `parsing`: `parse_elements` pa `sort_rows`.
6. Rezultat se piše u keš, posao prelazi u `done`.

`OverpassClient` i `NominatimClient` koriste `requests`, dakle blokiraju.
Pozivaju se kroz `asyncio.to_thread` da event loop ostane slobodan.

**Otkazivanje je kooperativno.** Posao nosi `threading.Event`; proverava se
između faza i u retry petlji. HTTP zahtev koji već visi ne može da se prekine
usred leta — `DELETE` odmah označava posao `cancelled` i frontend prestaje da
ga prati, ali nit može da doživi svoj timeout u pozadini. To je prihvatljivo i
biće tako dokumentovano; jedina alternativa je zaseban proces, što ne vredi.

Poslovi žive u memoriji i ne preživljavaju restart servera. Rezultati
preživljavaju, jer su u kešu.

## 7. Keš

Ključ: `sha256` od kanonskog JSON-a `{area_spec, sorted(categories)}`, prvih
16 heks znakova. Zapis u `~/.cache/osm_businesses/web/<key>.json`:

```json
{"version": 1, "created_at": "...", "area_label": "...", "categories": ["shop"],
 "elements_found": 4213, "rows": []}
```

Redovi se serijalizuju iz `Row.as_output_dict()` plus `category_key`, i
učitavaju natrag u `Row`. Zapis stariji od 30 dana se ignoriše i posao se
izvršava iznova. `version` polje postoji da bi promena šeme mogla da
invalidira stare zapise umesto da pukne pri učitavanju.

Keš oblasti iz `geo.AreaCache` ostaje kakav jeste i deli se sa CLI-jem.

## 8. Frontend

Jedna stranica, tri sekcije jedna ispod druge; svaka se otključava kad
prethodna da rezultat.

**Sekcija 1 — Oblast.** Polje za pretragu (debounce 400 ms, minimum 2 znaka)
gađa `/api/places`. Rezultati kao lista koju biraš klikom; izabrana oblast
crta svoju granicu na Leaflet mapi i mapa se pomera na nju. Pored pretrage,
dva dugmeta na mapi (Leaflet-Geoman ili Leaflet.draw sa CDN-a) crtaju
pravougaonik ili krug; nacrtani oblik zamenjuje izbor iz pretrage i obrnuto —
u svakom trenutku postoji tačno jedna aktivna oblast, i ona je vidljiva na mapi.

**Sekcija 2 — Kategorije i pokretanje.** Šest checkboxova za grube grupe
(prodavnice, usluge/objekti, kancelarije, zanati, turizam, zdravstvo), svi
čekirani po defaultu. Dugme "Pretraži". Dok posao radi: traka statusa sa
fazom, proteklim vremenom i dugmetom "Otkaži". Poll na `/api/jobs/{id}`
svake sekunde.

**Sekcija 3 — Rezultati.** Tabela sa kolonama iz `COLUMNS`, srpski nazivi
zaglavlja. Iznad tabele: pretraga po tekstu, checkbox "samo sa kontaktom",
i lista konkretnih kategorija iz `facets` sa brojevima. Straničenje po 50.
Klik na zaglavlje sortira. Dugme "Preuzmi Excel" vodi na `export.xlsx` sa
istim filterima u query stringu.

Bez frameworka i bez build koraka. Leaflet i plugin za crtanje sa CDN-a,
ostalo je jedan `app.js`. Vizuelni sloj (`index.html` + `style.css`) dolazi
iz Claude Designa; `app.js` se za elemente kači preko `data-` atributa, tako
da redizajn ne lomi logiku.

## 9. Export

`webapp/export.py` gradi `openpyxl` workbook:

- Zaglavlje na srpskom, podebljano, zamrznut prvi red, uključen autofilter.
- Širine kolona po dužini sadržaja, ograničene na razuman maksimum.
- `website` i `email` kao klikabilni linkovi.
- `phone` kao tekst, da Excel ne pojede vodeći `+`.
- `lat`/`lon` kao brojevi sa 7 decimala.
- Poseban list "Info": naziv oblasti, datum, primenjeni filteri, broj redova,
  i obavezna ODbL atribucija (© OpenStreetMap contributors).

Ime fajla: `firme-<slug oblasti>-<datum>.xlsx`, poslato kroz
`Content-Disposition`. Generiše se u memoriji (`BytesIO`), bez privremenih
fajlova.

## 10. Greške

| Situacija | Ponašanje |
|---|---|
| Nominatim ne nađe ništa | `200` sa praznom listom; UI kaže "Nema rezultata, probaj drugačiji naziv ili nacrtaj oblast na mapi." |
| Nominatim/mreža pukne | `503` sa porukom; UI nudi ponovni pokušaj. |
| Svi Overpass endpointi otkažu (`OverpassError`) | Posao → `error`, poruka razlikuje preopterećenje ("Overpass je trenutno zauzet, pokušaj za koji minut") od ostalog. |
| Overpass vrati 0 elemenata | Posao → `done` sa 0 redova; UI to kaže eksplicitno, ne prikazuje praznu tabelu bez objašnjenja. |
| Nepoznat `job_id` | `404`. |
| Neispravna oblast | `422` iz Pydantica, sa čitljivom porukom. |

Nijedan traceback ne ide u browser. Serverski log dobija pun izuzetak,
korisnik dobija rečenicu.

## 11. Testovi

Postojeća 73 testa moraju da nastave da prolaze nepromenjena. Novi testovi,
svi bez mreže:

- `test_webapp_export.py` — workbook se čita natrag preko `openpyxl`:
  zaglavlje, broj redova, telefon ostao tekst, linkovi postavljeni, "Info" list
  postoji i sadrži atribuciju.
- `test_webapp_jobs.py` — mašina stanja: `pending → running → done`;
  greška vodi u `error` sa porukom; otkazivanje vodi u `cancelled`; pogodak
  u kešu preskače izvršavanje; zapis starijeg datuma se ignoriše; ključ keša
  je isti za iste ulaze bez obzira na redosled kategorija.
- `test_webapp_api.py` — preko `TestClient`, sa mockovanim Nominatimom i
  Overpassom (isti pristup kao `test_geo.py`): svaka ruta, filteri na
  `/results`, straničenje, `facets`, `422` na neispravnu oblast, `404` na
  nepoznat posao, `export.xlsx` vraća pravi tip sadržaja.

Frontend se ne testira automatski; proverava se ručno.

## 12. Zavisnosti

Dodaje se u `requirements.txt`: `fastapi`, `uvicorn[standard]`, `openpyxl`,
`httpx` (za `TestClient`). Frontend zavisnosti su sa CDN-a, ništa se ne
instalira.

Pokretanje: `uvicorn webapp.main:app --reload`, pa `http://127.0.0.1:8000`.

## 13. Otvoreno za kasnije

Namerno izostavljeno: poligon kao oblast, pinovi rezultata na mapi, CSV/JSON
dugmad, red čekanja koji preživljava restart, autentikacija, Docker.
