# pn-cost-analyzer

Analizza i costi AWS degli account SEND (via Cost Explorer) e individua i **costi crescenti** per
**Microservice** (cost allocation tag), **servizio AWS** e **usage type**, separatamente per ogni account.

## Requisiti

- Python 3.9+
- `pip install -r requirements.txt`
- Login SSO attivo sui profili, es. `aws sso login --profile sso_pn-core-dev`
- Permesso `ce:GetCostAndUsage` e tag `Microservice` attivato come *cost allocation tag*

## Utilizzo

```bash
# tutti gli account, ultimi 60 giorni (fine esclusa = oggi)
python3 pn_cost_analyzer.py

# periodo fisso (consigliato per riusare la cache in esecuzioni successive)
python3 pn_cost_analyzer.py --end 2026-10-29

# solo alcuni profili, solo dalla cache
python3 pn_cost_analyzer.py --profiles sso_pn-core-prod-ro,sso_pn-confinfo-prod-ro --end 2026-10-29 --offline
```

Opzioni principali (`--help` per l'elenco completo):

| Opzione | Default | Descrizione |
|---|---|---|
| `--days` | 60 | Giorni analizzati |
| `--end` | oggi | Data di fine **esclusa** (YYYY-MM-DD) |
| `--metric` | UnblendedCost | Metrica Cost Explorer |
| `--tag-key` | Microservice | Tag di cost allocation |
| `--exclude-record-types` | Credit,Refund | RECORD_TYPE esclusi (`''` per nessuno) |
| `--min-service-cost` | 1 | Servizi sotto questa soglia (USD nel periodo) non vengono dettagliati per tag |
| `--min-delta-abs` / `--min-delta-pct` | 5 / 10 | Soglie per considerare un costo "crescente" |
| `--refresh` / `--offline` | | Forza il download / usa solo la cache |
| `--workers` | 4 | Account elaborati in parallelo |

## Come funziona

1. Per ogni profilo: una query giornaliera `SERVICE x USAGE_TYPE` (totale completo), poi per ogni servizio
   sopra soglia una query `TAG Microservice x USAGE_TYPE` filtrata per servizio (Cost Explorer ammette solo
   2 GroupBy). I servizi sotto soglia compaiono con microservice `(non dettagliato)`, le risorse senza tag
   con `(untagged)`. Ogni chiamata Cost Explorer costa 0,01 USD.
2. I dati vengono salvati in `cache/<profilo>/<start>_<end>_<metrica>_<hash>.json` e riutilizzati per lo
   stesso periodo e parametri.
3. Il periodo è diviso in due metà (30 + 30 giorni): per ogni elemento si calcolano delta assoluto, delta %
   e pendenza della regressione lineare sui costi giornalieri. Un elemento è "in crescita" se supera le
   soglie e la pendenza è positiva.

## Output (`output/`)

- `pn-costs_<start>_<end>.html`: report con riepilogo account, grafico giornaliero e tabelle ordinabili
  dei costi crescenti per Microservice, servizio, usage type e combinazione dei tre.
- `pn-costs_<start>_<end>_raw.csv`: dati grezzi giornalieri
  (`profile, account_id, date, microservice, service, usage_type, cost, unit, estimated`).
- `pn-costs_<start>_<end>_trend.csv`: metriche di trend per ogni elemento e livello di aggregazione.

Nota: i costi degli ultimi giorni sono spesso *stimati* da AWS; usare `--refresh` per aggiornarli.
