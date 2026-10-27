# Image Find

Mjet web lokal për të gjetur dhe ruajtur foto produktesh nga faqe të ndryshme sipas kodeve. Nuk ndryshon Excel-in dhe nuk prek repository të tjera.

## Nisja në Windows

Nga ky folder, hap `start_image_find.cmd`, ose përdor:

```powershell
python dantesse_web.py
```

Pastaj hap [http://127.0.0.1:8766](http://127.0.0.1:8766). Programi dëgjon vetëm në kompjuterin lokal.

## Përdorimi

1. Shkruaj path-in absolut të folderit ku do të ruhen fotot, p.sh. `Y:\05_CLIENTS\02_VS\10_DESIGN\02_FEINKOST\18_DANTESSE_PROJECT\300+`.
2. Jep linkun HTTP/HTTPS të webfaqes, qoftë homepage, kategori, faqe kërkimi apo produkt. Programi kërkon kodet në atë webfaqe; një model me `{code}` mund të përdoret kur e ke linkun e saktë të produktit.
3. Vendos kodet në kutinë e tekstit. **Çdo rresht jo bosh është një produkt i veçantë**:

   ```text
   000
   1111
   103210
   ```

4. Kliko **Gjej fotot**. Programi kontrollon faqen e produktit dhe kërkon foton në të dhënat e produktit, në metadata ose në figurat e faqes. Për Dantesse, kërkon edhe referencën e saktë të kodit: një foto e mbetur në server nuk mjafton për ta quajtur produktin të gjetur.
5. Shiko pamjet paraprake. Kliko **Ruaj të gjitha** për t'i ruajtur të gjitha fotot e gjetura me një klikim, ose zgjidh disa dhe kliko **Ruaj të zgjedhurat**. Për foto me disa produkte ose variante ngjyrash, kliko **Lëre jashtë** para ruajtjes masive; fotot e përjashtuara nuk ruhen.

Çdo foto konvertohet dhe ruhet si PNG me emrin e kodit (`103210.png`). Për kodet me `/`, kjo shenjë kodohet në emrin e skedarit (p.sh. `209%2F24.png`). Nëse skedari ekziston, programi nuk e mbishkruan. Produktet që nuk gjenden, fotot e përjashtuara dhe gabimet nuk ruhen. Rezultatet e kërkimit qëndrojnë në kujtesë deri sa ta mbyllësh programin ose të nisësh një kërkim të ri.

Faqet e internetit kanë struktura të ndryshme. Programi kontrollon HTML-në dhe, kur rezultatet ngarkohen me JavaScript, përdor Chromium për të gjetur kartën me kodin e saktë. Nëse webfaqja kërkon hyrje ose nuk lejon kërkim automatik, nuk ruan foto pa verifikim.

Instalo bibliotekën për konvertimin e fotove një herë:

```powershell
python -m pip install -r requirements.txt
```

Për faqet me JavaScript, instalo edhe Playwright dhe Chromium një herë:

```powershell
python -m pip install playwright
python -m playwright install chromium
```

Pa Playwright, kërkimi në HTML dhe linket direkte vazhdojnë të funksionojnë.

## Shënime për kërkime të ardhshme

Këto janë vetëm linke dhe lista kodesh për t'u përdorur më vonë. Nuk nisin kërkim ose shkarkim automatik.

### Riegel

Linku:

```text
https://www.riegel.de/4DCGI/ezshop?action=showPAGE&sKontaktKEY=kb5dLehB1PgBJqJzZWkLzreHToXlB2&sKontaktID=21968625&sTICKCOUNT=1&WorldNr=2&Suchid=8448989&ButtonName=&Beschriftung=&type=seo&multisb=true&Suchbegriff_1=Riegel-Alle-Weine&Suchbegriff_2=&Suchbegriff_3=&Suchbegriff_4=&PageType=eingrenzen&searchManufacturer=go&Suchstartpos=931&responsepage=listing.htm&sortierfeld=0
```

Linku i kërkimit të Riegel (programi vendos kodin pas `query=`):

```text
https://www.riegel.de/4DCGI/ezshop?action=livesuche_search&hid=120&sprachnr=de&enter=livesuche&uuid=&query=
```

Kodet (një për rresht):

```text
57800
57805
57810
58000
58005
58007
58008
58010
58015
58020
58030
58035
58040
```

### WT Group

Linku: https://wt.group/de/home

Kodet (një për rresht):

```text
219
3350
3351
209/24
3130
6244/24
71808
3131
19770
5544
```
