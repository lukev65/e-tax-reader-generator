# Importatore CSV generico (v0.2)

L'importatore `csv` legge un solo file CSV, indipendente dalla banca o dal broker, e ne genera l'estratto fiscale eCH-0196. È pensato per chi non ha un export supportato dagli altri importatori: basta riportare i propri dati in questo formato.

```console
opensteuerauszug process dati.csv --importer csv --tax-year 2024 \
    --tax-calculation-level minimal -o estratto.pdf
```

Nome, cantone e numero cliente si leggono dalla sezione `[general]` del file di configurazione:

```toml
[general]
full_name = "Mario Rossi"
canton = "TI"
language = "it"
client_number = "12345678"     # facoltativo
institution_name = "UBS"       # facoltativo
```

## Regole generali

- Il file contiene **una riga per ogni fatto**: un saldo, un movimento, un interesse, un dividendo o una spesa. La colonna `tipo` indica di quale fatto si tratta.
- Il separatore è `;` (si accetta anche `,`) e la codifica è UTF-8. La prima riga contiene i nomi delle colonne. L'ordine delle colonne è libero e quelle inutili si possono omettere.
- Le date si scrivono `AAAA-MM-GG` oppure `GG.MM.AAAA`.
- I numeri si scrivono `1234.56`. Si accettano anche `1'234.56` e `1234,56`.
- Gli importi sono **lordi e nella valuta originale**. Cambi, valori fiscali, separazione tra valori A e B e imposta preventiva li calcola il programma, con la Kursliste AFC.
- Se un dato contiene `;`, va racchiuso tra virgolette.

## Colonne

| Colonna | Contenuto |
|---|---|
| `tipo` | Tipo di riga (vedi sotto) |
| `data` | Data del fatto |
| `conto` | IBAN o numero del conto (per conti e debiti) |
| `deposito` | Numero del deposito titoli |
| `isin` | Codice ISIN del titolo |
| `valor` | Numero di valore svizzero (facoltativo, aiuta la ricerca nella Kursliste) |
| `descrizione` | Nome del conto, del titolo o della spesa |
| `categoria` | Titoli: `AZIONE`, `FONDO`, `OBBLIGAZIONE`, `OPZIONE`, `STRUTTURATO`, `ALTRO`. Spese: codice eCH-0196 da 1 a 44, oppure 99 (es. `22` = spese di deposito) |
| `valuta` | Codice ISO a 3 lettere (CHF, USD, EUR…) |
| `quantita` | Numero di pezzi (sempre positivo) |
| `prezzo` | Prezzo unitario (acquisti e vendite) |
| `importo` | Importo lordo |
| `data_ex` | Data ex-dividendo (facoltativa) |
| `ritenuta` | Imposta trattenuta sul dividendo, nella stessa valuta (facoltativa, serve per il controllo con la Kursliste) |
| `paese` | Codice ISO a 2 lettere (facoltativo). Di default il paese si ricava dall'IBAN per i conti e dall'ISIN per i titoli |

I nomi delle colonne si possono scrivere anche in inglese (`type`, `date`, `account`, `currency`, `amount`…).

## Tipi di riga

| `tipo` | Colonne obbligatorie | Significato |
|---|---|---|
| `CONTO` | data, conto, valuta, importo | Saldo del conto alla data (di solito il 31.12) |
| `INTERESSE` | data, conto, valuta, importo | Interesse attivo accreditato sul conto |
| `DEBITO` | data, conto, valuta, importo | Debito alla data (importo positivo) |
| `INTERESSE_PASSIVO` | data, conto, valuta, importo | Interesse passivo pagato sul debito |
| `SPESA` | data, valuta, importo, descrizione | Spesa (custodia, gestione…) |
| `SALDO_INIZIALE` | data, deposito, isin, valuta, quantita | Pezzi posseduti **all'inizio** della data (di solito l'1.1) |
| `ACQUISTO` / `VENDITA` | data, deposito, isin, valuta, quantita, prezzo | Movimento del titolo |
| `DIVIDENDO` | data, deposito, isin, valuta, importo | Dividendo o distribuzione |
| `REINVESTIMENTO` | data, deposito, isin, valuta, importo | Reddito reinvestito di un fondo ad accumulazione |
| `GUADAGNO_CAPITALE` | data, deposito, isin, valuta, importo | Distribuzione di utili di capitale |
| `SALDO_FINALE` | data, deposito, isin, quantita | Pezzi posseduti **alla fine** della data (di solito il 31.12) |

- Un conto deve avere sempre la stessa valuta. Per un conto multivaluta si usa una riga per valuta, con un `conto` diverso, ad esempio `CH93…-USD`.
- Basta indicare `descrizione` e `categoria` alla prima riga di ogni titolo.
- `SALDO_INIZIALE` e `SALDO_FINALE` sono facoltativi se i movimenti bastano a ricostruire le posizioni. Se ci sono, il programma controlla che saldo iniziale più movimenti sia uguale al saldo finale e segnala le differenze.
- Tutti gli errori vengono riportati insieme, con il numero di riga, così si possono correggere in una volta sola.

## Esempio

[`tests/samples/import/csv/datalevel_sample_2024.csv`](../tests/samples/import/csv/datalevel_sample_2024.csv) contiene un estratto completo, anonimizzato, con 6 conti, 4 debiti, 10 spese e 26 titoli. Ecco alcune righe:

```
tipo;data;conto;deposito;isin;valor;descrizione;categoria;valuta;quantita;prezzo;importo;data_ex;ritenuta;paese
CONTO;2024-12-31;CH9408497108000071633;;;;Conto privato;;CHF;;;14000;;;
INTERESSE;2024-09-02;CH0508497108000041516;;;;;;USD;;;318.01;;;
SPESA;2024-03-01;CH3908497108000041486;123;;;Custodian fees;22;CHF;;;10.81;;;
SALDO_INIZIALE;2024-01-01;;123;CH0010645932;1064593;Givaudan;AZIONE;CHF;1;;;;;
DIVIDENDO;2024-03-27;;123;CH0010645932;1064593;;;CHF;1;;68.00;2024-03-25;23.80;
SALDO_FINALE;2024-12-31;;123;CH0010645932;1064593;;;CHF;1;;;;;
```
